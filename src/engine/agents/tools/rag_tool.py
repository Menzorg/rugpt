"""
RAG Tool

LangChain tool for hybrid document search (vector + TSV rank fusion).
Delegates all DB access to RAGService.

org_id and user_id are injected via RunnableConfig — LLM sees only `query`.

Service lifecycle: call init_rag_service(service) once during engine startup.
"""
import logging
from typing import Annotated, Optional
from uuid import UUID

from langchain_core.tools import tool, InjectedToolArg
from langchain_core.runnables import RunnableConfig

from ...services.rag_service import RAGService
from ...storage.user_file_storage import UserFileStorage

logger = logging.getLogger("rugpt.agents.tools.rag")

# Shared service set once during engine startup via init_rag_service()
_rag_service: Optional[RAGService] = None
_user_file_storage: Optional[UserFileStorage] = None


def init_rag_service(service: RAGService, file_storage: Optional[UserFileStorage] = None) -> None:
    """Set the shared RAGService instance for all RAG tool calls."""
    global _rag_service, _user_file_storage
    _rag_service = service
    _user_file_storage = file_storage
    logger.info("RAG tool service initialized")


async def _can_access_file(file_id: str, org_id: str, user_id: str) -> bool:
    """Return True when the caller can see file_id in their org."""
    if _user_file_storage is None:
        logger.error("rag_search: file storage not initialized for access check")
        return False

    try:
        org_uuid = UUID(org_id)
        user_uuid = UUID(user_id)
        file_uuid = UUID(file_id)
    except ValueError:
        return False

    all_files = await _user_file_storage.list_by_org(org_uuid)
    return any(
        f.id == file_uuid and (f.uploaded_by_user_id == user_uuid or f.is_public) # if file is owned by user or public, they can access it
        for f in all_files
    )


async def _search_rag_async(
    query: str,
    org_id: str,
    user_id: str,
    top_k_docs: int = 3,
    chunks_per_doc: int = 3,
    file_id: Optional[str] = None,
    doc_query: Optional[str] = None,
) -> str:
    """
    Two-stage hybrid RAG search:
    1. find_docs / get_doc_by_id — find relevant document(s)
    2. search_concrete_in_doc — retrieve chunks from each document

    If file_id is provided, skip document search and use that document directly.
    If doc_query is provided (and file_id is not), use it for the document search stage.
    """
    if _rag_service is None:
        logger.error("rag_search: service not initialized, call init_rag_service() at startup")
        return "RAG search unavailable: service not initialized."

    if file_id is not None:
        if not await _can_access_file(file_id, org_id, user_id):
            return "You don't have access to that document"

        doc = await _rag_service.get_doc_by_id(file_id)
        if doc is None:
            logger.info(f"rag_search: document not found for file_id='{file_id}'")
            return "No relevant documents found."
        docs = [doc]
    else:
        search_query = doc_query if doc_query else query
        docs = await _rag_service.find_docs(
            org_id=org_id,
            user_id=user_id,
            query=search_query,
            top_k=top_k_docs,
        )

    if not docs:
        logger.info(f"rag_search: no documents found for query='{query}'")
        return "No relevant documents found."

    results = []
    for doc in docs:
        title = doc.doc_title or "Untitled"
        chunks = await _rag_service.search_concrete_in_doc(
            file_id=doc.file_id,
            query=query,
            top_k=chunks_per_doc,
        )

        doc_block = f"## {title}\n"
        if chunks:
            for chunk in chunks:
                doc_block += f"\n[{chunk.source_type}] {chunk.chunk_text}\n"
        else:
            doc_block += "\n(no matching content)\n"

        results.append(doc_block)

    logger.info(f"rag_search: found {len(docs)} docs with chunks")
    return "\n\n---\n\n".join(results)


@tool(response_format="content")
async def rag_search(
    query: str,
    config: Annotated[RunnableConfig, InjectedToolArg],
    file_id: Optional[str] = None,
    doc_query: Optional[str] = None,
) -> str:
    """Search documents in the knowledge base.
    Args:
        query: Search query in Russian or English
        file_id: Optional document ID. If provided, searches only within that specific document, skipping the document discovery stage.
        doc_query: Optional query used to find relevant documents before searching their contents. If provided (and file_id is not), this query is used for the document search stage instead of the main query.
    """
    configurable = config.get("configurable", {})
    org_id = configurable.get("org_id", "")
    user_id = configurable.get("user_id", "")

    logger.info(f"rag_search called: query={query}, org_id={org_id}, user_id={user_id}, file_id={file_id}, doc_query={doc_query}")

    if not org_id or not user_id:
        logger.error("rag_search: missing org_id or user_id in config")
        return "RAG search unavailable: missing context."

    return await _search_rag_async(query, org_id, user_id, file_id=file_id, doc_query=doc_query)
