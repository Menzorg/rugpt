"""
Internal Route

Read-only endpoints for trusted, co-located callers (the NestJS webclient
backend) that live BEHIND the network boundary. These routes are NOT mounted
under /web and carry NO ECDSA signature: security rests on the network — the
engine binds to localhost, the webclient reaches it only over the VPN, and
nginx MUST NOT expose /api/v1/internal/* publicly.

Currently used by the WS gateway to authorize chat:join room subscription:
the backend (untrusted, but inside the boundary) asks the engine whether a
user is a participant of a chat before letting it join that chat's room.
"""
import logging
from uuid import UUID

from fastapi import APIRouter, HTTPException, Depends
from pydantic import BaseModel
from typing import List

from ..services.engine_service import get_engine_service, EngineService

logger = logging.getLogger("rugpt.routes.internal")

router = APIRouter(prefix="/api/v1/internal", tags=["internal"])


# Dependency
def get_engine() -> EngineService:
    return get_engine_service()


class ChatParticipantsResponse(BaseModel):
    participants: List[str]


@router.get("/chats/{chat_id}/participants", response_model=ChatParticipantsResponse)
async def get_chat_participants(
    chat_id: UUID,
    engine: EngineService = Depends(get_engine),
):
    """Return the participant user ids of a chat (read-only, no auth).

    Used by the backend to verify chat:join room subscription. 404 if the
    chat does not exist.
    """
    chat = await engine.chat_service.get_chat(chat_id)
    if chat is None:
        raise HTTPException(status_code=404, detail="Chat not found")
    return ChatParticipantsResponse(
        participants=[str(p) for p in chat.participants]
    )
