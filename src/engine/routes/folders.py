"""
Folder Routes — personal file folders.

Endpoints under /api/v1/folders:
- POST    /folders              Create
- GET     /folders              List (flat)
- GET     /folders/tree         Tree (nested children)
- GET     /folders/{id}         Get one
- PATCH   /folders/{id}         Rename and/or move
- DELETE  /folders/{id}         Cascade soft-delete
- GET     /folders/{id}/files   List direct files in this folder
"""
import logging
from typing import List, Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Body
from pydantic import BaseModel

from ..services.engine_service import get_engine_service
from ..services.folder_service import (
    FolderError, FolderNotFound, FolderForbidden, FolderInvalidName,
    FolderMaxDepthExceeded, FolderCyclicMove, FolderInvalidParentOwner,
    FolderParentNotFound, FolderNameConflict,
)
from .auth import get_current_user

logger = logging.getLogger("rugpt.routes.folders")
router = APIRouter(prefix="/folders", tags=["folders"])


class FolderResponse(BaseModel):
    id: str
    user_id: str
    org_id: str
    parent_folder_id: Optional[str]
    name: str
    is_active: bool
    created_at: str
    updated_at: str


class FolderCreateRequest(BaseModel):
    name: str
    parent_folder_id: Optional[str] = None


class FolderDeleteResponse(BaseModel):
    success: bool
    deleted_folders: int
    deleted_files: int


_STATUS_MAP = {
    "EMPTY_NAME": 400, "NAME_TOO_LONG": 400,
    "MAX_DEPTH_EXCEEDED": 400, "CYCLIC_MOVE": 400,
    "INVALID_PARENT_OWNER": 400,
    "FORBIDDEN": 403,
    "FOLDER_NOT_FOUND": 404, "PARENT_NOT_FOUND": 404,
    "DUPLICATE_NAME": 409,
}


def _raise_for(e: FolderError) -> HTTPException:
    return HTTPException(
        status_code=_STATUS_MAP.get(e.code, 500),
        detail={"code": e.code, "message": e.message},
    )


def _resolve_target_user(current_user: dict, query_user_id: Optional[str]) -> UUID:
    """Non-admin must use own user_id. Admin can pass ?user_id= to target another."""
    if not query_user_id:
        return current_user["user_id"]
    if not current_user.get("is_admin"):
        raise HTTPException(status_code=403, detail={"code": "FORBIDDEN", "message": "Only admin can target other users"})
    try:
        return UUID(query_user_id)
    except ValueError:
        raise HTTPException(status_code=422, detail="Invalid user_id")


def _user_to_obj(current_user: dict):
    """Build a User-like duck for FolderService permission checks."""
    class _Actor:
        id = current_user["user_id"]
        org_id = current_user["org_id"]
        is_admin = current_user.get("is_admin", False)
    return _Actor()


@router.post("", response_model=FolderResponse)
async def create_folder(
    body: FolderCreateRequest,
    user_id: Optional[str] = Query(None),
    current_user: dict = Depends(get_current_user),
):
    engine = get_engine_service()
    target_user_id = _resolve_target_user(current_user, user_id)
    parent_uuid: Optional[UUID] = None
    if body.parent_folder_id:
        try:
            parent_uuid = UUID(body.parent_folder_id)
        except ValueError:
            raise HTTPException(status_code=422, detail="Invalid parent_folder_id")
    try:
        created = await engine.folder_service.create(
            user_id=target_user_id,
            org_id=current_user["org_id"],
            parent_folder_id=parent_uuid,
            name=body.name,
        )
        return FolderResponse(**created.to_dict())
    except FolderError as e:
        raise _raise_for(e)


@router.get("", response_model=List[FolderResponse])
async def list_folders(
    user_id: Optional[str] = Query(None),
    current_user: dict = Depends(get_current_user),
):
    engine = get_engine_service()
    target_user_id = _resolve_target_user(current_user, user_id)
    folders = await engine.user_file_folder_storage.list_by_user(target_user_id)
    return [FolderResponse(**f.to_dict()) for f in folders]


