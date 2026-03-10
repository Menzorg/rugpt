from __future__ import annotations

from fastapi import FastAPI, File, HTTPException, Query, UploadFile, APIRouter

from ..models.ragsearch import ChunkSearchResult, RelatedDocSearchResult
from ..services.rag_service import RAGService
from ..storage.rag_store import RAG_store

from fastapi import APIRouter, HTTPException, Depends
import engine.services.engine_service as engine_service

router = APIRouter(prefix="/rag", tags=["rag"])

engine = engine_service.EngineService.get_instance()

rag = engine.rag_service

# FOR DEBUG ONLY

@router.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@router.post("/rag/ingest")
async def ingest_doc(
    org_id: str = Query(..., min_length=1),
    user_id: str = Query(..., min_length=1),
    file: UploadFile = File(...),
) -> dict[str, str | int | bool]:
    data = await file.read()
    try:
        return await rag.ingest(
            filename=file.filename,
            content_type=file.content_type,
            data=data,
            org_id=org_id,
            user_id=user_id,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Ingestion failed: {exc}") from exc


@router.delete("/rag/{doc_id}")
async def delete_doc(doc_id: str) -> dict[str, str]:
    try:
        deleted = await store.delete_document(doc_id=doc_id)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Database delete failed: {exc}") from exc

    if not deleted:
        raise HTTPException(status_code=404, detail="Document not found.")

    return {"status": "deleted", "doc_id": doc_id}


@router.get("/rag/find")
async def find_docs(
    query: str = Query(..., min_length=1),
    top_k: int = Query(5, gt=0),
    org_id: str = Query(..., min_length=1),
    user_id: str = Query(..., min_length=1),
) -> list[RelatedDocSearchResult]:
    try:
        return await rag.find_docs(
            org_id=org_id,
            user_id=user_id,
            query=query,
            top_k=top_k,
        )
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Find docs failed: {exc}") from exc


@router.get("/rag/{doc_id}/search/abstract")
async def search_abstract_in_doc(
    doc_id: str,
    query: str = Query(..., min_length=1),
    top_k: int = Query(5, gt=0),
) -> list[ChunkSearchResult]:
    try:
        return await rag.search_abstract_in_doc(
            doc_id=doc_id,
            query=query,
            top_k=top_k,
        )
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Abstract chunk search failed: {exc}") from exc


@router.get("/rag/{doc_id}/search/concrete")
async def search_concrete_in_doc(
    doc_id: str,
    query: str = Query(..., min_length=1),
    top_k: int = Query(5, gt=0),
    tsv_weight: float = Query(1.0, gt=0.0),
) -> list[ChunkSearchResult]:
    try:
        return await rag.search_concrete_in_doc(
            doc_id=doc_id,
            query=query,
            top_k=top_k,
            tsv_weight=tsv_weight,
        )
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Concrete chunk search failed: {exc}") from exc
