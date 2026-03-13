from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile

from ..services.engine_service import get_engine_service
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
            content_type=file.content_type,
            data=data,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Ingestion failed: {exc}") from exc


@router.delete("/docs/{doc_id}")
async def delete_doc(
    doc_id: str,
    current_user: dict = Depends(get_current_user),
) -> dict[str, str]:
    engine = get_engine_service()
    try:
        deleted = await engine.rag_store.delete_document(doc_id=doc_id)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Database delete failed: {exc}") from exc

    if not deleted:
        raise HTTPException(status_code=404, detail="Document not found.")

    return {"status": "deleted", "doc_id": doc_id}


@router.get("/docs/find")
async def find_docs(
    query: str = Query(..., min_length=1),
    top_k: int = Query(5, gt=0),
    current_user: dict = Depends(get_current_user),
) -> list[dict[str, Any]]:
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


@router.get("/docs/{doc_id}/search/abstract")
async def search_abstract_in_doc(
    doc_id: str,
    query: str = Query(..., min_length=1),
    top_k: int = Query(5, gt=0),
    current_user: dict = Depends(get_current_user),
) -> list[dict[str, Any]]:
    engine = get_engine_service()
    try:
        return await engine.rag_service.search_abstract_in_doc(
            doc_id=doc_id,
            query=query,
            top_k=top_k,
        )
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Abstract chunk search failed: {exc}") from exc


@router.get("/docs/{doc_id}/search/concrete")
async def search_concrete_in_doc(
    doc_id: str,
    query: str = Query(..., min_length=1),
    top_k: int = Query(5, gt=0),
    tsv_weight: float = Query(1.0, gt=0.0),
    current_user: dict = Depends(get_current_user),
) -> list[dict[str, Any]]:
    engine = get_engine_service()
    try:
        return await engine.rag_service.search_concrete_in_doc(
            doc_id=doc_id,
            query=query,
            top_k=top_k,
            tsv_weight=tsv_weight,
        )
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Concrete chunk search failed: {exc}") from exc
