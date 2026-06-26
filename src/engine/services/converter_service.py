"""
Converter Service

Converts spreadsheet files to xlsx via unoserver.
Owns a single persistent aiohttp.ClientSession for connection reuse.
Call startup() once at engine boot and shutdown() at teardown.
"""

import aiohttp

from src.engine.unified_logger import get_logger
from ..config import Config

logger = get_logger("services")


class ConverterService:
    def __init__(self, unoserver_url: str) -> None:
        self._url = unoserver_url
        self._session: aiohttp.ClientSession | None = None

    async def startup(self) -> None:
        """Open the shared HTTP session. Call once at engine startup."""
        self._session = aiohttp.ClientSession()
        logger.info("ConverterService started (unoserver=%s)", self._url)

    async def shutdown(self) -> None:
        """Close the shared HTTP session. Call once at engine teardown."""
        if self._session is not None:
            await self._session.close()
            self._session = None
            logger.info("ConverterService shut down")

    async def to_xlsx(self, data: bytes, filename: str) -> bytes:
        """Convert a spreadsheet file to xlsx via unoserver.

        Uses the shared session for connection pooling.
        Timeout is intentionally high — the unoserver container enforces its own
        conversion deadline; this just guards against a completely unresponsive host.
        Raises ValueError on HTTP error or network failure so callers can surface HTTP 400.
        """
        if self._session is None:
            raise RuntimeError("ConverterService.startup() was not called")

        form = aiohttp.FormData()
        form.add_field("file", data, filename=filename, content_type="application/octet-stream")
        form.add_field("convert-to", "xlsx")
        async with self._session.post(
            self._url,
            data=form,
            timeout=aiohttp.ClientTimeout(total=400),
        ) as resp:
            body = await resp.read()
            if resp.status != 200:
                raise ValueError(
                    f"unoserver conversion failed: HTTP {resp.status} — {body[:200]!r}"
                )
            return body