@router.get("/tree")
async def get_folder_tree(
    user_id: Optional[str] = Query(None),
    current_user: dict = Depends(get_current_user),
):
    engine = get_engine_service()
    target_user_id = _resolve_target_user(current_user, user_id)
    return await engine.folder_service.get_tree(
        user_id=target_user_id,
        org_id=current_user["org_id"],
    )


@router.get("/{folder_id}", response_model=FolderResponse)
async def get_folder(
    folder_id: str,
    current_user: dict = Depends(get_current_user),
):
    engine = get_engine_service()
    try:
        fuuid = UUID(folder_id)
    except ValueError:
        raise HTTPException(status_code=422, detail="Invalid folder ID")
    folder = await engine.user_file_folder_storage.get_by_id(fuuid)
    if folder is None:
        raise HTTPException(status_code=404, detail={"code": "FOLDER_NOT_FOUND", "message": "Папка не найдена"})
    if folder.user_id != current_user["user_id"] and not (
        current_user.get("is_admin") and folder.org_id == current_user["org_id"]
    ):
        raise HTTPException(status_code=403, detail={"code": "FORBIDDEN", "message": "Доступ запрещён"})
    return FolderResponse(**folder.to_dict())


@router.patch("/{folder_id}", response_model=FolderResponse)
async def update_folder(
    folder_id: str,
    body: dict = Body(...),
    current_user: dict = Depends(get_current_user),
):
    """Rename and/or move. Body keys: 'name' (string) and/or 'parent_folder_id' (string|null|absent)."""
    engine = get_engine_service()
    try:
        fuuid = UUID(folder_id)
    except ValueError:
        raise HTTPException(status_code=422, detail="Invalid folder ID")
    actor = _user_to_obj(current_user)
    try:
        result = None
        if "name" in body:
            result = await engine.folder_service.rename(
                folder_id=fuuid, new_name=body["name"], actor=actor,
            )
        if "parent_folder_id" in body:
            new_parent = body["parent_folder_id"]
            new_parent_uuid = UUID(new_parent) if new_parent else None
            result = await engine.folder_service.move(
                folder_id=fuuid, new_parent_id=new_parent_uuid, actor=actor,
            )
        if result is None:
            folder = await engine.user_file_folder_storage.get_by_id(fuuid)
            if folder is None:
                raise HTTPException(status_code=404, detail={"code": "FOLDER_NOT_FOUND", "message": "Папка не найдена"})
            return FolderResponse(**folder.to_dict())
        return FolderResponse(**result.to_dict())
    except FolderError as e:
        raise _raise_for(e)
    except ValueError:
        raise HTTPException(status_code=422, detail="Invalid UUID in payload")


@router.delete("/{folder_id}", response_model=FolderDeleteResponse)
async def delete_folder(
    folder_id: str,
    current_user: dict = Depends(get_current_user),
):
    engine = get_engine_service()
    try:
        fuuid = UUID(folder_id)
    except ValueError:
        raise HTTPException(status_code=422, detail="Invalid folder ID")
    actor = _user_to_obj(current_user)
    try:
        out = await engine.folder_service.delete(folder_id=fuuid, actor=actor)
        return FolderDeleteResponse(success=True, **out)
    except FolderError as e:
        raise _raise_for(e)


@router.get("/{folder_id}/files")
async def list_folder_files(
    folder_id: str,
    current_user: dict = Depends(get_current_user),
):
    from .files import FileResponse
    engine = get_engine_service()
    try:
        fuuid = UUID(folder_id)
    except ValueError:
        raise HTTPException(status_code=422, detail="Invalid folder ID")
    folder = await engine.user_file_folder_storage.get_by_id(fuuid)
    if folder is None:
        raise HTTPException(status_code=404, detail={"code": "FOLDER_NOT_FOUND", "message": "Папка не найдена"})
    if folder.user_id != current_user["user_id"] and not (
        current_user.get("is_admin") and folder.org_id == current_user["org_id"]
    ):
        raise HTTPException(status_code=403, detail={"code": "FORBIDDEN", "message": "Доступ запрещён"})
    files = await engine.user_file_storage.list_by_user_in_folder(folder.user_id, fuuid)
    return [FileResponse(**f.to_dict()) for f in files]
