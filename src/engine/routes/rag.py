from __future__ import annotations

import logging
from uuid import UUID

from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile

from ..models.rag import ChunkSearchResult, RelatedDoc
from ..services.engine_service import get_engine_service
from ..tasks.ingest_queue import ingest_queue
from .auth import get_current_user

logger = logging.getLogger("rugpt.routes.rag")
router = APIRouter(prefix="/rag", tags=["rag"])


@router.post("/docs/ingest")
async def ingest_doc(
    file: UploadFile = File(...),
    current_user: dict = Depends(get_current_user),
) -> dict[str, str | int | bool]:
    engine = get_engine_service()
    data = await file.read()
    try:
        return await engine.rag_service.ingest(
            org_id=current_user["org_id"],
            user_id=current_user["user_id"],
            filename=file.filename,
            data=data,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Ingestion failed: {exc}") from exc


@router.delete("/docs/{file_id}")
async def delete_doc(
    file_id: str,
    current_user: dict = Depends(get_current_user),
) -> dict[str, str]:
    engine = get_engine_service()
    try:
        deleted = await engine.rag_store.delete_document(file_id=file_id)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Database delete failed: {exc}") from exc

    if not deleted:
        raise HTTPException(status_code=404, detail="Document not found.")

    return {"status": "deleted", "file_id": file_id}


@router.post("/docs/{file_id}/retry")
async def retry_ingestion(
    file_id: str,
    current_user: dict = Depends(get_current_user),
) -> dict[str, str]:
    """Re-trigger RAG ingestion for an already-uploaded file.

    Downloads the file binary from storage, then runs try_ingest in a
    background thread (same pattern as upload) so the endpoint returns
    immediately without blocking the event loop.
    """
    engine = get_engine_service()

    try:
        file_uuid = UUID(file_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid file_id")

    data, record = await engine.file_service.download(file_uuid)
    if not record:
        raise HTTPException(status_code=404, detail="File not found")
    if str(record.org_id) != str(current_user["org_id"]):
        raise HTTPException(status_code=403, detail="Access denied")

    ingest_queue.submit(
        file_id=file_uuid,
        org_id=str(record.org_id),
        user_id=str(record.user_id),
        filename=record.original_filename,
        data=data,
    )
    return {"status": "queued", "file_id": file_id}


@router.get("/docs/find")
async def find_docs(
    query: str = Query(..., min_length=1),
    top_k: int = Query(5, gt=0),
    current_user: dict = Depends(get_current_user),
) -> list[RelatedDoc]:
    engine = get_engine_service()
    try:
        return await engine.rag_service.find_docs(
            org_id=current_user["org_id"],
            user_id=current_user["user_id"],
            query=query,
            top_k=top_k,
        )
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Find docs failed: {exc}") from exc


@router.get("/docs/{file_id}/search/abstract")
async def search_abstract_in_doc(
    file_id: str,
    query: str = Query(..., min_length=1),
    top_k: int = Query(5, gt=0),
    current_user: dict = Depends(get_current_user),
) -> list[ChunkSearchResult]:
    engine = get_engine_service()
    try:
        return await engine.rag_service.search_abstract_in_doc(
            file_id=file_id,
            query=query,
            top_k=top_k,
        )
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Abstract chunk search failed: {exc}") from exc


@router.get("/docs/{file_id}/search/concrete")
async def search_concrete_in_doc(
    file_id: str,
    query: str = Query(..., min_length=1),
    top_k: int = Query(5, gt=0),
    tsv_weight: float = Query(1.0, gt=0.0),
    current_user: dict = Depends(get_current_user),
) -> list[ChunkSearchResult]:
    engine = get_engine_service()
    try:
        return await engine.rag_service.search_concrete_in_doc(
            file_id=file_id,
            query=query,
            top_k=top_k,
            tsv_weight=tsv_weight,
        )
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Concrete chunk search failed: {exc}") from exc
