"""
Roles Routes

Endpoints for AI role management.
Roles are predefined — only GET endpoints and admin cache management.
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
# Response Models
# ============================================

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
    agent_type: str
    agent_config: dict
    tools: list
    prompt_file: Optional[str]
    is_active: bool
    created_at: str
    updated_at: str


# ============================================
# Helper
# ============================================

def _get_roles_service() -> RolesService:
    """Get RolesService with PromptCache"""
    engine = get_engine_service()
    return RolesService(
        engine.role_storage,
        engine.user_storage,
        engine.prompt_cache,
    )


# ============================================
# Routes: Read-only
# ============================================

@router.get("", response_model=List[RoleResponse])
@router.get("/", response_model=List[RoleResponse])
async def list_roles(current_user: dict = Depends(get_current_user)):
    """List roles in current organization"""
    roles_service = _get_roles_service()
    roles = await roles_service.list_roles(current_user["org_id"])
    return [RoleResponse(**r.to_dict()) for r in roles]


@router.get("/{role_id}", response_model=RoleResponse)
async def get_role(
    role_id: str,
    current_user: dict = Depends(get_current_user)
):
    """Get role by ID"""
    roles_service = _get_roles_service()

    try:
        role_uuid = UUID(role_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid role ID")

    role = await roles_service.get_role(role_uuid)
    if not role:
        raise HTTPException(status_code=404, detail="Role not found")

    if role.org_id != current_user["org_id"]:
        raise HTTPException(status_code=403, detail="Access denied")

    return RoleResponse(**role.to_dict())


@router.get("/code/{code}", response_model=RoleResponse)
async def get_role_by_code(
    code: str,
    current_user: dict = Depends(get_current_user)
):
    """Get role by code in current organization"""
    roles_service = _get_roles_service()

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
    roles_service = _get_roles_service()

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


# ============================================
# Routes: Admin — Prompt Cache Management
# ============================================

@router.post("/admin/cache/prompts/clear")
async def clear_all_prompt_cache(
    current_user: dict = Depends(get_current_user)
):
    """Clear entire prompt cache (admin only)"""
    engine = get_engine_service()

    user = await engine.user_storage.get_by_id(current_user["user_id"])
    if not user or not user.is_admin:
        raise HTTPException(status_code=403, detail="Admin access required")

    roles_service = _get_roles_service()
    roles_service.clear_prompt_cache()

    return {"success": True, "message": "All prompt caches cleared"}


@router.post("/admin/cache/prompts/clear/{role_code}")
async def clear_role_prompt_cache(
    role_code: str,
    current_user: dict = Depends(get_current_user)
):
    """Clear prompt cache for a specific role (admin only)"""
    engine = get_engine_service()

    user = await engine.user_storage.get_by_id(current_user["user_id"])
    if not user or not user.is_admin:
        raise HTTPException(status_code=403, detail="Admin access required")

    roles_service = _get_roles_service()

    # Find role to get prompt_file
    role = await roles_service.get_role_by_code(role_code, current_user["org_id"])
    if not role:
        raise HTTPException(status_code=404, detail="Role not found")

    if role.prompt_file:
        roles_service.clear_prompt_cache(role.prompt_file)
        return {"success": True, "message": f"Prompt cache cleared for {role_code}"}

    return {"success": True, "message": f"Role {role_code} has no prompt_file, nothing to clear"}
