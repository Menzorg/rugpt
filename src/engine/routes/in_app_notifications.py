"""
In-App Notifications Routes

Bell icon notification endpoints:
- GET /in-app-notifications — list notifications (with pagination, unread filter)
- GET /in-app-notifications/unread-count — badge count
- PATCH /in-app-notifications/{id}/read — mark one as read
- POST /in-app-notifications/read-all — mark all as read
"""
import logging
from typing import Optional, List
from uuid import UUID

from fastapi import APIRouter, HTTPException, Depends, Query
from pydantic import BaseModel

from ..services.engine_service import get_engine_service
from .auth import get_current_user

logger = logging.getLogger("rugpt.routes.in_app_notifications")
router = APIRouter(prefix="/in-app-notifications", tags=["in-app-notifications"])


class NotificationResponse(BaseModel):
    id: str
    user_id: str
    org_id: str
    type: str
    title: str
    content: Optional[str]
    reference_type: Optional[str]
    reference_id: Optional[str]
    is_read: bool
    created_at: str


class UnreadCountResponse(BaseModel):
    count: int


@router.get("", response_model=List[NotificationResponse])
async def list_notifications(
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    unread_only: bool = Query(False),
    current_user: dict = Depends(get_current_user),
):
    """List notifications for the current user"""
    engine = get_engine_service()
    notifications = await engine.in_app_notification_service.list_for_user(
        user_id=current_user["user_id"],
        limit=limit,
        offset=offset,
        unread_only=unread_only,
    )
    return [NotificationResponse(**n.to_dict()) for n in notifications]


@router.get("/unread-count", response_model=UnreadCountResponse)
async def get_unread_count(current_user: dict = Depends(get_current_user)):
    """Get unread notification count for bell badge"""
    engine = get_engine_service()
    count = await engine.in_app_notification_service.count_unread(current_user["user_id"])
    return UnreadCountResponse(count=count)


@router.patch("/{notification_id}/read")
async def mark_notification_read(
    notification_id: str,
    current_user: dict = Depends(get_current_user),
):
    """Mark a single notification as read"""
    engine = get_engine_service()
    try:
        notif_uuid = UUID(notification_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid notification ID")

    notification = await engine.in_app_notification_service.get(notif_uuid)
    if not notification:
        raise HTTPException(status_code=404, detail="Notification not found")
    if notification.user_id != current_user["user_id"]:
        raise HTTPException(status_code=403, detail="Access denied")

    await engine.in_app_notification_service.mark_read(notif_uuid)
    return {"success": True, "message": "Notification marked as read"}


@router.post("/read-all")
async def mark_all_read(current_user: dict = Depends(get_current_user)):
    """Mark all notifications as read for the current user"""
    engine = get_engine_service()
    count = await engine.in_app_notification_service.mark_all_read(current_user["user_id"])
    return {"success": True, "marked_count": count}
