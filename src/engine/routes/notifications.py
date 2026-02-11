"""
Notification Routes

Endpoints for notification channel management, Telegram webhook, and log.
"""
import logging
from typing import Optional, List
from uuid import UUID

from fastapi import APIRouter, HTTPException, Depends, Request
from pydantic import BaseModel

from ..services.engine_service import get_engine_service
from .auth import get_current_user

logger = logging.getLogger("rugpt.routes.notifications")
router = APIRouter(prefix="/notifications", tags=["notifications"])


# ============================================
# Request/Response Models
# ============================================

class RegisterChannelRequest(BaseModel):
    """Register notification channel"""
    channel_type: str                    # 'telegram', 'email'
    config: dict                         # {"chat_id": "..."} or {"email": "..."}
    priority: int = 0


class ChannelResponse(BaseModel):
    """Notification channel response"""
    id: str
    user_id: str
    org_id: str
    channel_type: str
    config: dict
    is_enabled: bool
    is_verified: bool
    priority: int
    created_at: str
    updated_at: str


class NotificationLogResponse(BaseModel):
    """Notification log entry"""
    id: str
    user_id: str
    channel_type: str
    event_id: Optional[str]
    role_id: Optional[str]
    content: str
    status: str
    attempts: int
    error_message: Optional[str]
    created_at: str
    updated_at: str


# ============================================
# Channel Routes
# ============================================

@router.get("/channels", response_model=List[ChannelResponse])
async def list_channels(current_user: dict = Depends(get_current_user)):
    """List notification channels for current user"""
    engine = get_engine_service()
    channels = await engine.notification_service.get_user_channels(
        current_user["user_id"], enabled_only=False
    )
    return [ChannelResponse(**c.to_dict()) for c in channels]


@router.post("/channels", response_model=ChannelResponse)
async def register_channel(
    request: RegisterChannelRequest,
    current_user: dict = Depends(get_current_user),
):
    """Register or update a notification channel"""
    engine = get_engine_service()

    if request.channel_type not in ("telegram", "email"):
        raise HTTPException(
            status_code=400,
            detail="channel_type must be 'telegram' or 'email'"
        )

    channel = await engine.notification_service.register_channel(
        user_id=current_user["user_id"],
        org_id=current_user["org_id"],
        channel_type=request.channel_type,
        config=request.config,
        priority=request.priority,
    )
    return ChannelResponse(**channel.to_dict())


@router.delete("/channels/{channel_type}")
async def remove_channel(
    channel_type: str,
    current_user: dict = Depends(get_current_user),
):
    """Remove a notification channel"""
    engine = get_engine_service()
    removed = await engine.notification_service.remove_channel(
        current_user["user_id"], channel_type
    )
    if not removed:
        raise HTTPException(status_code=404, detail="Channel not found")
    return {"success": True, "message": f"Channel '{channel_type}' removed"}


@router.post("/channels/{channel_type}/verify")
async def verify_channel(
    channel_type: str,
    current_user: dict = Depends(get_current_user),
):
    """Mark a channel as verified (admin or after confirmation flow)"""
    engine = get_engine_service()
    channel = await engine.notification_service.verify_channel(
        current_user["user_id"], channel_type
    )
    if not channel:
        raise HTTPException(status_code=404, detail="Channel not found")
    return ChannelResponse(**channel.to_dict())


# ============================================
# Telegram Webhook
# ============================================

@router.post("/telegram/webhook")
async def telegram_webhook(request: Request):
    """
    Telegram Bot webhook endpoint.

    Handles /start command — saves chat_id for the user.
    The user sends /start <user_id> to link their Telegram to RuGPT.
    """
    engine = get_engine_service()

    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON")

    message = body.get("message", {})
    text = message.get("text", "")
    chat = message.get("chat", {})
    chat_id = chat.get("id")

    if not chat_id:
        return {"ok": True}

    # Handle /start <user_id>
    if text.startswith("/start"):
        parts = text.split()
        if len(parts) >= 2:
            user_id_str = parts[1]
            try:
                user_id = UUID(user_id_str)
            except ValueError:
                logger.warning(f"Invalid user_id in /start: {user_id_str}")
                return {"ok": True}

            # Look up user to get org_id
            user = await engine.user_storage.get_by_id(user_id)
            if not user:
                logger.warning(f"User not found for Telegram link: {user_id}")
                return {"ok": True}

            # Register + verify the Telegram channel
            channel = await engine.notification_service.register_channel(
                user_id=user_id,
                org_id=user.org_id,
                channel_type="telegram",
                config={"chat_id": str(chat_id)},
                priority=10,  # Telegram gets high priority
            )
            await engine.notification_service.verify_channel(user_id, "telegram")

            # Send confirmation via Telegram
            telegram_sender = engine.notification_service._senders.get("telegram")
            if telegram_sender:
                await telegram_sender.send(
                    {"chat_id": str(chat_id)},
                    f"RuGPT notifications linked for user {user.name}."
                )

            logger.info(
                f"Telegram linked for user {user_id}, chat_id={chat_id}"
            )
        else:
            logger.debug(f"/start without user_id from chat_id={chat_id}")

    return {"ok": True}


# ============================================
# Notification Log
# ============================================

@router.get("/log", response_model=List[NotificationLogResponse])
async def get_notification_log(
    limit: int = 50,
    current_user: dict = Depends(get_current_user),
):
    """Get notification delivery log for current user"""
    engine = get_engine_service()
    logs = await engine.notification_service.get_notification_log(
        current_user["user_id"], limit=min(limit, 200)
    )
    return [NotificationLogResponse(**l.to_dict()) for l in logs]
