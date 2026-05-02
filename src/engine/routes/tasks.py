"""
Task Management Routes

CRUD endpoints for employee tasks.
Item 9: ownership, prioritization, status transitions, deadline negotiation.
"""
import logging
from datetime import datetime
from typing import Optional
from uuid import UUID

from fastapi import APIRouter, HTTPException, Depends, Query
from pydantic import BaseModel

from ..services.engine_service import get_engine_service
from ..services.task_service import ParticipantAlreadyExists
from .auth import get_current_user

logger = logging.getLogger("rugpt.routes.tasks")
router = APIRouter(prefix="/tasks", tags=["tasks"])


class CreateTaskRequest(BaseModel):
    title: str
    description: Optional[str] = None
    assignee_user_id: str
    deadline: Optional[str] = None  # ISO 8601
    project_id: Optional[str] = None
    priority: Optional[int] = None  # 1..3; default derived from creator's role
    participant_user_ids: Optional[list[str]] = None


class AddParticipantRequest(BaseModel):
    user_id: str


class UpdateTaskRequest(BaseModel):
    title: Optional[str] = None
    description: Optional[str] = None
    assignee_user_id: Optional[str] = None
    deadline: Optional[str] = None
    # Use a dict wrapper sentinel so None can explicitly detach while omission means "no change".
    # Pydantic v1/v2 both accept missing keys as None -- to distinguish, we use a marker field.
    project_id: Optional[str] = None
    detach_project: bool = False
    priority: Optional[int] = None  # 1..3


class DeadlineRequest(BaseModel):
    deadline: str  # ISO 8601


class ProposeDeadlineRequest(BaseModel):
    proposed_deadline: str  # ISO 8601


class RejectRequest(BaseModel):
    comment: Optional[str] = None


def _serialize_entry(entry: dict) -> dict:
    """Serialize a {task, creator?, assignee?} entry to dict for API response."""
    out = entry["task"].to_dict()
    if entry.get("creator"):
        out["creator"] = {
            "id": str(entry["creator"]["id"]),
            "name": entry["creator"]["name"],
            "is_admin": entry["creator"]["is_admin"],
            "is_head": entry["creator"]["is_head"],
        }
    if entry.get("assignee"):
        out["assignee"] = {
            "id": str(entry["assignee"]["id"]),
            "name": entry["assignee"]["name"],
            "is_head": entry["assignee"]["is_head"],
        }
    out["participants"] = [
        {"id": str(p["id"]), "name": p["name"]} for p in entry.get("participants") or []
    ]
    return out


async def _load_user(engine, user_id: UUID):
    """Helper: load user object from storage (for permission checks)."""
    return await engine.user_storage.get_by_id(user_id)


async def _hydrate_participants(engine, entries: list) -> list:
    """Bulk-fetch active participants for entries and attach as entry['participants']."""
    if not entries:
        return entries
    task_ids = [e["task"].id for e in entries]
    by_task = await engine.task_participant_storage.get_for_tasks(task_ids)
    for e in entries:
        e["participants"] = by_task.get(e["task"].id, [])
    return entries


# ============================================
# Lists
# ============================================

@router.get("")
async def list_tasks(
    status: Optional[str] = Query(None),
    assignee_user_id: Optional[str] = Query(None),
    current_user: dict = Depends(get_current_user),
):
    """
    Legacy general list. Admins see all org tasks, employees see only their own.
    Use /tasks/my and /tasks/created-by-me for prioritized lists.
    """
    engine = get_engine_service()
    org_id = current_user["org_id"]

    try:
        if assignee_user_id:
            assignee_uuid = UUID(assignee_user_id)
            if not current_user.get("is_admin") and assignee_uuid != current_user["user_id"]:
                raise HTTPException(status_code=403, detail="Access denied")
            tasks = await engine.task_service.list_by_assignee(assignee_uuid, status)
        elif current_user.get("is_admin"):
            tasks = await engine.task_service.list_by_org(org_id, status)
        else:
            tasks = await engine.task_service.list_by_assignee(current_user["user_id"], status)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    if current_user.get("is_head") and not current_user.get("is_admin"):
        visible_ids = await engine.department_service.get_visible_user_ids(
            current_user["user_id"], current_user["org_id"],
        )
        tasks = [t for t in tasks if t.assignee_user_id in visible_ids]

    return [t.to_dict() for t in tasks]


