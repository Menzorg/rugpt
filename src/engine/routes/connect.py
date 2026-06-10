"""
Connect Route

Zero Trust WebSocket handshake endpoint. The client signs POST /api/v1/connect
with its device ECDSA key; the backend relays it under /api/v1/web/connect.
WebSignatureMiddleware verifies the signature and sets request.state.zt_user_id.
This route reads that signature-verified identity DIRECTLY (no JWT): the device
signature is the proof. It deliberately does NOT use get_current_user, which
additionally requires a JWT Authorization header that the WS handshake (signature
only) does not carry.
"""
import logging
from uuid import UUID

from fastapi import APIRouter, HTTPException, Depends, Request
from pydantic import BaseModel
from typing import Optional

from ..services.engine_service import get_engine_service, EngineService

logger = logging.getLogger("rugpt.routes.connect")

router = APIRouter(prefix="/api/v1", tags=["connect"])


# Dependency
def get_engine() -> EngineService:
    return get_engine_service()


class ConnectResponse(BaseModel):
    """Socket identity for an authenticated WS connection."""
    user_id: str
    org_id: Optional[str] = None
    email: Optional[str] = None
    is_admin: bool = False


@router.post("/connect", response_model=ConnectResponse)
async def connect(
    request: Request,
    engine: EngineService = Depends(get_engine),
):
    """Return the socket identity for the signature-verified user (WS handshake)."""
    zt_user_id = getattr(request.state, "zt_user_id", None)
    if zt_user_id is None:
        raise HTTPException(status_code=401, detail="Unsigned request")
    user = await engine.user_storage.get_by_id(UUID(str(zt_user_id)))
    if user is None:
        raise HTTPException(status_code=404, detail="User not found")
    return ConnectResponse(
        user_id=str(user.id),
        org_id=str(user.org_id) if user.org_id else None,
        email=user.email or None,
        is_admin=user.is_admin,
    )
