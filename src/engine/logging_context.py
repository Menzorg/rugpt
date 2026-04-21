"""
Correlation ID propagation for cross-subsystem tracing.

Exposes:
- `correlation_id_var` — ContextVar, binds correlation_id to the current async
  scope. Read/write from anywhere without threading it through function
  signatures.
- `get_correlation_id()` — convenience reader.
- `bind_correlation_id(value)` — setter that returns a reset token. Used by
  Kafka consumers and any other code path that enters outside HTTP (e.g.
  scheduler) and needs to establish a correlation_id for its own flow.
- `CorrelationIDFilter` — logging filter that stamps every LogRecord with the
  current correlation_id so it shows up in the log format string.
- `CorrelationIDMiddleware` — ASGI middleware that extracts `X-Correlation-ID`
  from the request headers (or generates a new UUID), binds it for the request
  lifecycle, and echoes it back in the response headers.
- `RequestLoggingMiddleware` — logs every HTTP request start/finish with the
  method, path, status and duration, so every route is observable without
  editing each endpoint individually.
"""
import contextvars
import logging
import time
import uuid
from typing import Optional

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response


_HEADER = "X-Correlation-ID"

correlation_id_var: contextvars.ContextVar[str] = contextvars.ContextVar(
    "correlation_id", default="-"
)


def get_correlation_id() -> str:
    return correlation_id_var.get()


def bind_correlation_id(value: Optional[str]) -> contextvars.Token:
    """Bind a correlation_id to the current context. Returns a token — caller
    must pass it to `correlation_id_var.reset(token)` when the scope ends."""
    return correlation_id_var.set(value or str(uuid.uuid4()))


class CorrelationIDFilter(logging.Filter):
    """Stamps every LogRecord with the current correlation_id."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.correlation_id = correlation_id_var.get()
        return True


_QUERY_PARAM = "correlation_id"


class CorrelationIDMiddleware(BaseHTTPMiddleware):
    """Binds correlation_id for the request lifecycle.

    Source priority:
    1. `correlation_id` query parameter — primary channel (webclient injects it
       into every request URL centrally, no header dependency).
    2. `X-Correlation-ID` header — fallback for callers that still set a header.
    3. Freshly generated UUID — if neither is present (e.g. direct ops curl).
    """

    async def dispatch(self, request: Request, call_next) -> Response:
        incoming = (
            request.query_params.get(_QUERY_PARAM)
            or request.headers.get(_HEADER)
            or request.headers.get(_HEADER.lower())
        )
        token = bind_correlation_id(incoming)
        try:
            response = await call_next(request)
            response.headers[_HEADER] = correlation_id_var.get()
            return response
        finally:
            correlation_id_var.reset(token)


_request_logger = logging.getLogger("rugpt.request")

# Paths excluded from request logging to avoid flooding on high-frequency polls.
_SKIP_PATHS = {
    "/api/v1/health",
    "/api/v1/health/ready",
    "/api/v1/health/live",
}


class RequestLoggingMiddleware(BaseHTTPMiddleware):
    """Logs start and finish of every HTTP request with method, path, status,
    duration. Correlation_id is injected by the logging filter, so the lines
    are trivially greppable per trace."""

    async def dispatch(self, request: Request, call_next) -> Response:
        path = request.url.path
        if path in _SKIP_PATHS:
            return await call_next(request)

        method = request.method
        _request_logger.info(f"--> {method} {path}")
        started = time.perf_counter()
        status = 500
        try:
            response = await call_next(request)
            status = response.status_code
            return response
        finally:
            duration_ms = (time.perf_counter() - started) * 1000
            _request_logger.info(f"<-- {method} {path} {status} ({duration_ms:.1f}ms)")
