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
from langgraph.prebuilt import ToolRuntime

from ..runtime import RagSearchRuntimeData, RuntimeContext
from ...services.rag_service import RAGService
from ...storage.user_file_storage import UserFileStorage
from ...utils.token_counter import count_tokens

logger = logging.getLogger("rugpt.agents.tools.rag")
_TOOL_ERROR_RESULT = "Tool execution caused errors. No result"
_DEFAULT_TOP_K = 4

_rag_service: Optional[RAGService] = None
_user_file_storage: Optional[UserFileStorage] = None


def init_rag_service(service: RAGService, file_storage: Optional[UserFileStorage] = None) -> None:
    """Set the shared RAGService instance for all RAG tool calls."""
    global _rag_service, _user_file_storage
    _rag_service = service
    _user_file_storage = file_storage
    logger.info("RAG tool service initialized")


async def _can_access_file(file_id: str, org_id: str, user_id: str, is_admin: bool = False) -> bool:
    """Return True when the caller can see file_id in their org.

    Admins bypass ownership and public-flag checks — they can access any file in the org.
    """
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
        f.id == file_uuid and (is_admin or f.uploaded_by_user_id == user_uuid or f.is_public)
        for f in all_files
    )


def _top_k_for_seen_chunks(seen_count: int) -> int:
    #if seen_count > 30:
    #    return 3
    if seen_count > 15:
        return 3
    return _DEFAULT_TOP_K


def _remember_seen_chunks(runtime_data: object, chunks: list) -> None:
    if isinstance(runtime_data, RagSearchRuntimeData):
        runtime_data.chunk_ids.update(str(chunk.chunk_id) for chunk in chunks)


@tool(response_format="content")
async def rag_search(
    file_id: str,
    query: str,
    config: Annotated[RunnableConfig, InjectedToolArg],
    runtime: ToolRuntime[RuntimeContext],
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
        is_admin = bool(configurable.get("is_admin", False))

        logger.info(f"rag_search called: file_id={file_id}, query={query}, org_id={org_id}, user_id={user_id}, is_admin={is_admin}")

        if not org_id or not user_id:
            logger.error("rag_search: missing org_id or user_id in config")
            return "RAG search unavailable: missing context."

        if _rag_service is None:
            logger.error("rag_search: service not initialized, call init_rag_service() at startup")
            return "RAG search unavailable: service not initialized."

        if not await _can_access_file(file_id, org_id, user_id, is_admin):
            return "You don't have access to that document."


        file_uuid = UUID(file_id)
        doc = await _user_file_storage.get_by_id(file_uuid)
        if doc is None:
            logger.info(f"rag_search: document not found for file_id='{file_id}'")
            return "Document not found."
        
        file_status = await _user_file_storage.get_status(file_uuid)
        if file_status != "indexed":
            return f"FILE IS NOT INDEXED. CURRENT STATUS: {file_status}"

        runtime_data = runtime.context.rag_search_runtime_data
        seen_count = (
            len(runtime_data.chunk_ids)
            if isinstance(runtime_data, RagSearchRuntimeData)
            else 0
        )

        # Block search if the cumulative RAG token budget is exhausted.
        if runtime.context.rag_spent_tokens >= 25000:
            logger.info(
                "rag_search: blocked for file_id=%s — rag_spent_tokens=%d >= 25000",
                file_id, runtime.context.rag_spent_tokens,
            )
            doc_name = doc.original_filename or file_id
            return (
                f"[RAG SEARCH IS BLOCKED TO PREVENT CONTEXT WINDOW EXPLOSION. "
                f"USE WHAT YOU'VE GOT ALREADY AND TELL USER THAT YOU NEED ONE MORE RUN TO SEARCH {doc_name}]"
            )

        top_k = _top_k_for_seen_chunks(seen_count)

        chunks = await _rag_service.search_concrete_in_doc(
            file_id=file_id,
            query=query,
            top_k=top_k,
        )

        if not chunks:
            return f"No relevant content found in '{doc.original_filename or file_id}'."

        _remember_seen_chunks(runtime_data, chunks)

        lines = [f"## {doc.original_filename or file_id}"]
        for chunk in chunks:
            idx = f"chunk_index={chunk.chunk_index}" if chunk.chunk_index else ""
            lines.append(f"\n[{chunk.source_type}, {idx}] {chunk.chunk_text}")

        result = "\n".join(lines)
        spent = count_tokens(result)
        runtime.context.rag_spent_tokens += spent
        logger.info(
            "rag_search: returned %d chunks for file_id=%s (seen_chunks=%d, top_k=%d, tokens=%d, rag_spent_tokens=%d)",
            len(chunks), file_id, seen_count, top_k, spent, runtime.context.rag_spent_tokens,
        )
        return result
    except Exception as e:
        logger.error(f"rag_search failed: {e}", exc_info=True)
        return _TOOL_ERROR_RESULT
