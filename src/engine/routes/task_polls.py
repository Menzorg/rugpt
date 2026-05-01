"""
Task Polls Routes

Endpoints for daily morning polls (dialog-driven):
- GET /task-polls/today — get today's poll for current user
- GET /task-polls/today/chat — resolve chat_id of today's poll for the dialog UI
- GET /task-polls — list polls (history); pending-only by default
- GET /task-polls/{poll_id} — get a specific poll
- POST /task-polls/{poll_id}/submit — finish dialog: enqueue AI summary, return 202

Submission no longer takes a `responses[]` body — task statuses are derived by the
poll-summary AI from the chat dialog. Completion is signalled to the frontend
asynchronously via the chat.events WS broadcast (Kafka -> NestJS).
"""
import logging
from datetime import date as _date
from typing import Optional, List
from uuid import UUID

from fastapi import APIRouter, HTTPException, Depends, Query, Response
from pydantic import BaseModel

from ..config import Config
from ..services.engine_service import get_engine_service
from .auth import get_current_user

logger = logging.getLogger("rugpt.routes.task_polls")
router = APIRouter(prefix="/task-polls", tags=["task-polls"])


# poll_interviewer_ai system user lives in the RuGPT system org (migration 029).
_POLL_INTERVIEWER_USERNAME = "poll_interviewer_ai"


class TaskPollResponse(BaseModel):
    id: str
    org_id: str
    assignee_user_id: str
    poll_date: str
    status: str
    responses: list
    created_at: str
    completed_at: Optional[str]
    expires_at: Optional[str]


class TodayPollChatResponse(BaseModel):
    chat_id: str
    poll_id: str
    status: str


@router.get("/today", response_model=Optional[TaskPollResponse])
async def get_today_poll(current_user: dict = Depends(get_current_user)):
    """Get today's poll for the current user (if exists)"""
    engine = get_engine_service()
    poll = await engine.task_poll_service.get_today_poll(current_user["user_id"])
    if not poll:
        return None
    return TaskPollResponse(**poll.to_dict())


@router.get("/today/chat", response_model=TodayPollChatResponse)
async def get_today_chat(current_user: dict = Depends(get_current_user)):
    """Resolve chat_id for today's active poll for the current user.

    Returns 404 if no poll exists for today, or if the poll has no chat
    (data integrity issue — poll predates dialog migration). Used by the
    frontend PollChat component (Task 16) to mount onto the right chat.
    """
    engine = get_engine_service()
    user_id = current_user["user_id"]

    poll = await engine.task_poll_service.storage.get_by_user_and_date(
        user_id, _date.today(),
    )
    if poll is None:
        raise HTTPException(status_code=404, detail="No poll for today")

    chat = await engine.chat_storage.get_by_poll_id(poll.id)
    if chat is None:
        # Data integrity issue: poll exists but its chat doesn't. Distinct from
        # the routine "no poll for today" empty state above — this is operator
        # alert territory.
        logger.error(
            f"get_today_chat: poll {poll.id} exists but no chat — "
            f"poll creation likely failed mid-flight (Task 12 retry should fix)"
        )
        raise HTTPException(
            status_code=500,
            detail="Poll chat missing — data integrity issue",
        )

    return TodayPollChatResponse(
        chat_id=str(chat.id),
        poll_id=str(poll.id),
        status=poll.status,
    )


@router.get("", response_model=List[TaskPollResponse])
async def list_polls(
    include_completed: bool = Query(False),
    limit: int = Query(30, ge=1, le=100),
    current_user: dict = Depends(get_current_user),
):
    """List polls for the current user.

    `include_completed=False` (default) returns only pending polls — keeps the
    existing client behaviour intact. History UIs (Task 17) pass `True` to see
    completed/expired entries.
    """
    engine = get_engine_service()
    polls = await engine.task_poll_service.list_by_user(current_user["user_id"], limit)
    if not include_completed:
        polls = [p for p in polls if p.status == "pending"]
    return [TaskPollResponse(**p.to_dict()) for p in polls]


@router.get("/{poll_id}", response_model=TaskPollResponse)
async def get_poll(poll_id: str, current_user: dict = Depends(get_current_user)):
    """Get a specific poll by ID"""
    engine = get_engine_service()
    try:
        poll_uuid = UUID(poll_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid poll ID")

    poll = await engine.task_poll_service.get(poll_uuid)
    if not poll:
        raise HTTPException(status_code=404, detail="Poll not found")
    if poll.assignee_user_id != current_user["user_id"]:
        raise HTTPException(status_code=403, detail="Access denied")

    return TaskPollResponse(**poll.to_dict())


@router.post("/{poll_id}/submit")
async def submit_poll(
    poll_id: str,
    current_user: dict = Depends(get_current_user),
):
    """Submit poll: enqueue summary generation via Kafka. Returns 202 Accepted.

    The actual summary is generated asynchronously by AgentRequestHandler
    (kind='poll_summary'); completion is signalled to the frontend via the
    chat.events WS broadcast.

    Validation flow:
      - 404 if poll missing
      - 403 if caller is not the assignee
      - 409 if poll is not in `pending` state (idempotency: re-submit no-op)
      - 409 if no user message from assignee exists in the chat yet
      - 500 on data-integrity issues (missing chat / missing system user)
      - 503 if Kafka publish fails (frontend may retry)
    """
    engine = get_engine_service()
    try:
        poll_uuid = UUID(poll_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid poll ID")

    user_id = current_user["user_id"]

    poll = await engine.task_poll_service.storage.get_by_id(poll_uuid)
    if poll is None:
        raise HTTPException(status_code=404, detail="Poll not found")
    if poll.assignee_user_id != user_id:
        raise HTTPException(status_code=403, detail="Not your poll")
    if poll.status != "pending":
        raise HTTPException(status_code=409, detail=f"Poll already {poll.status}")

    # Find chat for this poll
    chat = await engine.chat_storage.get_by_poll_id(poll_uuid)
    if chat is None:
        raise HTTPException(
            status_code=500,
            detail="Poll chat not found — did poll creation succeed?",
        )

    # Validate at least one user message from assignee in chat (защита от пустого
    # submit — Task 5 на фронте кнопка disabled, но бэк защищает дополнительно).
    has_user_msg = await engine.message_storage.messages_exist_from_sender(
        chat.id, user_id,
    )
    if not has_user_msg:
        raise HTTPException(
            status_code=409,
            detail="Сначала ответьте AI о ваших задачах",
        )

    # Resolve poll_interviewer_ai (responder for system 'Отчёт сдан' message
    # + agent_run trace).
    interviewer = await engine.user_storage.get_by_username(
        _POLL_INTERVIEWER_USERNAME,
        Config.SYSTEM_ORG_ID,
    )
    if interviewer is None:
        raise HTTPException(
            status_code=500,
            detail="poll_interviewer_ai not found (migration 029)",
        )

    try:
        request_id = await engine.ai_service.enqueue_poll_summary(
            poll_id=poll_uuid,
            chat_id=chat.id,
            responder_id=interviewer.id,
        )
    except Exception as e:
        logger.error(
            f"Failed to enqueue poll_summary for poll={poll_uuid}: {e}",
            exc_info=True,
        )
        raise HTTPException(status_code=503, detail="Service temporarily unavailable")

    logger.info(
        f"submit_poll: enqueued summary poll={poll_uuid} chat={chat.id} "
        f"request_id={request_id} user={user_id}"
    )
    return Response(status_code=202)
