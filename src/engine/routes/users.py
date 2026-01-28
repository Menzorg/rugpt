"""
Users Routes

Endpoints for user management.
"""
import logging
from typing import Optional, List
from uuid import UUID

from fastapi import APIRouter, HTTPException, Depends
from pydantic import BaseModel, EmailStr

from ..services.engine_service import get_engine_service
from ..services.users_service import UsersService
from .auth import get_current_user

logger = logging.getLogger("rugpt.routes.users")
router = APIRouter(prefix="/users", tags=["users"])


# ============================================
# Request/Response Models
# ============================================

class CreateUserRequest(BaseModel):
    """Create user request (admin only)"""
    name: str
    username: str
    email: EmailStr
    password: str
    is_admin: bool = False
    role_id: Optional[str] = None


class UpdateUserRequest(BaseModel):
    """Update user request"""
    name: Optional[str] = None
    username: Optional[str] = None
    email: Optional[EmailStr] = None
    avatar_url: Optional[str] = None
    is_admin: Optional[bool] = None


class ChangePasswordRequest(BaseModel):
    """Change password request"""
    current_password: str
    new_password: str


class AssignRoleRequest(BaseModel):
    """Assign role to user request"""
    role_id: Optional[str] = None  # None to unassign


class UserResponse(BaseModel):
    """User response"""
    id: str
    org_id: str
    name: str
    username: str
    email: str
    role_id: Optional[str]
    is_admin: bool
    is_active: bool
    avatar_url: Optional[str]
    created_at: str
    updated_at: str
    last_seen_at: Optional[str]


# ============================================
# Routes
# ============================================

@router.get("", response_model=List[UserResponse])
@router.get("/", response_model=List[UserResponse])
async def list_users(current_user: dict = Depends(get_current_user)):
    """List users in current organization"""
    engine = get_engine_service()
    users_service = UsersService(engine.user_storage)

    users = await users_service.list_users(current_user["org_id"])
    return [UserResponse(**u.to_dict()) for u in users]


@router.post("", response_model=UserResponse)
@router.post("/", response_model=UserResponse)
async def create_user(
    request: CreateUserRequest,
    current_user: dict = Depends(get_current_user)
):
    """Create a new user (admin only)"""
    engine = get_engine_service()

    # Check if current user is admin
    user = await engine.user_storage.get_by_id(current_user["user_id"])
    if not user or not user.is_admin:
        raise HTTPException(status_code=403, detail="Admin access required")

    users_service = UsersService(engine.user_storage)

    role_id = UUID(request.role_id) if request.role_id else None

    try:
        new_user = await users_service.create_user(
            org_id=current_user["org_id"],
            name=request.name,
            username=request.username,
            email=request.email,
            password=request.password,
            is_admin=request.is_admin,
            role_id=role_id
        )
        return UserResponse(**new_user.to_dict())
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.get("/{user_id}", response_model=UserResponse)
async def get_user(
    user_id: str,
    current_user: dict = Depends(get_current_user)
):
    """Get user by ID"""
    engine = get_engine_service()
    users_service = UsersService(engine.user_storage)

    try:
        user_uuid = UUID(user_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid user ID")

    user = await users_service.get_user(user_uuid)
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    # Users can only view users in their org
    if user.org_id != current_user["org_id"]:
        raise HTTPException(status_code=403, detail="Access denied")

    return UserResponse(**user.to_dict())


@router.get("/username/{username}", response_model=UserResponse)
async def get_user_by_username(
    username: str,
    current_user: dict = Depends(get_current_user)
):
    """Get user by username in current organization"""
    engine = get_engine_service()
    users_service = UsersService(engine.user_storage)

    user = await users_service.get_user_by_username(username, current_user["org_id"])
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    return UserResponse(**user.to_dict())


@router.patch("/{user_id}", response_model=UserResponse)
async def update_user(
    user_id: str,
    request: UpdateUserRequest,
    current_user: dict = Depends(get_current_user)
):
    """Update user (self or admin)"""
    engine = get_engine_service()
    users_service = UsersService(engine.user_storage)

    try:
        user_uuid = UUID(user_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid user ID")

    # Check permissions
    current = await engine.user_storage.get_by_id(current_user["user_id"])
    is_self = user_uuid == current_user["user_id"]
    is_admin = current and current.is_admin

    if not is_self and not is_admin:
        raise HTTPException(status_code=403, detail="Access denied")

    # Only admins can change is_admin
    if request.is_admin is not None and not is_admin:
        raise HTTPException(status_code=403, detail="Only admins can change admin status")

    try:
        updated = await users_service.update_user(
            user_id=user_uuid,
            name=request.name,
            username=request.username,
            email=request.email,
            avatar_url=request.avatar_url,
            is_admin=request.is_admin
        )
        if not updated:
            raise HTTPException(status_code=404, detail="User not found")
        return UserResponse(**updated.to_dict())
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/{user_id}/password")
async def change_password(
    user_id: str,
    request: ChangePasswordRequest,
    current_user: dict = Depends(get_current_user)
):
    """Change user password (self only)"""
    try:
        user_uuid = UUID(user_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid user ID")

    # Only self can change password
    if user_uuid != current_user["user_id"]:
        raise HTTPException(status_code=403, detail="Can only change own password")

    engine = get_engine_service()
    users_service = UsersService(engine.user_storage)

    # Verify current password
    if not await users_service.verify_password(user_uuid, request.current_password):
        raise HTTPException(status_code=400, detail="Current password is incorrect")

    # Change password
    await users_service.change_password(user_uuid, request.new_password)
    return {"success": True, "message": "Password changed"}


@router.post("/{user_id}/role")
async def assign_role(
    user_id: str,
    request: AssignRoleRequest,
    current_user: dict = Depends(get_current_user)
):
    """Assign AI role to user (admin only)"""
    engine = get_engine_service()

    # Check if current user is admin
    current = await engine.user_storage.get_by_id(current_user["user_id"])
    if not current or not current.is_admin:
        raise HTTPException(status_code=403, detail="Admin access required")

    try:
        user_uuid = UUID(user_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid user ID")

    role_id = UUID(request.role_id) if request.role_id else None

    # Verify role exists if provided
    if role_id:
        role = await engine.role_storage.get_by_id(role_id)
        if not role:
            raise HTTPException(status_code=404, detail="Role not found")
        if role.org_id != current_user["org_id"]:
            raise HTTPException(status_code=403, detail="Role belongs to different organization")

    users_service = UsersService(engine.user_storage)
    result = await users_service.assign_role(user_uuid, role_id)

    if not result:
        raise HTTPException(status_code=404, detail="User not found")

    return {"success": True, "message": "Role assigned" if role_id else "Role unassigned"}


@router.delete("/{user_id}")
async def deactivate_user(
    user_id: str,
    current_user: dict = Depends(get_current_user)
):
    """Deactivate user (admin only)"""
    engine = get_engine_service()

    # Check if current user is admin
    current = await engine.user_storage.get_by_id(current_user["user_id"])
    if not current or not current.is_admin:
        raise HTTPException(status_code=403, detail="Admin access required")

    try:
        user_uuid = UUID(user_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid user ID")

    # Can't deactivate self
    if user_uuid == current_user["user_id"]:
        raise HTTPException(status_code=400, detail="Cannot deactivate yourself")

    users_service = UsersService(engine.user_storage)
    result = await users_service.deactivate_user(user_uuid)

    if not result:
        raise HTTPException(status_code=404, detail="User not found")

    return {"success": True, "message": "User deactivated"}
