"""
Task Management Routes

CRUD endpoints for employee tasks.
Managers see all org tasks, employees see only their own.
"""
import logging
from datetime import datetime
from typing import Optional, List
from uuid import UUID

from fastapi import APIRouter, HTTPException, Depends, Query
from pydantic import BaseModel

from ..services.engine_service import get_engine_service
from .auth import get_current_user

logger = logging.getLogger("rugpt.routes.tasks")
router = APIRouter(prefix="/tasks", tags=["tasks"])


class CreateTaskRequest(BaseModel):
    title: str
    description: Optional[str] = None
    assignee_user_id: str
    deadline: Optional[str] = None  # ISO 8601


class UpdateTaskRequest(BaseModel):
    title: Optional[str] = None
    description: Optional[str] = None
    status: Optional[str] = None
    assignee_user_id: Optional[str] = None
    deadline: Optional[str] = None


class TaskResponse(BaseModel):
    id: str
    org_id: str
    title: str
    description: Optional[str]
    status: str
    assignee_user_id: str
    deadline: Optional[str]
    is_active: bool
    created_at: str
    updated_at: str


@router.get("", response_model=List[TaskResponse])
async def list_tasks(
    status: Optional[str] = Query(None),
    assignee_user_id: Optional[str] = Query(None),
    current_user: dict = Depends(get_current_user),
):
    """
    List tasks. Admins see all org tasks, employees see only their own.
    Optional filters: status, assignee_user_id.
    """
    engine = get_engine_service()
    org_id = current_user["org_id"]

    try:
        if assignee_user_id:
            assignee_uuid = UUID(assignee_user_id)
            # Non-admin can only see their own tasks
            if not current_user.get("is_admin") and assignee_uuid != current_user["user_id"]:
                raise HTTPException(status_code=403, detail="Access denied")
            tasks = await engine.task_service.list_by_assignee(assignee_uuid, status)
        elif current_user.get("is_admin"):
            tasks = await engine.task_service.list_by_org(org_id, status)
        else:
            tasks = await engine.task_service.list_by_assignee(current_user["user_id"], status)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    return [TaskResponse(**t.to_dict()) for t in tasks]


@router.post("", response_model=TaskResponse)
async def create_task(
    request: CreateTaskRequest,
    current_user: dict = Depends(get_current_user),
):
    """Create a new task (typically by a manager)"""
    engine = get_engine_service()

    try:
        assignee_uuid = UUID(request.assignee_user_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid assignee_user_id")

    deadline = None
    if request.deadline:
        try:
            deadline = datetime.fromisoformat(request.deadline)
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid deadline format (use ISO 8601)")

    try:
        task = await engine.task_service.create(
            org_id=current_user["org_id"],
            title=request.title,
            description=request.description,
            assignee_user_id=assignee_uuid,
            deadline=deadline,
        )
        return TaskResponse(**task.to_dict())
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.get("/{task_id}", response_model=TaskResponse)
async def get_task(task_id: str, current_user: dict = Depends(get_current_user)):
    """Get a single task by ID"""
    engine = get_engine_service()
    try:
        task_uuid = UUID(task_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid task ID")

    task = await engine.task_service.get(task_uuid)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    if task.org_id != current_user["org_id"]:
        raise HTTPException(status_code=403, detail="Access denied")
    # Non-admin can only see their own tasks
    if not current_user.get("is_admin") and task.assignee_user_id != current_user["user_id"]:
        raise HTTPException(status_code=403, detail="Access denied")

    return TaskResponse(**task.to_dict())


@router.patch("/{task_id}", response_model=TaskResponse)
async def update_task(
    task_id: str,
    request: UpdateTaskRequest,
    current_user: dict = Depends(get_current_user),
):
    """Update a task"""
    engine = get_engine_service()
    try:
        task_uuid = UUID(task_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid task ID")

    task = await engine.task_service.get(task_uuid)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    if task.org_id != current_user["org_id"]:
        raise HTTPException(status_code=403, detail="Access denied")

    assignee_uuid = None
    if request.assignee_user_id:
        try:
            assignee_uuid = UUID(request.assignee_user_id)
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid assignee_user_id")

    deadline = None
    if request.deadline:
        try:
            deadline = datetime.fromisoformat(request.deadline)
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid deadline format")

    try:
        updated = await engine.task_service.update(
            task_id=task_uuid,
            title=request.title,
            description=request.description,
            status=request.status,
            assignee_user_id=assignee_uuid,
            deadline=deadline,
        )
        return TaskResponse(**updated.to_dict())
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.delete("/{task_id}")
async def deactivate_task(task_id: str, current_user: dict = Depends(get_current_user)):
    """Soft-delete a task"""
    engine = get_engine_service()
    try:
        task_uuid = UUID(task_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid task ID")

    task = await engine.task_service.get(task_uuid)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    if task.org_id != current_user["org_id"]:
        raise HTTPException(status_code=403, detail="Access denied")

    await engine.task_service.deactivate(task_uuid)
    return {"success": True, "message": "Task deactivated"}