@router.get("/my")
async def list_my_tasks(
    project_id: Optional[str] = Query(None),
    current_user: dict = Depends(get_current_user),
):
    """Tasks where current user is assignee, status != done.
    Sorted by priority + deadline. Optional ?project_id filter."""
    engine = get_engine_service()
    entries = await engine.task_service.list_my_tasks(
        current_user["user_id"], include_done=False,
    )
    if project_id:
        try:
            pid = UUID(project_id)
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid project_id")
        entries = [e for e in entries if e["task"].project_id == pid]
    entries = await _hydrate_participants(engine, entries)
    return [_serialize_entry(e) for e in entries]


@router.get("/created-by-me")
async def list_created_by_me(
    project_id: Optional[str] = Query(None),
    current_user: dict = Depends(get_current_user),
):
    """Tasks where current user is creator, status != done.
    Optional ?project_id filter."""
    engine = get_engine_service()
    entries = await engine.task_service.list_tasks_created_by(
        current_user["user_id"], include_done=False,
    )
    if project_id:
        try:
            pid = UUID(project_id)
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid project_id")
        entries = [e for e in entries if e["task"].project_id == pid]
    entries = await _hydrate_participants(engine, entries)
    return [_serialize_entry(e) for e in entries]


@router.get("/archive")
async def list_archive(
    limit: int = Query(200, ge=1, le=1000),
    current_user: dict = Depends(get_current_user),
):
    """Archived tasks for current user: done or cancelled (is_active=false),
    where the user was creator or assignee. Used by the '/tasks -> Архив' tab
    and direct URLs to closed tasks/chats."""
    engine = get_engine_service()
    entries = await engine.task_service.list_archived(
        current_user["user_id"], current_user["org_id"], limit,
    )
    entries = await _hydrate_participants(engine, entries)
    return [_serialize_entry(e) for e in entries]


@router.get("/participating")
async def list_participating(
    project_id: Optional[str] = Query(None),
    current_user: dict = Depends(get_current_user),
):
    """Tasks where current user is in task_participants, status != done."""
    engine = get_engine_service()
    entries = await engine.task_service.list_participating(
        current_user["user_id"], include_done=False,
    )
    if project_id:
        try:
            pid = UUID(project_id)
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid project_id")
        entries = [e for e in entries if e["task"].project_id == pid]
    entries = await _hydrate_participants(engine, entries)
    return [_serialize_entry(e) for e in entries]


@router.get("/done")
async def list_done(current_user: dict = Depends(get_current_user)):
    """All status='done' tasks where user is creator OR assignee OR participant."""
    engine = get_engine_service()
    entries = await engine.task_service.list_done(current_user["user_id"])
    entries = await _hydrate_participants(engine, entries)
    return [_serialize_entry(e) for e in entries]


# ============================================
# Create / Get / Update / Delete
# ============================================

