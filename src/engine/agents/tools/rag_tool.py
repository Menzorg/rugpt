"""
RAG Tool

LangChain tool for hybrid chunk search within a specific document.
Delegates all DB access to RAGService.

org_id and user_id are injected via RunnableConfig — LLM sees only query and file_id.

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
_TOOL_ERROR_RESULT = "Tool execution caused errors. No result"

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
        f.id == file_uuid and (f.uploaded_by_user_id == user_uuid or f.is_public)
        for f in all_files
    )


@tool(response_format="content")
async def rag_search(
    file_id: str,
    query: str,
    config: Annotated[RunnableConfig, InjectedToolArg],
) -> str:
    """Search for relevant chunks within a specific document.
    Use list_documents first to find the document ID, then call this tool.
    Args:
        file_id: Document ID to search within.
        query: Search query in Russian or English.
    """
    try:
        configurable = config.get("configurable", {})
        org_id = configurable.get("org_id", "")
        user_id = configurable.get("user_id", "")

        logger.info(f"rag_search called: file_id={file_id}, query={query}, org_id={org_id}, user_id={user_id}")

        if not org_id or not user_id:
            logger.error("rag_search: missing org_id or user_id in config")
            return "RAG search unavailable: missing context."

        if _rag_service is None:
            logger.error("rag_search: service not initialized, call init_rag_service() at startup")
            return "RAG search unavailable: service not initialized."

        if not await _can_access_file(file_id, org_id, user_id):
            return "You don't have access to that document."

        doc = await _rag_service.get_doc_by_id(file_id)
        if doc is None:
            logger.info(f"rag_search: document not found for file_id='{file_id}'")
            return "Document not found."

        chunks = await _rag_service.search_concrete_in_doc(
            file_id=file_id,
            query=query,
            top_k=5,
        )

        if not chunks:
            return f"No relevant content found in '{doc.doc_title or file_id}'."

        lines = [f"## {doc.doc_title or file_id}"]
        for chunk in chunks:
            idx = f"chunk_index={chunk.chunk_index}" if chunk.chunk_index else ""
            lines.append(f"\n[{chunk.source_type}, {idx}] {chunk.chunk_text}")

        logger.info(f"rag_search: returned {len(chunks)} chunks for file_id={file_id}")
        return "\n".join(lines)
    except Exception as e:
        logger.error(f"rag_search failed: {e}", exc_info=True)
        return _TOOL_ERROR_RESULT
