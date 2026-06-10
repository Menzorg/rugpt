"""
File Management Routes

Endpoints for file upload/download/management:
- POST /files/upload — upload a file for an employee
- GET /files — list files (by user or org)
- GET /files/{file_id} — get file metadata
- GET /files/{file_id}/download — download file binary
- DELETE /files/{file_id} — soft-delete file
"""

from src.engine.unified_logger import get_logger
from typing import Optional, List
from urllib.parse import quote as urlquote
from uuid import UUID

from fastapi import APIRouter, HTTPException, Depends, Query, UploadFile, File, Form, Body
from fastapi.responses import Response
from pydantic import BaseModel

from ..services.engine_service import get_engine_service
from ..services.folder_service import FolderError
from ..constants import CONTENT_TYPES
from .auth import get_current_user

logger = get_logger("routes")
router = APIRouter(prefix="/files", tags=["files"])

class FileResponse(BaseModel):
    id: str
    user_id: str
    org_id: str
    uploaded_by_user_id: str
    storage_key: str
    original_filename: str
    file_type: str
    file_size: int
    rag_status: str
    rag_error: Optional[str]
    indexed_at: Optional[str]
    is_table: bool
    is_public: bool
    is_active: bool
    created_at: str
    updated_at: str
    cloned_from_file_id: Optional[str] = None
    folder_id: Optional[str] = None

@router.post("/upload", response_model=FileResponse)
async def upload_file(
    file: UploadFile = File(...),
    target_user_id: Optional[str] = Form(None, description="Employee UUID who owns this file (defaults to authenticated user). Only admins may set it to another user."),
    is_public: bool = Form(False, description="Make file visible to all org users"),
    folder_id: Optional[str] = Form(None, description="Target folder UUID (defaults to root)"),
    current_user: dict = Depends(get_current_user),
):
    """Upload a file for an employee (manager action)"""
    engine = get_engine_service()


    try:
        user_uuid = UUID(target_user_id) if target_user_id else current_user["user_id"]
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid target_user_id")

    if not current_user.get("is_admin") and user_uuid != current_user["user_id"]:
        raise HTTPException(status_code=403, detail="You can only upload files for yourself")

    folder_uuid: Optional[UUID] = None
    if folder_id and folder_id != "null":
        try:
            folder_uuid = UUID(folder_id)
        except ValueError:
            raise HTTPException(status_code=422, detail="Invalid folder_id")
        try:
            await engine.folder_service.verify_folder_owner(
                folder_id=folder_uuid, user_id=user_uuid, org_id=current_user["org_id"],
            )
        except FolderError as e:
            raise HTTPException(
                status_code={"FOLDER_NOT_FOUND": 404, "INVALID_PARENT_OWNER": 400}.get(e.code, 400),
                detail={"code": e.code, "message": e.message},
            )

    data = await file.read()
    logger.info("Can read file. Trying to ingest")

    try:
        created = await engine.file_service.upload(
            org_id=current_user["org_id"],
            user_id=user_uuid,
            uploaded_by_user_id=current_user["user_id"],
            filename=file.filename or "unnamed",
            data=data,
            is_public=is_public,
            folder_id=folder_uuid,
        )
        return FileResponse(**created.to_dict())
    except ValueError as e:
        logger.error("Can't ingest file", exc_info=e)
        raise HTTPException(status_code=400, detail=str(e))

@router.get("", response_model=List[FileResponse])
async def list_files(
    folder_id: Optional[str] = Query(None, description="Filter by folder. 'null'=root, UUID=specific, omit=all"),
    current_user: dict = Depends(get_current_user),
):
    engine = get_engine_service()
    if folder_id is not None:
        if folder_id == "null":
            folder_uuid = None
        else:
            try:
                folder_uuid = UUID(folder_id)
            except ValueError:
                raise HTTPException(status_code=422, detail="Invalid folder_id")
        files = await engine.user_file_storage.list_by_user_in_folder(current_user["user_id"], folder_uuid)
    elif current_user.get("is_admin"):
        files = await engine.file_service.list_by_org(current_user["org_id"])
    else:
        files = await engine.file_service.list_by_user(current_user["user_id"])
    return [FileResponse(**f.to_dict()) for f in files]

