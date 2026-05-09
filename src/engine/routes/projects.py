"""
Project Routes

CRUD endpoints for projects + project chat resolution.
Item 11: projects group tasks; chat is lazily created on first linked task.
"""

from src.engine.unified_logger import get_logger
from typing import Optional
from uuid import UUID

from fastapi import APIRouter, HTTPException, Depends, Query
from pydantic import BaseModel

from ..services.engine_service import get_engine_service
from .auth import get_current_user

logger = get_logger("routes")
router = APIRouter(prefix="/projects", tags=["projects"])

class CreateProjectRequest(BaseModel):
    name: str
    description: Optional[str] = None

class UpdateProjectRequest(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None

async def _load_user(engine, user_id: UUID):
    return await engine.user_storage.get_by_id(user_id)

@router.get("")
async def list_projects(
    include_archived: bool = Query(False),
    current_user: dict = Depends(get_current_user),
):
    """List all projects in the current user's organization."""
    engine = get_engine_service()
    projects = await engine.project_service.list_by_org(
        current_user["org_id"], include_archived,
    )
    return [p.to_dict() for p in projects]

@router.post("")
async def create_project(
    request: CreateProjectRequest,
    current_user: dict = Depends(get_current_user),
):
    """Create a project (head/admin only)."""
    engine = get_engine_service()
    user = await _load_user(engine, current_user["user_id"])
    if not user:
        raise HTTPException(status_code=401, detail="User not found")

    try:
        project = await engine.project_service.create(
            name=request.name,
            user=user,
            description=request.description,
        )
        return project.to_dict()
    except PermissionError as e:
        raise HTTPException(status_code=403, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

@router.get("/{project_id}")
async def get_project(
    project_id: str,
    current_user: dict = Depends(get_current_user),
):
    """Get a project by ID. 404 if not found or cross-org."""
    engine = get_engine_service()
    try:
        pid = UUID(project_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid project ID")

    user = await _load_user(engine, current_user["user_id"])
    if not user:
        raise HTTPException(status_code=401, detail="User not found")

    project = await engine.project_service.get(pid, user)
    if project is None:
        raise HTTPException(status_code=404, detail="Project not found")
    return project.to_dict()

@router.patch("/{project_id}")
async def update_project(
    project_id: str,
    request: UpdateProjectRequest,
    current_user: dict = Depends(get_current_user),
):
    """Update a project (head/admin only)."""
    engine = get_engine_service()
    try:
        pid = UUID(project_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid project ID")

    user = await _load_user(engine, current_user["user_id"])
    if not user:
        raise HTTPException(status_code=401, detail="User not found")

    try:
        project = await engine.project_service.update(
            project_id=pid,
            user=user,
            name=request.name,
            description=request.description,
        )
        return project.to_dict()
    except PermissionError as e:
        raise HTTPException(status_code=403, detail=str(e))
    except ValueError as e:
        msg = str(e)
        if "not found" in msg.lower():
            raise HTTPException(status_code=404, detail=msg)
        raise HTTPException(status_code=400, detail=msg)

@router.delete("/{project_id}")
async def delete_project(
    project_id: str,
    current_user: dict = Depends(get_current_user),
):
    """Soft-delete a project (head/admin only). Archives its chat; tasks keep project_id."""
    engine = get_engine_service()
    try:
        pid = UUID(project_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid project ID")

    user = await _load_user(engine, current_user["user_id"])
    if not user:
        raise HTTPException(status_code=401, detail="User not found")

    try:
        ok = await engine.project_service.delete(pid, user)
    except PermissionError as e:
        raise HTTPException(status_code=403, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))

    if not ok:
        raise HTTPException(status_code=404, detail="Project not found")
    return {"success": True, "message": "Project archived"}

@router.get("/{project_id}/chat")
async def get_project_chat(
    project_id: str,
    current_user: dict = Depends(get_current_user),
):
    """Resolve a project's chat. 404 if project not found or cross-org or chat missing."""
    engine = get_engine_service()
    try:
        pid = UUID(project_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid project ID")

    user = await _load_user(engine, current_user["user_id"])
    if not user:
        raise HTTPException(status_code=401, detail="User not found")

    project = await engine.project_service.get(pid, user)
    if project is None:
        raise HTTPException(status_code=404, detail="Project not found")

    chat = await engine.chat_service.chat_storage.get_by_project_id(pid)
    if chat is None:
        raise HTTPException(status_code=404, detail="Project chat not found")
    return chat.to_dict()
