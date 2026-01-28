"""
Organizations Routes

Endpoints for organization management (admin only).
"""
import logging
from typing import Optional, List
from uuid import UUID

from fastapi import APIRouter, HTTPException, Depends
from pydantic import BaseModel

from ..services.engine_service import get_engine_service
from ..services.org_service import OrgService
from .auth import get_current_user

logger = logging.getLogger("rugpt.routes.organizations")
router = APIRouter(prefix="/organizations", tags=["organizations"])


# ============================================
# Request/Response Models
# ============================================

class CreateOrgRequest(BaseModel):
    """Create organization request"""
    name: str
    slug: Optional[str] = None
    description: Optional[str] = None


class UpdateOrgRequest(BaseModel):
    """Update organization request"""
    name: Optional[str] = None
    slug: Optional[str] = None
    description: Optional[str] = None


class OrgResponse(BaseModel):
    """Organization response"""
    id: str
    name: str
    slug: str
    description: Optional[str]
    is_active: bool
    created_at: str
    updated_at: str


# ============================================
# Routes
# ============================================

@router.post("", response_model=OrgResponse)
@router.post("/", response_model=OrgResponse)
async def create_organization(
    request: CreateOrgRequest,
    current_user: dict = Depends(get_current_user)
):
    """
    Create a new organization.

    Note: In production, this should be restricted to super-admins.
    """
    engine = get_engine_service()

    # Check if current user is admin
    user = await engine.user_storage.get_by_id(current_user["user_id"])
    if not user or not user.is_admin:
        raise HTTPException(status_code=403, detail="Admin access required")

    org_service = OrgService(engine.org_storage)

    try:
        org = await org_service.create_organization(
            name=request.name,
            slug=request.slug,
            description=request.description
        )
        return OrgResponse(**org.to_dict())
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.get("", response_model=List[OrgResponse])
@router.get("/", response_model=List[OrgResponse])
async def list_organizations(current_user: dict = Depends(get_current_user)):
    """List all organizations (admin only)"""
    engine = get_engine_service()

    # Check if current user is admin
    user = await engine.user_storage.get_by_id(current_user["user_id"])
    if not user or not user.is_admin:
        raise HTTPException(status_code=403, detail="Admin access required")

    org_service = OrgService(engine.org_storage)
    orgs = await org_service.list_organizations()
    return [OrgResponse(**org.to_dict()) for org in orgs]


@router.get("/{org_id}", response_model=OrgResponse)
async def get_organization(
    org_id: str,
    current_user: dict = Depends(get_current_user)
):
    """Get organization by ID"""
    engine = get_engine_service()
    org_service = OrgService(engine.org_storage)

    try:
        org_uuid = UUID(org_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid organization ID")

    org = await org_service.get_organization(org_uuid)
    if not org:
        raise HTTPException(status_code=404, detail="Organization not found")

    return OrgResponse(**org.to_dict())


@router.patch("/{org_id}", response_model=OrgResponse)
async def update_organization(
    org_id: str,
    request: UpdateOrgRequest,
    current_user: dict = Depends(get_current_user)
):
    """Update organization (admin only)"""
    engine = get_engine_service()

    # Check if current user is admin
    user = await engine.user_storage.get_by_id(current_user["user_id"])
    if not user or not user.is_admin:
        raise HTTPException(status_code=403, detail="Admin access required")

    try:
        org_uuid = UUID(org_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid organization ID")

    org_service = OrgService(engine.org_storage)

    try:
        org = await org_service.update_organization(
            org_id=org_uuid,
            name=request.name,
            slug=request.slug,
            description=request.description
        )
        if not org:
            raise HTTPException(status_code=404, detail="Organization not found")
        return OrgResponse(**org.to_dict())
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.delete("/{org_id}")
async def deactivate_organization(
    org_id: str,
    current_user: dict = Depends(get_current_user)
):
    """Deactivate organization (admin only)"""
    engine = get_engine_service()

    # Check if current user is admin
    user = await engine.user_storage.get_by_id(current_user["user_id"])
    if not user or not user.is_admin:
        raise HTTPException(status_code=403, detail="Admin access required")

    try:
        org_uuid = UUID(org_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid organization ID")

    org_service = OrgService(engine.org_storage)
    result = await org_service.deactivate_organization(org_uuid)

    if not result:
        raise HTTPException(status_code=404, detail="Organization not found")

    return {"success": True, "message": "Organization deactivated"}
