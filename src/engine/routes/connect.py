"""
Connect Route

Zero Trust WebSocket handshake endpoint. The client signs POST /api/v1/connect
with its device ECDSA key; the backend relays it under /api/v1/web/connect.
WebSignatureMiddleware verifies the signature and sets request.state.zt_user_id,
from which get_current_user derives the identity. This route returns the socket
identity the client needs to open an authenticated WS connection.
"""
import logging

from fastapi import APIRouter, HTTPException, Depends
from pydantic import BaseModel
from typing import Optional

from .auth import get_current_user
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
    current_user: dict = Depends(get_current_user),
    engine: EngineService = Depends(get_engine),
):
    """Return the socket identity for the signature-verified user (WS handshake)."""
    user_id = current_user["user_id"]
    user = await engine.user_storage.get_by_id(user_id)
    if user is None:
        raise HTTPException(status_code=404, detail="User not found")
    return ConnectResponse(
        user_id=str(user.id),
        org_id=str(user.org_id) if user.org_id else None,
        email=user.email or None,
        is_admin=user.is_admin,
    )
