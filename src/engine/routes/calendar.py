"""
Calendar Routes

Endpoints for calendar event management.
"""
import logging
from datetime import datetime
from typing import Optional, List
from uuid import UUID

from fastapi import APIRouter, HTTPException, Depends
from pydantic import BaseModel

from ..services.engine_service import get_engine_service
from .auth import get_current_user

logger = logging.getLogger("rugpt.routes.calendar")
router = APIRouter(prefix="/calendar", tags=["calendar"])


# ============================================
# Request/Response Models
# ============================================

class CreateEventRequest(BaseModel):
    """Create calendar event request"""
    role_id: str
    title: str
    description: Optional[str] = None
    event_type: str = "one_time"              # "one_time" or "recurring"
    scheduled_at: Optional[str] = None         # ISO datetime for one_time
    cron_expression: Optional[str] = None      # cron for recurring


class UpdateEventRequest(BaseModel):
    """Update calendar event request"""
    title: Optional[str] = None
    description: Optional[str] = None
    scheduled_at: Optional[str] = None
    cron_expression: Optional[str] = None
    metadata: Optional[dict] = None


class EventResponse(BaseModel):
    """Calendar event response"""
    id: str
    role_id: str
    org_id: str
    title: str
    description: Optional[str]
    event_type: str
    scheduled_at: Optional[str]
    cron_expression: Optional[str]
    next_trigger_at: Optional[str]
    last_triggered_at: Optional[str]
    trigger_count: int
    source_chat_id: Optional[str]
    source_message_id: Optional[str]
    metadata: dict
    created_by_user_id: Optional[str]
    is_active: bool
    created_at: str
    updated_at: str


# ============================================
# Routes
# ============================================

@router.get("/events", response_model=List[EventResponse])
async def list_events(current_user: dict = Depends(get_current_user)):
    """List calendar events in current organization"""
    engine = get_engine_service()
    events = await engine.calendar_service.list_events(current_user["org_id"])
    return [EventResponse(**e.to_dict()) for e in events]


@router.post("/events", response_model=EventResponse)
async def create_event(
    request: CreateEventRequest,
    current_user: dict = Depends(get_current_user),
):
    """Create a calendar event"""
    engine = get_engine_service()

    try:
        role_uuid = UUID(request.role_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid role_id")

    # Verify role exists and belongs to org
    role = await engine.role_storage.get_by_id(role_uuid)
    if not role:
        raise HTTPException(status_code=404, detail="Role not found")
    if role.org_id != current_user["org_id"]:
        raise HTTPException(status_code=403, detail="Access denied")

    # Parse scheduled_at
    scheduled_at = None
    if request.scheduled_at:
        try:
            scheduled_at = datetime.fromisoformat(request.scheduled_at)
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid scheduled_at format (use ISO)")

    try:
        event = await engine.calendar_service.create_event(
            role_id=role_uuid,
            org_id=current_user["org_id"],
            title=request.title,
            description=request.description,
            event_type=request.event_type,
            scheduled_at=scheduled_at,
            cron_expression=request.cron_expression,
            created_by_user_id=current_user["user_id"],
        )
        return EventResponse(**event.to_dict())
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.get("/events/{event_id}", response_model=EventResponse)
async def get_event(
    event_id: str,
    current_user: dict = Depends(get_current_user),
):
    """Get a calendar event by ID"""
    engine = get_engine_service()

    try:
        event_uuid = UUID(event_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid event ID")

    event = await engine.calendar_service.get_event(event_uuid)
    if not event:
        raise HTTPException(status_code=404, detail="Event not found")
    if event.org_id != current_user["org_id"]:
        raise HTTPException(status_code=403, detail="Access denied")

    return EventResponse(**event.to_dict())


@router.patch("/events/{event_id}", response_model=EventResponse)
async def update_event(
    event_id: str,
    request: UpdateEventRequest,
    current_user: dict = Depends(get_current_user),
):
    """Update a calendar event"""
    engine = get_engine_service()

    try:
        event_uuid = UUID(event_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid event ID")

    # Verify ownership
    event = await engine.calendar_service.get_event(event_uuid)
    if not event:
        raise HTTPException(status_code=404, detail="Event not found")
    if event.org_id != current_user["org_id"]:
        raise HTTPException(status_code=403, detail="Access denied")

    # Parse scheduled_at
    scheduled_at = None
    if request.scheduled_at:
        try:
            scheduled_at = datetime.fromisoformat(request.scheduled_at)
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid scheduled_at format")

    try:
        updated = await engine.calendar_service.update_event(
            event_id=event_uuid,
            title=request.title,
            description=request.description,
            scheduled_at=scheduled_at,
            cron_expression=request.cron_expression,
            metadata=request.metadata,
        )
        return EventResponse(**updated.to_dict())
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.delete("/events/{event_id}")
async def deactivate_event(
    event_id: str,
    current_user: dict = Depends(get_current_user),
):
    """Deactivate a calendar event"""
    engine = get_engine_service()

    try:
        event_uuid = UUID(event_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid event ID")

    # Verify ownership
    event = await engine.calendar_service.get_event(event_uuid)
    if not event:
        raise HTTPException(status_code=404, detail="Event not found")
    if event.org_id != current_user["org_id"]:
        raise HTTPException(status_code=403, detail="Access denied")

    await engine.calendar_service.deactivate_event(event_uuid)
    return {"success": True, "message": "Event deactivated"}


@router.get("/roles/{role_id}/events", response_model=List[EventResponse])
async def list_role_events(
    role_id: str,
    current_user: dict = Depends(get_current_user),
):
    """List calendar events for a specific role"""
    engine = get_engine_service()

    try:
        role_uuid = UUID(role_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid role ID")

    # Verify role belongs to org
    role = await engine.role_storage.get_by_id(role_uuid)
    if not role:
        raise HTTPException(status_code=404, detail="Role not found")
    if role.org_id != current_user["org_id"]:
        raise HTTPException(status_code=403, detail="Access denied")

    events = await engine.calendar_service.list_role_events(role_uuid)
    return [EventResponse(**e.to_dict()) for e in events]