@router.get("/{file_id}", response_model=FileResponse)
async def get_file(file_id: str, current_user: dict = Depends(get_current_user)):
    """Get file metadata"""
    engine = get_engine_service()
    try:
        file_uuid = UUID(file_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid file ID")

    file_record = await engine.file_service.get(file_uuid)
    if not file_record:
        raise HTTPException(status_code=404, detail="File not found")
    if file_record.org_id != current_user["org_id"]:
        raise HTTPException(status_code=403, detail="Access denied")

    return FileResponse(**file_record.to_dict())

@router.get("/{file_id}/download")
async def download_file(file_id: str, current_user: dict = Depends(get_current_user)):
    """Download file binary data"""
    engine = get_engine_service()
    try:
        file_uuid = UUID(file_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid file ID")

    # Check access
    file_record = await engine.file_service.get(file_uuid)
    if not file_record:
        raise HTTPException(status_code=404, detail="File not found")
    if file_record.org_id != current_user["org_id"]:
        raise HTTPException(status_code=403, detail="Access denied")

    try:
        data, record = await engine.file_service.download(file_uuid)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="File data not found in storage")

    # HTTP-заголовки кодируются как latin-1, кириллица в filename вызывает
    # UnicodeEncodeError. RFC 6266 / 5987: UTF-8 имя кодируется percent-escape
    # в filename*. ASCII-fallback в filename для старых клиентов.
    fname = record.original_filename or "file"
    ascii_fallback = fname.encode("ascii", errors="ignore").decode("ascii") or "file"
    encoded = urlquote(fname, safe="")
    content_disposition = (
        f"attachment; filename=\"{ascii_fallback}\"; "
        f"filename*=UTF-8''{encoded}"
    )
    return Response(
        content=data,
        media_type=CONTENT_TYPES.get(record.file_type, "application/octet-stream"),
        headers={"Content-Disposition": content_disposition},
    )

@router.delete("/{file_id}")
async def delete_file(file_id: str, current_user: dict = Depends(get_current_user)):
    """Soft-delete a file"""
    engine = get_engine_service()
    try:
        file_uuid = UUID(file_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid file ID")

    file_record = await engine.file_service.get(file_uuid)
    if not file_record:
        raise HTTPException(status_code=404, detail="File not found")
    if file_record.org_id != current_user["org_id"]:
        raise HTTPException(status_code=403, detail="Access denied")

    await engine.file_service.delete(file_uuid)
    return {"success": True, "message": "File deleted"}

@router.post("/{file_id}/index", response_model=FileResponse)
async def index_file_for_rag(
    file_id: str,
    current_user: dict = Depends(get_current_user),
):
    """Owner triggers RAG indexing for an already-uploaded file.

    Idempotent: if file is already pending/indexing/indexed, returns its current state
    without re-enqueuing.
    """
    engine = get_engine_service()
    try:
        file_uuid = UUID(file_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid file ID")

    try:
        updated, _future = await engine.file_service.index_for_rag(
            file_id=file_uuid,
            requesting_user_id=current_user["user_id"],
            requesting_org_id=current_user["org_id"],
            requesting_is_admin=bool(current_user.get("is_admin")),
        )
        return FileResponse(**updated.to_dict())
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except PermissionError as e:
        raise HTTPException(status_code=403, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

@router.post("/{file_id}/clone", response_model=FileResponse)
async def clone_file(
    file_id: str,
    current_user: dict = Depends(get_current_user),
):
    """«Add to my files»: create a metadata-only clone of a chat-attached file.

    Permission: requesting user must be a participant of at least one chat in
    which this file appears as an attachment.
    """
    engine = get_engine_service()
    try:
        src_uuid = UUID(file_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid file ID")

    has_access = await engine.chat_service.user_can_access_attached_file(
        user_id=current_user["user_id"],
        file_id=src_uuid,
        org_id=current_user["org_id"],
    )
    if not has_access:
        raise HTTPException(status_code=403, detail="No access to this file")

    try:
        cloned = await engine.file_service.clone(
            source_file_id=src_uuid,
            requesting_user_id=current_user["user_id"],
            org_id=current_user["org_id"],
        )
        return FileResponse(**cloned.to_dict())
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

@router.get("/{file_id}/rag-status")
async def get_rag_status(file_id: str, current_user: dict = Depends(get_current_user)):
    """Get RAG indexing status for a file."""
    engine = get_engine_service()
    try:
        file_uuid = UUID(file_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid file ID")

    file_record = await engine.file_service.get(file_uuid)
    if not file_record:
        raise HTTPException(status_code=404, detail="File not found")
    if file_record.org_id != current_user["org_id"]:
        raise HTTPException(status_code=403, detail="Access denied")

    return {
        "file_id": str(file_record.id),
        "rag_status": file_record.rag_status,
        "rag_error": file_record.rag_error,
        "indexed_at": file_record.indexed_at.isoformat() if file_record.indexed_at else None,
    }

@router.patch("/{file_id}/public", response_model=FileResponse)
async def set_file_public(
    file_id: str,
    is_public: bool = Body(..., embed=True),
    current_user: dict = Depends(get_current_user),
):
    """Set or clear the is_public flag. Only the file owner can change this."""
    engine = get_engine_service()
    try:
        file_uuid = UUID(file_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid file ID")

    file_record = await engine.file_service.get(file_uuid)
    if not file_record:
        raise HTTPException(status_code=404, detail="File not found")
    if not current_user.get("is_admin"):
        raise HTTPException(status_code=403, detail="Only admins can change file visibility")
    if str(file_record.user_id) != str(current_user["user_id"]):
        raise HTTPException(status_code=403, detail="Only the file owner can change visibility")

    updated = await engine.file_service.change_public(file_uuid, is_public)
    if not updated:
        raise HTTPException(status_code=404, detail="File not found")
    return FileResponse(**updated.to_dict())


class MoveFileBody(BaseModel):
    folder_id: Optional[str] = None


@router.patch("/{file_id}/folder", response_model=FileResponse)
async def move_file_to_folder(
    file_id: str,
    body: MoveFileBody,
    current_user: dict = Depends(get_current_user),
):
    """Move file to a different folder. body.folder_id=null → move to root."""
    engine = get_engine_service()
    try:
        file_uuid = UUID(file_id)
    except ValueError:
        raise HTTPException(status_code=422, detail="Invalid file ID")

    file_record = await engine.file_service.get(file_uuid)
    if not file_record:
        raise HTTPException(status_code=404, detail="File not found")
    if file_record.org_id != current_user["org_id"]:
        raise HTTPException(status_code=403, detail="Access denied")

    new_folder_uuid: Optional[UUID] = None
    if body.folder_id:
        try:
            new_folder_uuid = UUID(body.folder_id)
        except ValueError:
            raise HTTPException(status_code=422, detail="Invalid folder_id")
        try:
            await engine.folder_service.verify_folder_owner(
                folder_id=new_folder_uuid,
                user_id=file_record.user_id,
                org_id=file_record.org_id,
            )
        except FolderError as e:
            raise HTTPException(
                status_code={"FOLDER_NOT_FOUND": 404, "INVALID_PARENT_OWNER": 400}.get(e.code, 400),
                detail={"code": e.code, "message": e.message},
            )

    try:
        moved = await engine.file_service.move_to_folder(
            file_id=file_uuid,
            new_folder_id=new_folder_uuid,
            actor_user_id=current_user["user_id"],
            actor_is_admin=current_user.get("is_admin", False),
            actor_org_id=current_user["org_id"],
        )
    except PermissionError as e:
        raise HTTPException(status_code=403, detail=str(e))
    if not moved:
        raise HTTPException(status_code=404, detail="File not found")
    return FileResponse(**moved.to_dict())
