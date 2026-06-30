"""
Task Management Routes

CRUD endpoints for employee tasks.
Item 9: ownership, prioritization, status transitions, deadline negotiation.
"""

from src.engine.unified_logger import get_logger
from datetime import datetime
from typing import Optional, List
from uuid import UUID

from fastapi import APIRouter, HTTPException, Depends, Query
from pydantic import BaseModel

from ..services.engine_service import get_engine_service
from ..services.task_service import ParticipantAlreadyExists
from .auth import get_current_user

logger = get_logger("routes")
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
    target_user_id: str

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

class MergePreviewRequest(BaseModel):
    source_task_ids: List[str]

class MergeApplyRequest(BaseModel):
    merge_request_id: str
    title: str
    description: Optional[str] = None
    assignee_user_id: str
    project_id: Optional[str] = None
    deadline: Optional[str] = None  # ISO 8601

def _serialize_entry(entry: dict) -> dict:
    """Serialize a {task, creator?, assignee?} entry to dict for API response."""
    out = entry["task"].to_dict()
    if entry.get("creator"):
        _cdept = entry["creator"].get("department_id")
        out["creator"] = {
            "id": str(entry["creator"]["id"]),
            "name": entry["creator"]["name"],
            "is_admin": entry["creator"]["is_admin"],
            "is_head": entry["creator"]["is_head"],
            "department_id": str(_cdept) if _cdept else None,
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


async def _can_manage_task(engine, current_user: dict, task) -> bool:
    """Whether the acting user may delete or merge this task.

    Allowed: org admin (org match is enforced separately by callers), the task
    creator, or the department head of the task creator's department. Shared by
    task deletion and task merge so both use the same rule.
    """
    # Org head — any task in their organization.
    if current_user.get("is_admin"):
        return True
    # Creator.
    if (
        task.created_by_user_id is not None
        and task.created_by_user_id == current_user["user_id"]
    ):
        return True
    # Department head of the task creator's department.
    if (
        current_user.get("is_head")
        and current_user.get("department_id") is not None
        and task.created_by_user_id is not None
    ):
        creator = await _load_user(engine, task.created_by_user_id)
        if creator is not None and creator.department_id == current_user["department_id"]:
            return True
    return False

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

# ============================================
# Merge (declared before /{task_id} so static paths win)
# ============================================

async def _load_mergeable_sources(engine, raw_ids: List[str], current_user: dict) -> list:
    """Resolve + authorize source tasks for a merge. Each must exist, be in the
    actor's org and pass _can_manage_task (creator / dept head / org admin)."""
    ids: List[UUID] = []
    seen = set()
    for raw in raw_ids:
        try:
            tid = UUID(raw)
        except (ValueError, TypeError):
            raise HTTPException(status_code=400, detail=f"Invalid task id: {raw}")
        if tid not in seen:
            seen.add(tid)
            ids.append(tid)
    if len(ids) < 2:
        raise HTTPException(status_code=400, detail="Need at least two distinct tasks to merge")

    tasks = []
    for tid in ids:
        task = await engine.task_service.get(tid)
        if task is None:
            raise HTTPException(status_code=404, detail=f"Task not found: {tid}")
        if task.org_id != current_user["org_id"]:
            raise HTTPException(status_code=403, detail="Access denied")
        if not await _can_manage_task(engine, current_user, task):
            raise HTTPException(status_code=403, detail=f"Not allowed to merge task {tid}")
        tasks.append(task)
    return tasks


@router.post("/merge/preview")
async def merge_preview(
    request: MergePreviewRequest,
    current_user: dict = Depends(get_current_user),
):
    """Formulate a merged task from >=2 source tasks via the task_merger agent.
    No mutation — persists a preview (+snapshot) the human verifies before apply."""
    engine = get_engine_service()
    source_tasks = await _load_mergeable_sources(engine, request.source_task_ids, current_user)
    actor = await _load_user(engine, current_user["user_id"])
    if actor is None:
        raise HTTPException(status_code=401, detail="User not found")
    try:
        req = await engine.task_merge_service.preview(actor, source_tasks)
    except Exception as e:
        logger.error(f"merge preview failed: {e}", exc_info=True)
        raise HTTPException(status_code=502, detail="Merge preview failed")
    return {
        "merge_request_id": str(req.id),
        "source_task_ids": [str(t) for t in req.source_task_ids],
        "proposed_title": req.proposed_title,
        "proposed_description": req.proposed_description,
        "proposed_summary": req.proposed_summary,
    }


@router.post("/merge/apply")
async def merge_apply(
    request: MergeApplyRequest,
    current_user: dict = Depends(get_current_user),
):
    """Apply a previewed merge: create the new task and close the sources.
    Deterministic — no LLM. Re-authorizes on every source task."""
    engine = get_engine_service()
    try:
        mr_id = UUID(request.merge_request_id)
    except (ValueError, TypeError):
        raise HTTPException(status_code=400, detail="Invalid merge_request_id")

    merge_request = await engine.task_merge_request_storage.get(mr_id)
    if merge_request is None:
        raise HTTPException(status_code=404, detail="Merge request not found")
    if merge_request.org_id != current_user["org_id"]:
        raise HTTPException(status_code=403, detail="Access denied")
    if merge_request.status != "previewed":
        # Already applied or previously failed — never re-run a non-pending
        # request (would create a duplicate merged task from sources a prior
        # partial run may have closed).
        raise HTTPException(status_code=409, detail=f"Merge request is '{merge_request.status}', not pending")

    # Re-authorize against the live source tasks.
    await _load_mergeable_sources(
        engine, [str(t) for t in merge_request.source_task_ids], current_user
    )

    actor = await _load_user(engine, current_user["user_id"])
    if actor is None:
        raise HTTPException(status_code=401, detail="User not found")

    if not request.title.strip():
        raise HTTPException(status_code=400, detail="Title required")
    try:
        assignee_uuid = UUID(request.assignee_user_id)
    except (ValueError, TypeError):
        raise HTTPException(status_code=400, detail="Invalid assignee_user_id")
    project_uuid = None
    if request.project_id:
        try:
            project_uuid = UUID(request.project_id)
        except (ValueError, TypeError):
            raise HTTPException(status_code=400, detail="Invalid project_id")
    deadline = None
    if request.deadline:
        try:
            deadline = datetime.fromisoformat(request.deadline.replace("Z", "+00:00"))
        except (ValueError, TypeError):
            raise HTTPException(status_code=400, detail="Invalid deadline")

    if not await engine.department_service.check_visible(
        current_user["user_id"], assignee_uuid, current_user["org_id"],
    ):
        raise HTTPException(status_code=403, detail="Assignee not visible")

    try:
        new_task = await engine.task_merge_service.apply(
            actor,
            merge_request,
            title=request.title.strip(),
            description=request.description,
            assignee_user_id=assignee_uuid,
            project_id=project_uuid,
            deadline=deadline,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return new_task.to_dict()


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
    """Soft-delete a task. Allowed for the creator, the department head of the
    creator's department, or an org admin."""
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
    if not await _can_manage_task(engine, current_user, task):
        raise HTTPException(status_code=403, detail="Not allowed to delete this task")

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
        user_uuid = UUID(request.target_user_id)
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

@router.delete("/{task_id}/participants/{target_user_id}", status_code=204)
async def remove_participant(
    task_id: str,
    target_user_id: str,
    current_user: dict = Depends(get_current_user),
):
    """Remove a participant. 204 on success. 404 if not present. 403 on perms."""
    engine = get_engine_service()
    try:
        task_uuid = UUID(task_id)
        user_uuid = UUID(target_user_id)
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