@router.post("")
async def create_task(
    request: CreateTaskRequest,
    current_user: dict = Depends(get_current_user),
):
    """Create a new task. created_by_user_id auto-set to current user."""
    engine = get_engine_service()

    try:
        assignee_uuid = UUID(request.assignee_user_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid assignee_user_id")

    if not await engine.department_service.check_visible(
        current_user["user_id"], assignee_uuid, current_user["org_id"],
    ):
        raise HTTPException(status_code=403, detail="Assignee not visible")

    deadline = None
    if request.deadline:
        try:
            deadline = datetime.fromisoformat(request.deadline)
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid deadline format (use ISO 8601)")

    project_uuid = None
    if request.project_id:
        try:
            project_uuid = UUID(request.project_id)
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid project_id")

    participant_uuids: Optional[list] = None
    if request.participant_user_ids:
        try:
            participant_uuids = [UUID(p) for p in request.participant_user_ids]
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid participant_user_ids")

    if request.priority is not None and request.priority not in (1, 2, 3):
        raise HTTPException(status_code=400, detail="priority must be 1, 2 or 3")

    try:
        task = await engine.task_service.create(
            org_id=current_user["org_id"],
            title=request.title,
            description=request.description,
            assignee_user_id=assignee_uuid,
            deadline=deadline,
            created_by_user_id=current_user["user_id"],
            project_id=project_uuid,
            priority=request.priority,
            participant_user_ids=participant_uuids,
        )
        return task.to_dict()
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.get("/{task_id}")
async def get_task(task_id: str, current_user: dict = Depends(get_current_user)):
    """Get a single task by ID. Visible to creator, assignee, participants, or admin."""
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

    user = await _load_user(engine, current_user["user_id"])
    if user is None or not await _can_see_task_async(task, user, engine):
        raise HTTPException(status_code=403, detail="Access denied")

    out = task.to_dict()
    out["participants"] = [
        {"id": str(p["id"]), "name": p["name"]}
        for p in await engine.task_participant_storage.list_active_user_dicts(task_uuid)
    ]
    return out


@router.patch("/{task_id}")
async def update_task(
    task_id: str,
    request: UpdateTaskRequest,
    current_user: dict = Depends(get_current_user),
):
    """Update task fields (title/description/assignee/deadline). Status changes go through /take, /mark-done, /accept, /reject."""
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

    # Only the creator can edit a task. Admins do not get an override —
    # editing other people's tasks isn't part of the admin role.
    # Legacy tasks without a creator are not editable (status transitions still work).
    if task.created_by_user_id is None:
        raise HTTPException(status_code=403, detail="Legacy task without creator cannot be edited")
    if task.created_by_user_id != current_user["user_id"]:
        raise HTTPException(status_code=403, detail="Only creator can update task")

    # Lock editing on terminal statuses — done/cancelled tasks are historical.
    if task.status in ("done", "cancelled"):
        raise HTTPException(
            status_code=409,
            detail=f"Cannot edit task with status '{task.status}'",
        )

    assignee_uuid = None
    if request.assignee_user_id:
        try:
            assignee_uuid = UUID(request.assignee_user_id)
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid assignee_user_id")
        if not await engine.department_service.check_visible(
            current_user["user_id"], assignee_uuid, current_user["org_id"],
        ):
            raise HTTPException(status_code=403, detail="Assignee not visible")

    deadline = None
    if request.deadline:
        try:
            deadline = datetime.fromisoformat(request.deadline)
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid deadline format")

    # project_id semantics: detach_project=true -> None (detach);
    # project_id set -> reassign; both unset -> no change.
    from ..services.task_service import TaskService
    project_kwarg = TaskService._UNSET
    if request.detach_project:
        project_kwarg = None
    elif request.project_id:
        try:
            project_kwarg = UUID(request.project_id)
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid project_id")

    if request.priority is not None and request.priority not in (1, 2, 3):
        raise HTTPException(status_code=400, detail="priority must be 1, 2 or 3")

    try:
        updated = await engine.task_service.update(
            task_id=task_uuid,
            title=request.title,
            description=request.description,
            assignee_user_id=assignee_uuid,
            deadline=deadline,
            project_id=project_kwarg,
            priority=request.priority,
            actor_user_id=current_user["user_id"],
        )
        return updated.to_dict()
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.delete("/{task_id}")
async def deactivate_task(task_id: str, current_user: dict = Depends(get_current_user)):
    """Soft-delete a task (only creator)."""
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
    if task.created_by_user_id is None:
        raise HTTPException(status_code=403, detail="Legacy task without creator cannot be deleted")
    if task.created_by_user_id != current_user["user_id"]:
        raise HTTPException(status_code=403, detail="Only creator can delete task")

    user = await _load_user(engine, current_user["user_id"])
    await engine.task_service.deactivate(task_uuid, user=user)
    return {"success": True, "message": "Task deactivated"}


# ============================================
# Status transitions
# ============================================

@router.post("/{task_id}/take")
async def take_task(task_id: str, current_user: dict = Depends(get_current_user)):
    """Assignee takes task: created → in_progress."""
    engine = get_engine_service()
    try:
        task_uuid = UUID(task_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid task ID")

    user = await _load_user(engine, current_user["user_id"])
    if not user:
        raise HTTPException(status_code=401, detail="User not found")

    try:
        task = await engine.task_service.take_task(task_uuid, user)
        return task.to_dict()
    except PermissionError as e:
        raise HTTPException(status_code=403, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/{task_id}/mark-done")
async def mark_done(task_id: str, current_user: dict = Depends(get_current_user)):
    """Assignee marks task done: in_progress → awaiting_review."""
    engine = get_engine_service()
    try:
        task_uuid = UUID(task_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid task ID")

    user = await _load_user(engine, current_user["user_id"])
    if not user:
        raise HTTPException(status_code=401, detail="User not found")

    try:
        task = await engine.task_service.mark_done(task_uuid, user)
        return task.to_dict()
    except PermissionError as e:
        raise HTTPException(status_code=403, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/{task_id}/accept")
async def accept_task(task_id: str, current_user: dict = Depends(get_current_user)):
    """Creator accepts: awaiting_review → done."""
    engine = get_engine_service()
    try:
        task_uuid = UUID(task_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid task ID")

    user = await _load_user(engine, current_user["user_id"])
    if not user:
        raise HTTPException(status_code=401, detail="User not found")

    try:
        task = await engine.task_service.accept_task(task_uuid, user)
        return task.to_dict()
    except PermissionError as e:
        raise HTTPException(status_code=403, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/{task_id}/reject")
async def reject_task(
    task_id: str,
    request: RejectRequest,
    current_user: dict = Depends(get_current_user),
):
    """Creator rejects: awaiting_review → in_progress. Comment optional, logged only."""
    engine = get_engine_service()
    try:
        task_uuid = UUID(task_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid task ID")

    user = await _load_user(engine, current_user["user_id"])
    if not user:
        raise HTTPException(status_code=401, detail="User not found")

    try:
        task = await engine.task_service.reject_task(task_uuid, user, comment=request.comment)
        return task.to_dict()
    except PermissionError as e:
        raise HTTPException(status_code=403, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


# ============================================
# Deadline
# ============================================

@router.patch("/{task_id}/deadline")
async def set_deadline(
    task_id: str,
    request: DeadlineRequest,
    current_user: dict = Depends(get_current_user),
):
    """Creator sets deadline directly."""
    engine = get_engine_service()
    try:
        task_uuid = UUID(task_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid task ID")
    try:
        deadline = datetime.fromisoformat(request.deadline)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid deadline format (use ISO 8601)")

    user = await _load_user(engine, current_user["user_id"])
    if not user:
        raise HTTPException(status_code=401, detail="User not found")

    try:
        task = await engine.task_service.set_deadline(task_uuid, user, deadline)
        return task.to_dict()
    except PermissionError as e:
        raise HTTPException(status_code=403, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/{task_id}/deadline-proposal")
async def propose_deadline(
    task_id: str,
    request: ProposeDeadlineRequest,
    current_user: dict = Depends(get_current_user),
):
    """Assignee proposes alternative deadline."""
    engine = get_engine_service()
    try:
        task_uuid = UUID(task_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid task ID")
    try:
        proposed = datetime.fromisoformat(request.proposed_deadline)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid proposed_deadline format (use ISO 8601)")

    user = await _load_user(engine, current_user["user_id"])
    if not user:
        raise HTTPException(status_code=401, detail="User not found")

    try:
        task = await engine.task_service.propose_deadline(task_uuid, user, proposed)
        return task.to_dict()
    except PermissionError as e:
        raise HTTPException(status_code=403, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/{task_id}/deadline-proposal/accept")
async def accept_proposed_deadline(task_id: str, current_user: dict = Depends(get_current_user)):
    """Creator accepts proposed deadline."""
    engine = get_engine_service()
    try:
        task_uuid = UUID(task_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid task ID")

    user = await _load_user(engine, current_user["user_id"])
    if not user:
        raise HTTPException(status_code=401, detail="User not found")

    try:
        task = await engine.task_service.accept_proposed_deadline(task_uuid, user)
        return task.to_dict()
    except PermissionError as e:
        raise HTTPException(status_code=403, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/{task_id}/deadline-proposal/reject")
async def reject_proposed_deadline(task_id: str, current_user: dict = Depends(get_current_user)):
    """Creator rejects proposed deadline."""
    engine = get_engine_service()
    try:
        task_uuid = UUID(task_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid task ID")

    user = await _load_user(engine, current_user["user_id"])
    if not user:
        raise HTTPException(status_code=401, detail="User not found")

    try:
        task = await engine.task_service.reject_proposed_deadline(task_uuid, user)
        return task.to_dict()
    except PermissionError as e:
        raise HTTPException(status_code=403, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


# ============================================
# Task chat & events (item 11)
# ============================================

async def _can_see_task_async(task, user, engine) -> bool:
    """Strict visibility: creator, current assignee, participant, or admin of same org."""
    if task.org_id != user.org_id and not user.is_admin:
        return False
    if user.id == task.created_by_user_id:
        return True
    if user.id == task.assignee_user_id:
        return True
    if user.is_admin and task.org_id == user.org_id:
        return True
    parts = await engine.task_participant_storage.list_user_ids(task.id)
    if user.id in parts:
        return True
    return False


@router.get("/{task_id}/chat")
async def get_task_chat(task_id: str, current_user: dict = Depends(get_current_user)):
    """Resolve a task's chat. 404 if task/chat missing or actor outside can_see_task."""
    engine = get_engine_service()
    try:
        task_uuid = UUID(task_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid task ID")

    task = await engine.task_service.get(task_uuid)
    if task is None:
        raise HTTPException(status_code=404, detail="Task not found")

    user = await _load_user(engine, current_user["user_id"])
    if user is None:
        raise HTTPException(status_code=401, detail="User not found")
    if not await _can_see_task_async(task, user, engine):
        raise HTTPException(status_code=404, detail="Task not found")

    chat = await engine.chat_service.get_task_chat(task_uuid)
    if chat is None:
        raise HTTPException(status_code=404, detail="Task chat not found")
    return chat.to_dict()


@router.get("/{task_id}/events")
async def list_task_events(
    task_id: str,
    limit: int = Query(100, ge=1, le=500),
    current_user: dict = Depends(get_current_user),
):
    """Audit trail for a task (item 11)."""
    engine = get_engine_service()
    try:
        task_uuid = UUID(task_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid task ID")

    task = await engine.task_service.get(task_uuid)
    if task is None:
        raise HTTPException(status_code=404, detail="Task not found")

    user = await _load_user(engine, current_user["user_id"])
    if user is None:
        raise HTTPException(status_code=401, detail="User not found")
    if not await _can_see_task_async(task, user, engine):
        raise HTTPException(status_code=404, detail="Task not found")

    events = await engine.task_event_service.list_for_task(task_uuid, limit)
    return [e.to_dict() for e in events]


# ============================================
# Participants
# ============================================

@router.post("/{task_id}/participants", status_code=201)
async def add_participant(
    task_id: str,
    request: AddParticipantRequest,
    current_user: dict = Depends(get_current_user),
):
    """Add a participant. 201 + {id, name}. 409 on duplicate. 403 on perms."""
    engine = get_engine_service()
    try:
        task_uuid = UUID(task_id)
        user_uuid = UUID(request.user_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid id")

    task = await engine.task_service.get(task_uuid)
    if task is None:
        raise HTTPException(status_code=404, detail="Task not found")
    if task.org_id != current_user["org_id"]:
        raise HTTPException(status_code=403, detail="Access denied")

    actor = await _load_user(engine, current_user["user_id"])
    if actor is None:
        raise HTTPException(status_code=401, detail="User not found")

    try:
        result = await engine.task_service.add_participant(task_uuid, user_uuid, actor)
    except PermissionError as e:
        raise HTTPException(status_code=403, detail=str(e))
    except ParticipantAlreadyExists as e:
        raise HTTPException(status_code=409, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    return {"id": str(result["id"]), "name": result["name"]}


@router.delete("/{task_id}/participants/{user_id}", status_code=204)
async def remove_participant(
    task_id: str,
    user_id: str,
    current_user: dict = Depends(get_current_user),
):
    """Remove a participant. 204 on success. 404 if not present. 403 on perms."""
    engine = get_engine_service()
    try:
        task_uuid = UUID(task_id)
        user_uuid = UUID(user_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid id")

    task = await engine.task_service.get(task_uuid)
    if task is None:
        raise HTTPException(status_code=404, detail="Task not found")
    if task.org_id != current_user["org_id"]:
        raise HTTPException(status_code=403, detail="Access denied")

    actor = await _load_user(engine, current_user["user_id"])
    if actor is None:
        raise HTTPException(status_code=401, detail="User not found")

    try:
        ok = await engine.task_service.remove_participant(task_uuid, user_uuid, actor)
    except PermissionError as e:
        raise HTTPException(status_code=403, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    if not ok:
        raise HTTPException(status_code=404, detail="Participant not found")
    return None
