"""WebSignatureMiddleware — Zero Trust enforcement для /api/v1/web/*.

Тонкий HTTP-слой: allowlist, no-signature, парсинг тела, извлечение полей,
сборка payload, проверка route-id. Криптопроверка делегируется ОДНИМ вызовом
SignatureService.verify_request_signature (route/middleware → service → storage),
как game_service.verify_request_signature в rptext.
"""
import hashlib
import json
from typing import Any, Dict, Optional, Tuple
from urllib.parse import urlencode
from uuid import UUID

from fastapi import Request
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request as StarletteRequest
from starlette.responses import JSONResponse

from ..config import Config
from ..services.engine_service import get_engine_service
from ..services.nonce_store import NonceStoreUnavailable
from ..security.signature_payload import build_payload, SIGNATURE_FIELDS
from ..security.route_match import resolve_route_template
from ..unified_logger import get_logger

logger = get_logger("services")


def _suffix(path: str) -> str:
    return path[len(Config.WEB_PREFIX):] if path.startswith(Config.WEB_PREFIX) else path


def _is_allowed(path: str) -> bool:
    s = _suffix(path)
    return any(s.startswith(r) for r in Config.WEB_ALLOWED_ROUTES)


def _requires_signature(path: str) -> bool:
    s = _suffix(path)
    return not any(s.startswith(r) for r in Config.WEB_NO_SIGNATURE_ROUTES)


def _err(status: int, detail: str) -> JSONResponse:
    return JSONResponse(status_code=status, content={"detail": detail})


class WebSignatureMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        path = request.url.path
        if not path.startswith(Config.WEB_PREFIX):
            return await call_next(request)

        if not _is_allowed(path):
            return _err(404, "Route not available via /web/")

        method = request.method
        content_type = request.headers.get("content-type", "")
        is_multipart = content_type.startswith("multipart/form-data")

        # ── Multipart (file upload) ветка ─────────────────────────────────────
        # Тело — multipart/form-data. Подписываемое тело = канонический JSON из
        # { <текстовые form-поля минус signature-поля>, file_sha256 }. Сырые
        # байты переотдаём вниз как есть, чтобы UploadFile внизу остался цел.
        if is_multipart and method in ("POST", "PUT", "PATCH", "DELETE"):
            raw = await request.body()  # читаем поток ОДИН раз, кэшируется в _body
            if not _requires_signature(path):
                # No-signature multipart-роутов сейчас нет, но не ломаем поток:
                # _body уже выставлен (raw), вниз уйдёт нетронутое тело.
                return await self._forward_multipart(request, call_next, raw)
            return await self._dispatch_multipart(request, call_next, raw, content_type, method, path)

        body_bytes = b""
        body_data: Dict[str, Any] = {}
        if method in ("POST", "PUT", "PATCH", "DELETE"):
            body_bytes = await request.body()
            if body_bytes:
                try:
                    body_data = json.loads(body_bytes.decode("utf-8"))
                except json.JSONDecodeError:
                    return _err(400, "Invalid JSON body")

        if not _requires_signature(path):
            return await self._forward(request, call_next, body_data, had_body=bool(body_bytes))

        if method == "GET":
            q = request.query_params
            user_id = q.get("user_id"); signature = q.get("signature")
            nonce = q.get("nonce"); sig_ts = q.get("sig_timestamp"); route_id = q.get("route_id")
        else:
            user_id = body_data.get("user_id"); signature = body_data.get("signature")
            nonce = body_data.get("nonce"); sig_ts = body_data.get("sig_timestamp")
            route_id = body_data.get("route_id")

        signed_body = body_data if method != "GET" else None
        verdict = await self._verify(
            request, method, path, user_id, signature, nonce, sig_ts, route_id, signed_body,
        )
        if isinstance(verdict, JSONResponse):
            return verdict

        request.state.zt_user_id = verdict
        return await self._forward(request, call_next, body_data, had_body=bool(body_bytes))

    async def _verify(self, request, method, path, user_id, signature, nonce, sig_ts,
                      route_id, signed_body: Optional[Dict[str, Any]]):
        """Общая проверка ZT-подписи (JSON и multipart). Возвращает str(uid) при
        успехе либо JSONResponse при отказе.

        signed_body — словарь, по которому строится canonical body для payload
        (None для GET → пустое тело).
        """
        if not all([user_id, signature, nonce, route_id]) or sig_ts is None:
            return _err(401, "Missing signature data")

        try:
            uid = UUID(str(user_id))
        except (ValueError, TypeError, AttributeError):
            return _err(401, "Invalid user_id")

        try:
            timestamp = int(sig_ts)
        except (ValueError, TypeError):
            return _err(401, "Invalid sig_timestamp")

        target_path = path.replace(Config.WEB_PREFIX, "/api/v1", 1)
        matched = resolve_route_template(request.app.routes, method, target_path)
        if matched is None:
            return _err(404, "Unknown route")
        if route_id != matched:
            logger.warning("route-id mismatch", operation="web_signature",
                           signed=route_id, actual=matched, user_id=str(uid))
            return _err(401, "Route mismatch")

        payload = build_payload(method, route_id, signed_body, nonce, timestamp)

        engine = get_engine_service()
        try:
            ok, result = await engine.signature_service.verify_request_signature(
                uid, payload, signature, nonce, timestamp
            )
        except NonceStoreUnavailable:
            return _err(503, "Signature verifier unavailable, please retry")
        if not ok:
            return _err(401, result.get("error", "Invalid device signature"))

        return str(uid)

    async def _dispatch_multipart(self, request, call_next, raw: bytes,
                                  content_type: str, method: str, path: str):
        # Парсим multipart из СЫРЫХ байт через отдельный Request, не трогая
        # поток исходного запроса (его _body уже = raw, уйдёт вниз нетронутым).
        try:
            form = await self._parse_multipart(raw, content_type)
        except Exception:
            return _err(400, "Invalid multipart body")

        # Извлекаем signature-поля из FORM (не из файла).
        user_id = form.get("user_id"); signature = form.get("signature")
        nonce = form.get("nonce"); sig_ts = form.get("sig_timestamp")
        route_id = form.get("route_id")

        # file_sha256 по байтам загружаемого файла (конвенциональное имя поля "file").
        file_sha256, file_err = await self._extract_file_sha256(form)
        if file_err is not None:
            return _err(400, file_err)

        # signed_dict = текстовые form-поля (минус signature-поля) + file_sha256.
        signed_dict: Dict[str, Any] = {}
        for key, value in form.multi_items():
            if hasattr(value, "filename"):  # UploadFile → пропускаем (это файл)
                continue
            if key in SIGNATURE_FIELDS:
                continue
            signed_dict[key] = value
        signed_dict["file_sha256"] = file_sha256

        verdict = await self._verify(
            request, method, path, user_id, signature, nonce, sig_ts, route_id, signed_dict,
        )
        if isinstance(verdict, JSONResponse):
            return verdict

        request.state.zt_user_id = verdict
        return await self._forward_multipart(request, call_next, raw)

    async def _parse_multipart(self, raw: bytes, content_type: str):
        """Распарсить multipart из сырых байт через одноразовый Request."""
        scope = {
            "type": "http",
            "method": "POST",
            "headers": [(b"content-type", content_type.encode("latin-1"))],
        }
        tmp = StarletteRequest(scope)
        tmp._body = raw

        async def _receive():
            return {"type": "http.request", "body": raw, "more_body": False}

        tmp._receive = _receive
        return await tmp.form()

    async def _extract_file_sha256(self, form) -> Tuple[Optional[str], Optional[str]]:
        """sha256 байт загружаемого файла. Конвенция: поле 'file'. Если такого нет,
        берём единственный файл в форме. Несколько файлов / отсутствие → ошибка."""
        uploads = [(k, v) for k, v in form.multi_items() if hasattr(v, "filename")]
        if not uploads:
            return None, "No file in multipart body"
        chosen = None
        named = [v for k, v in uploads if k == "file"]
        if len(named) == 1:
            chosen = named[0]
        elif len(named) > 1:
            return None, "Multiple 'file' fields not supported"
        elif len(uploads) == 1:
            chosen = uploads[0][1]
        else:
            return None, "Ambiguous file field; expected single 'file'"

        await chosen.seek(0)
        file_bytes = await chosen.read()
        await chosen.seek(0)
        return hashlib.sha256(file_bytes).hexdigest(), None

    async def _forward_multipart(self, request, call_next, raw: bytes):
        # Переписываем /web → /api/v1 и переотдаём ИСХОДНЫЕ сырые байты multipart,
        # чтобы downstream-роут заново распарсил intact-форму (UploadFile цел).
        request.scope["path"] = request.scope["path"].replace(Config.WEB_PREFIX, "/api/v1", 1)
        request.scope["raw_path"] = request.scope["path"].encode("ascii")
        request._body = raw
        return await call_next(request)

    async def _forward(self, request, call_next, body_data, had_body):
        request.scope["path"] = request.scope["path"].replace(Config.WEB_PREFIX, "/api/v1", 1)
        request.scope["raw_path"] = request.scope["path"].encode("ascii")

        if had_body:
            clean = {k: v for k, v in body_data.items() if k not in SIGNATURE_FIELDS}
            request._body = json.dumps(clean).encode("utf-8")

        if request.query_params:
            qp = dict(request.query_params)
            if any(f in qp for f in SIGNATURE_FIELDS) or "user_id" in qp:
                for f in SIGNATURE_FIELDS:
                    qp.pop(f, None)
                request.scope["query_string"] = urlencode(qp).encode("ascii")

        return await call_next(request)
