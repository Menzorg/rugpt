"""
Task Polls Routes

Endpoints for daily morning polls:
- GET /task-polls/today — get today's poll for current user
- GET /task-polls — list polls history
- GET /task-polls/{poll_id} — get a specific poll
- POST /task-polls/{poll_id}/submit — submit responses
"""
import logging
from datetime import date
from typing import Optional, List
from uuid import UUID

from fastapi import APIRouter, HTTPException, Depends, Query
from pydantic import BaseModel

from ..services.engine_service import get_engine_service
from .auth import get_current_user

logger = logging.getLogger("rugpt.routes.task_polls")
router = APIRouter(prefix="/task-polls", tags=["task-polls"])


class PollResponseItem(BaseModel):
    task_id: str
    new_status: Optional[str] = None       # created | in_progress | done
    comment: Optional[str] = None


class SubmitPollRequest(BaseModel):
    responses: List[PollResponseItem]


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


@router.get("/today", response_model=Optional[TaskPollResponse])
async def get_today_poll(current_user: dict = Depends(get_current_user)):
    """Get today's poll for the current user (if exists)"""
    engine = get_engine_service()
    poll = await engine.task_poll_service.get_today_poll(current_user["user_id"])
    if not poll:
        return None
    return TaskPollResponse(**poll.to_dict())


@router.get("", response_model=List[TaskPollResponse])
async def list_polls(
    limit: int = Query(30, ge=1, le=100),
    current_user: dict = Depends(get_current_user),
):
    """List polls for the current user"""
    engine = get_engine_service()
    polls = await engine.task_poll_service.list_by_user(current_user["user_id"], limit)
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


@router.post("/{poll_id}/submit", response_model=TaskPollResponse)
async def submit_poll(
    poll_id: str,
    request: SubmitPollRequest,
    current_user: dict = Depends(get_current_user),
):
    """Submit responses to a poll"""
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

    responses = [r.model_dump() for r in request.responses]

    try:
        updated = await engine.task_poll_service.submit_responses(poll_uuid, responses)
        return TaskPollResponse(**updated.to_dict())
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
