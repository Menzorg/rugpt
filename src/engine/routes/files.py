"""
File Management Routes

Endpoints for file upload/download/management:
- POST /files/upload — upload a file for an employee
- GET /files — list files (by user or org)
- GET /files/{file_id} — get file metadata
- GET /files/{file_id}/download — download file binary
- DELETE /files/{file_id} — soft-delete file
"""
import logging
from typing import Optional, List
from uuid import UUID

from fastapi import APIRouter, HTTPException, Depends, Query, UploadFile, File, Form
from fastapi.responses import Response
from pydantic import BaseModel

from ..services.engine_service import get_engine_service
from ..constants import CONTENT_TYPES
from .auth import get_current_user

logger = logging.getLogger("rugpt.routes.files")
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
    is_active: bool
    created_at: str
    updated_at: str


@router.post("/upload", response_model=FileResponse)
async def upload_file(
    file: UploadFile = File(...),
    user_id: str = Form(..., description="Employee UUID who owns this file"),
    current_user: dict = Depends(get_current_user),
):
    """Upload a file for an employee (manager action)"""
    engine = get_engine_service()

    try:
        user_uuid = UUID(user_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid user_id")

    data = await file.read()

    try:
        created = await engine.file_service.upload(
            org_id=current_user["org_id"],
            user_id=user_uuid,
            uploaded_by_user_id=current_user["user_id"],
            filename=file.filename or "unnamed",
            data=data,
        )
        return FileResponse(**created.to_dict())
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.get("", response_model=List[FileResponse])
async def list_files(
    user_id: Optional[str] = Query(None, description="Filter by employee UUID"),
    current_user: dict = Depends(get_current_user),
):
    """List files. Filter by user_id or get all org files (admin)."""
    engine = get_engine_service()

    if user_id:
        try:
            user_uuid = UUID(user_id)
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid user_id")
        files = await engine.file_service.list_by_user(user_uuid)
    else:
        files = await engine.file_service.list_by_org(current_user["org_id"])

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

    return Response(
        content=data,
        media_type=CONTENT_TYPES.get(record.file_type, "application/octet-stream"),
        headers={
            "Content-Disposition": f'attachment; filename="{record.original_filename}"'
        },
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
