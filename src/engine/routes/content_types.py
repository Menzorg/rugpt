"""
Content Type Routes

Admin-managed per-org catalog of file content types ("Отчёт", "Заказ", ...).
The picker (GET active) is available to any authenticated user; all mutations are
admin-only. Soft-delete via is_active. See migration 051.
"""

from src.engine.unified_logger import get_logger
from uuid import UUID
from typing import Optional

import asyncpg
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from ..services.engine_service import get_engine_service
from .auth import get_current_user

logger = get_logger("routes")
router = APIRouter(prefix="/content-types", tags=["content-types"])


class CreateContentTypeRequest(BaseModel):
    name: str
    description: str = ""


class UpdateContentTypeRequest(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    is_active: Optional[bool] = None


def _require_admin(current_user: dict):
    if not current_user.get("is_admin"):
        raise HTTPException(status_code=403, detail="Admin access required")


@router.get("/")
async def list_content_types(
    include_inactive: bool = Query(False),
    current_user: dict = Depends(get_current_user),
):
    """List content types of the caller's org. Picker: active only.
    include_inactive=true (admin only) also returns deactivated entries."""
    if include_inactive:
        _require_admin(current_user)
    engine = get_engine_service()
    items = await engine.content_type_service.list(
        current_user["org_id"], include_inactive=include_inactive,
    )
    return [c.to_dict() for c in items]


@router.post("/")
async def create_content_type(
    request: CreateContentTypeRequest,
    current_user: dict = Depends(get_current_user),
):
    _require_admin(current_user)
    name = request.name.strip()
    if not name:
        raise HTTPException(status_code=400, detail="Name is required")
    engine = get_engine_service()
    try:
        ct = await engine.content_type_service.create(
            org_id=current_user["org_id"], name=name, description=request.description or "",
        )
    except asyncpg.UniqueViolationError:
        raise HTTPException(status_code=409, detail="Content type with this name already exists")
    logger.info("content_type create: org=%s name=%s id=%s", current_user["org_id"], name, ct.id)
    return ct.to_dict()


@router.patch("/{content_type_id}")
async def update_content_type(
    content_type_id: UUID,
    request: UpdateContentTypeRequest,
    current_user: dict = Depends(get_current_user),
):
    _require_admin(current_user)
    engine = get_engine_service()
    ct = await engine.content_type_service.get(content_type_id)
    if not ct or ct.org_id != current_user["org_id"]:
        raise HTTPException(status_code=404, detail="Content type not found")
    try:
        updated = await engine.content_type_service.update(
            content_type_id,
            name=request.name,
            description=request.description,
            is_active=request.is_active,
        )
    except asyncpg.UniqueViolationError:
        raise HTTPException(status_code=409, detail="Content type with this name already exists")
    return updated.to_dict()


@router.delete("/{content_type_id}")
async def delete_content_type(
    content_type_id: UUID,
    current_user: dict = Depends(get_current_user),
):
    _require_admin(current_user)
    engine = get_engine_service()
    ct = await engine.content_type_service.get(content_type_id)
    if not ct or ct.org_id != current_user["org_id"]:
        raise HTTPException(status_code=404, detail="Content type not found")
    await engine.content_type_service.delete(content_type_id)
    logger.info("content_type soft-delete: org=%s id=%s", current_user["org_id"], content_type_id)
    return {"status": "deleted"}
