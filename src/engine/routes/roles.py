"""
Roles Routes

Endpoints for AI role management.
"""
import logging
from typing import Optional, List
from uuid import UUID

from fastapi import APIRouter, HTTPException, Depends
from pydantic import BaseModel

from ..services.engine_service import get_engine_service
from ..services.roles_service import RolesService
from .auth import get_current_user

logger = logging.getLogger("rugpt.routes.roles")
router = APIRouter(prefix="/roles", tags=["roles"])


# ============================================
# Request/Response Models
# ============================================

class CreateRoleRequest(BaseModel):
    """Create role request"""
    name: str
    code: str
    system_prompt: str
    description: Optional[str] = None
    model_name: str = "qwen2.5:7b"


class UpdateRoleRequest(BaseModel):
    """Update role request"""
    name: Optional[str] = None
    code: Optional[str] = None
    description: Optional[str] = None
    system_prompt: Optional[str] = None
    model_name: Optional[str] = None


class RoleResponse(BaseModel):
    """Role response"""
    id: str
    org_id: str
    name: str
    code: str
    description: Optional[str]
    system_prompt: str
    rag_collection: Optional[str]
    model_name: str
    is_active: bool
    created_at: str
    updated_at: str


class RoleUsersResponse(BaseModel):
    """Role with assigned users"""
    role: RoleResponse
    users: List[dict]


# ============================================
# Routes
# ============================================

@router.get("", response_model=List[RoleResponse])
@router.get("/", response_model=List[RoleResponse])
async def list_roles(current_user: dict = Depends(get_current_user)):
    """List roles in current organization"""
    engine = get_engine_service()
    roles_service = RolesService(engine.role_storage, engine.user_storage)

    roles = await roles_service.list_roles(current_user["org_id"])
    return [RoleResponse(**r.to_dict()) for r in roles]


@router.post("", response_model=RoleResponse)
@router.post("/", response_model=RoleResponse)
async def create_role(
    request: CreateRoleRequest,
    current_user: dict = Depends(get_current_user)
):
    """Create a new AI role (admin only)"""
    engine = get_engine_service()

    # Check if current user is admin
    user = await engine.user_storage.get_by_id(current_user["user_id"])
    if not user or not user.is_admin:
        raise HTTPException(status_code=403, detail="Admin access required")

    roles_service = RolesService(engine.role_storage, engine.user_storage)

    try:
        role = await roles_service.create_role(
            org_id=current_user["org_id"],
            name=request.name,
            code=request.code,
            system_prompt=request.system_prompt,
            description=request.description,
            model_name=request.model_name
        )
        return RoleResponse(**role.to_dict())
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.get("/{role_id}", response_model=RoleResponse)
async def get_role(
    role_id: str,
    current_user: dict = Depends(get_current_user)
):
    """Get role by ID"""
    engine = get_engine_service()
    roles_service = RolesService(engine.role_storage, engine.user_storage)

    try:
        role_uuid = UUID(role_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid role ID")

    role = await roles_service.get_role(role_uuid)
    if not role:
        raise HTTPException(status_code=404, detail="Role not found")

    # Check org access
    if role.org_id != current_user["org_id"]:
        raise HTTPException(status_code=403, detail="Access denied")

    return RoleResponse(**role.to_dict())


@router.get("/code/{code}", response_model=RoleResponse)
async def get_role_by_code(
    code: str,
    current_user: dict = Depends(get_current_user)
):
    """Get role by code in current organization"""
    engine = get_engine_service()
    roles_service = RolesService(engine.role_storage, engine.user_storage)

    role = await roles_service.get_role_by_code(code, current_user["org_id"])
    if not role:
        raise HTTPException(status_code=404, detail="Role not found")

    return RoleResponse(**role.to_dict())


@router.get("/{role_id}/users")
async def get_role_users(
    role_id: str,
    current_user: dict = Depends(get_current_user)
):
    """Get users assigned to a role"""
    engine = get_engine_service()
    roles_service = RolesService(engine.role_storage, engine.user_storage)

    try:
        role_uuid = UUID(role_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid role ID")

    role = await roles_service.get_role(role_uuid)
    if not role:
        raise HTTPException(status_code=404, detail="Role not found")

    if role.org_id != current_user["org_id"]:
        raise HTTPException(status_code=403, detail="Access denied")

    users = await roles_service.get_users_with_role(role_uuid)
    return {
        "role": RoleResponse(**role.to_dict()),
        "users": [u.to_dict() for u in users]
    }


@router.patch("/{role_id}", response_model=RoleResponse)
async def update_role(
    role_id: str,
    request: UpdateRoleRequest,
    current_user: dict = Depends(get_current_user)
):
    """Update role (admin only)"""
    engine = get_engine_service()

    # Check if current user is admin
    user = await engine.user_storage.get_by_id(current_user["user_id"])
    if not user or not user.is_admin:
        raise HTTPException(status_code=403, detail="Admin access required")

    try:
        role_uuid = UUID(role_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid role ID")

    # Check role belongs to org
    role = await engine.role_storage.get_by_id(role_uuid)
    if not role:
        raise HTTPException(status_code=404, detail="Role not found")
    if role.org_id != current_user["org_id"]:
        raise HTTPException(status_code=403, detail="Access denied")

    roles_service = RolesService(engine.role_storage, engine.user_storage)

    try:
        updated = await roles_service.update_role(
            role_id=role_uuid,
            name=request.name,
            code=request.code,
            description=request.description,
            system_prompt=request.system_prompt,
            model_name=request.model_name
        )
        return RoleResponse(**updated.to_dict())
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.delete("/{role_id}")
async def deactivate_role(
    role_id: str,
    current_user: dict = Depends(get_current_user)
):
    """Deactivate role (admin only)"""
    engine = get_engine_service()

    # Check if current user is admin
    user = await engine.user_storage.get_by_id(current_user["user_id"])
    if not user or not user.is_admin:
        raise HTTPException(status_code=403, detail="Admin access required")

    try:
        role_uuid = UUID(role_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid role ID")

    # Check role belongs to org
    role = await engine.role_storage.get_by_id(role_uuid)
    if not role:
        raise HTTPException(status_code=404, detail="Role not found")
    if role.org_id != current_user["org_id"]:
        raise HTTPException(status_code=403, detail="Access denied")

    roles_service = RolesService(engine.role_storage, engine.user_storage)
    await roles_service.deactivate_role(role_uuid)

    return {"success": True, "message": "Role deactivated"}
