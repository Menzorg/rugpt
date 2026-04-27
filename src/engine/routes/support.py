"""
Support Routes — tech-support ticket lifecycle endpoints.

Client-facing routes (Task 14). Operator routes (queue / take / operator/my)
are in Task 15. Common chat/events endpoints are Task 16.

Endpoints:
- POST   /support/tickets               — create new ticket + chat
- GET    /support/tickets/my            — list own tickets (optional status/limit)
- GET    /support/tickets/{ticket_id}   — get ticket (requester or RuGPT Support op)
- POST   /support/tickets/{id}/escalate — client requests human operator
- POST   /support/tickets/{id}/close    — close (either party may close)

The router is created here but NOT mounted on app — that's Task 19.
"""
import logging
from typing import Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from ..config import Config
from ..models.support_ticket import SupportTicketCategory, SupportTicketStatus
from ..services.engine_service import get_engine_service
from .auth import get_current_user

logger = logging.getLogger("rugpt.routes.support")
router = APIRouter(prefix="/support", tags=["support"])


# ============================================
# Request bodies
# ============================================

class CreateTicketRequest(BaseModel):
    """POST /support/tickets body."""
    category: str  # "how_to" | "bug" | "other"
    initial_message: str


# ============================================
# Helpers
# ============================================

def _map_value_error(exc: ValueError) -> HTTPException:
    """Map ValueError raised by service layer to HTTP status.

    Convention:
    - "not found" in message  → 404
    - everything else (already closed / invalid state / etc.) → 400
    """
    msg = str(exc)
    if "not found" in msg.lower():
        return HTTPException(status_code=404, detail=msg)
    return HTTPException(status_code=400, detail=msg)


async def _load_user_or_404(engine, current_user: dict):
    """Resolve current_user dict → User object, 404 if missing."""
    user = await engine.user_storage.get_by_id(current_user["user_id"])
    if user is None:
        raise HTTPException(status_code=404, detail="User not found")
    return user


# ============================================
# POST /support/tickets
# ============================================

@router.post("/tickets")
async def create_ticket(
    body: CreateTicketRequest,
    current_user: dict = Depends(get_current_user),
):
    """
    Create a new support ticket + dedicated chat.

    Routing happens inside service layer:
    - HOW_TO → AI joins chat, queue NOT notified
    - BUG / OTHER → handoff stamped immediately, queue notified
    """
    engine = get_engine_service()

    # Validate category enum before calling service
    try:
        category = SupportTicketCategory(body.category)
    except ValueError:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid category: {body.category}",
        )

    user = await _load_user_or_404(engine, current_user)

    ticket, chat = await engine.support_ticket_service.create_ticket(
        requester=user,
        category=category,
        initial_message=body.initial_message,
    )
    logger.info(
        "create_ticket: id=%s category=%s requester=%s chat=%s",
        ticket.id, category.value, user.id, chat.id,
    )
    return {"ticket": ticket.to_dict(), "chat_id": str(chat.id)}


# ============================================
# GET /support/tickets/my
# ============================================

@router.get("/tickets/my")
async def list_my_tickets(
    status: Optional[str] = Query(None, description="open | in_progress | closed"),
    limit: int = Query(50, ge=1, le=200),
    current_user: dict = Depends(get_current_user),
):
    """List tickets requested by the current user (sorted by created_at DESC)."""
    engine = get_engine_service()

    status_enum: Optional[SupportTicketStatus] = None
    if status is not None:
        try:
            status_enum = SupportTicketStatus(status)
        except ValueError:
            raise HTTPException(
                status_code=400,
                detail=f"Invalid status: {status}",
            )

    tickets = await engine.support_ticket_storage.list_by_requester(
        current_user["user_id"], status=status_enum, limit=limit,
    )
    return [t.to_dict() for t in tickets]


# ============================================
# GET /support/tickets/{ticket_id}
# ============================================

@router.get("/tickets/{ticket_id}")
async def get_ticket(
    ticket_id: UUID,
    current_user: dict = Depends(get_current_user),
):
    """
    Fetch a single ticket.

    Auth:
    - Requester (ticket.requester_user_id == current_user) — gets the basic payload.
    - RuGPT Support operator (current_user.org_id == RUGPT_SUPPORT_ORG_ID) — gets
      the basic payload plus cross-org requester profile fields:
      `requester_name`, `requester_email`, `requester_org_name`, `requester_org_id`.
    - Anyone else → 403.
    """
    engine = get_engine_service()

    ticket = await engine.support_ticket_storage.get_by_id(ticket_id)
    if ticket is None:
        raise HTTPException(status_code=404, detail="Ticket not found")

    user_id: UUID = current_user["user_id"]
    user_org_id: UUID = current_user["org_id"]

    is_requester = ticket.requester_user_id == user_id
    is_operator = user_org_id == Config.RUGPT_SUPPORT_ORG_ID

    if not (is_requester or is_operator):
        raise HTTPException(status_code=403, detail="Access denied")

    payload = ticket.to_dict()

    if is_operator:
        # Cross-org embed for operator view: profile of a user from a different
        # org is normally not accessible via /users (orgship-bound). Embed here
        # so operator UI can identify the client without extra plumbing.
        requester = await engine.user_storage.get_by_id(ticket.requester_user_id)
        org = await engine.org_storage.get_by_id(ticket.requester_org_id)
        if requester is not None:
            payload["requester_name"] = requester.name
            payload["requester_email"] = requester.email
        if org is not None:
            payload["requester_org_name"] = org.name
            payload["requester_org_id"] = str(org.id)

    return payload


# ============================================
# POST /support/tickets/{ticket_id}/escalate
# ============================================

@router.post("/tickets/{ticket_id}/escalate")
async def escalate(
    ticket_id: UUID,
    current_user: dict = Depends(get_current_user),
):
    """Client clicks 'Call operator' — stamp ai_handoff_at + notify queue."""
    engine = get_engine_service()

    user = await _load_user_or_404(engine, current_user)

    try:
        ticket = await engine.support_ticket_service.escalate(ticket_id, user)
    except PermissionError as e:
        raise HTTPException(status_code=403, detail=str(e))
    except ValueError as e:
        raise _map_value_error(e)

    return ticket.to_dict()


# ============================================
# POST /support/tickets/{ticket_id}/close
# ============================================

@router.post("/tickets/{ticket_id}/close")
async def close_ticket(
    ticket_id: UUID,
    current_user: dict = Depends(get_current_user),
):
    """Either party (requester or operator) closes the ticket."""
    engine = get_engine_service()

    user = await _load_user_or_404(engine, current_user)

    try:
        ticket = await engine.support_ticket_service.close_ticket(ticket_id, user)
    except PermissionError as e:
        raise HTTPException(status_code=403, detail=str(e))
    except ValueError as e:
        raise _map_value_error(e)

    return ticket.to_dict()


# ============================================================
# Operator routes (Task 15)
# ============================================================


def _require_operator(current_user: dict) -> None:
    """Raise 403 if the caller is not a member of the RuGPT Support org."""
    if current_user["org_id"] != Config.RUGPT_SUPPORT_ORG_ID:
        raise HTTPException(status_code=403, detail="Operator access only")


@router.get("/queue")
async def get_queue(
    limit: int = Query(100, ge=1, le=500),
    current_user: dict = Depends(get_current_user),
):
    """List unassigned open tickets — RuGPT Support operators only."""
    _require_operator(current_user)
    engine = get_engine_service()
    tickets = await engine.support_ticket_storage.list_queue(limit)
    return [t.to_dict() for t in tickets]


@router.post("/tickets/{ticket_id}/take")
async def take_ticket(
    ticket_id: UUID,
    current_user: dict = Depends(get_current_user),
):
    """Take a ticket from the queue (atomic CAS). Returns 409 if already taken."""
    _require_operator(current_user)
    engine = get_engine_service()
    operator = await _load_user_or_404(engine, current_user)
    try:
        ticket = await engine.support_ticket_service.take_ticket(ticket_id, operator)
    except PermissionError as e:
        raise HTTPException(status_code=403, detail=str(e))
    except ValueError as e:
        msg = str(e)
        msg_lower = msg.lower()
        if "already taken" in msg_lower or "not in open state" in msg_lower:
            raise HTTPException(status_code=409, detail=msg)
        if "not found" in msg_lower:
            raise HTTPException(status_code=404, detail=msg)
        raise HTTPException(status_code=400, detail=msg)
    return ticket.to_dict()


@router.get("/operator/my")
async def get_operator_my(
    limit: int = Query(100, ge=1, le=500),
    current_user: dict = Depends(get_current_user),
):
    """List tickets currently assigned to the calling operator."""
    _require_operator(current_user)
    engine = get_engine_service()
    tickets = await engine.support_ticket_storage.list_by_assignee(
        current_user["user_id"], limit,
    )
    return [t.to_dict() for t in tickets]


# ============================================================
# Common routes (Task 16)
# ============================================================


def _require_ticket_access(ticket, current_user: dict) -> None:
    """Raise 403 if caller is neither requester nor a RuGPT Support operator."""
    is_requester = ticket.requester_user_id == current_user["user_id"]
    is_operator = current_user["org_id"] == Config.RUGPT_SUPPORT_ORG_ID
    if not (is_requester or is_operator):
        raise HTTPException(status_code=403, detail="Access denied")


@router.get("/tickets/{ticket_id}/chat")
async def get_ticket_chat(
    ticket_id: UUID,
    current_user: dict = Depends(get_current_user),
):
    """Resolve chat_id for a support ticket."""
    engine = get_engine_service()
    ticket = await engine.support_ticket_storage.get_by_id(ticket_id)
    if ticket is None:
        raise HTTPException(status_code=404, detail="Ticket not found")
    _require_ticket_access(ticket, current_user)
    chat = await engine.chat_storage.get_by_support_ticket(ticket_id)
    if chat is None:
        raise HTTPException(status_code=404, detail="Chat not found for ticket")
    return {"chat_id": str(chat.id)}


@router.get("/tickets/{ticket_id}/events")
async def get_ticket_events(
    ticket_id: UUID,
    limit: int = Query(200, ge=1, le=500),
    current_user: dict = Depends(get_current_user),
):
    """List audit-trail events for a support ticket (DESC by created_at)."""
    engine = get_engine_service()
    ticket = await engine.support_ticket_storage.get_by_id(ticket_id)
    if ticket is None:
        raise HTTPException(status_code=404, detail="Ticket not found")
    _require_ticket_access(ticket, current_user)
    events = await engine.support_ticket_event_storage.list_by_ticket(ticket_id, limit)
    return [e.to_dict() for e in events]
