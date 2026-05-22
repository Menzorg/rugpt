"""
RAG Tool

LangChain tool for hybrid chunk search within a specific document.
Delegates all DB access to RAGService.

org_id and caller identity are injected via RunnableConfig — LLM sees only query and file_id.

Service lifecycle: call init_rag_service(service) once during engine startup.
"""

from src.engine.unified_logger import get_logger
from typing import Optional
from uuid import UUID

from pydantic import BaseModel, Field
from langchain_core.tools import StructuredTool
from langchain_core.runnables import RunnableConfig
from langgraph.prebuilt import ToolRuntime

from ..runtime import RagSearchRuntimeData, RuntimeContext
from ...services.rag_service import RAGService
from ...storage.user_file_storage import UserFileStorage
from ...utils.token_counter import count_tokens


class RagSearchInput(BaseModel):
    file_id: str = Field(description="Document ID to search within.")
    query: str = Field(description="Search query in Russian or English.")

logger = get_logger("agents")
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


def _resolve_tool_identity(configurable: dict) -> tuple[str, str, bool]:
    caller_user_id = configurable.get("caller_user_id", "")
    org_id = configurable.get("org_id", "")
    # callee_user_id == caller_user_id in direct calls (always set by executor).
    callee_user_id = configurable.get("callee_user_id", "")
    public_only_owner = bool(callee_user_id and callee_user_id != caller_user_id)
    return callee_user_id or caller_user_id, org_id, public_only_owner


async def _can_access_file(
    file_id: str,
    org_id: str,
    owner_user_id: str,
    public_only_owner: bool,
    is_admin: bool = False,
) -> bool:
    """Return True when the caller can see file_id in their org.

    When another user calls an owner by mention, that owner's private docs stay hidden.
    """
    if _user_file_storage is None:
        logger.error("rag_search: file storage not initialized for access check")
        return False

    try:
        org_uuid = UUID(org_id)
        owner_uuid = UUID(owner_user_id)
        file_uuid = UUID(file_id)
    except ValueError:
        return False

    all_files = await _user_file_storage.list_by_org(org_uuid)
    for f in all_files:
        if f.id != file_uuid:
            continue
        if f.user_id == owner_uuid:
            if public_only_owner:
                return f.is_public
            return True
        return is_admin or f.is_public
    return False

def _top_k_for_seen_chunks(seen_count: int) -> int:
    #if seen_count > 30:
    #    return 3
    if seen_count > 15:
        return 3
    return _DEFAULT_TOP_K

def _remember_seen_chunks(runtime_data: object, chunks: list) -> None:
    if isinstance(runtime_data, RagSearchRuntimeData):
        runtime_data.chunk_ids.update(str(chunk.chunk_id) for chunk in chunks)

async def _rag_search(
    file_id: str,
    query: str,
    config: RunnableConfig,
    runtime: ToolRuntime[RuntimeContext],
) -> str:
    try:
        configurable = config.get("configurable", {})
        user_id, org_id, public_only_owner = _resolve_tool_identity(configurable)
        is_admin = bool(configurable.get("is_admin", False))

        logger.info(
            "rag_search start: file_id=%s query=%r org_id=%s user_id=%s public_only_owner=%s is_admin=%s",
            file_id, query, org_id, user_id, public_only_owner, is_admin,
        )

        if not org_id or not user_id:
            logger.error("rag_search: missing org_id or user_id in config")
            return "RAG search unavailable: missing context."

        if _rag_service is None:
            logger.error("rag_search: service not initialized, call init_rag_service() at startup")
            return "RAG search unavailable: service not initialized."

        can_access = await _can_access_file(file_id, org_id, user_id, public_only_owner, is_admin)
        logger.info(
            "rag_search access: file_id=%s allowed=%s user_id=%s public_only_owner=%s is_admin=%s",
            file_id, can_access, user_id, public_only_owner, is_admin,
        )
        if not can_access:
            return "You don't have access to that document."

        file_uuid = UUID(file_id)
        doc = await _user_file_storage.get_by_id(file_uuid)
        if doc is None:
            logger.info(f"rag_search: document not found for file_id='{file_id}'")
            return "Document not found."
        
        file_status = await _user_file_storage.get_status(file_uuid)
        logger.info(
            "rag_search document: file_id=%s filename=%r status=%s is_table=%s",
            file_id, doc.original_filename, file_status, doc.is_table,
        )
        if file_status != "indexed":
            return f"FILE IS NOT INDEXED. CURRENT STATUS: {file_status}"

        async with runtime.context.lock:
            # Read shared run state only long enough to choose this search size.
            runtime_data = runtime.context.rag_search_runtime_data
            seen_count = (
                len(runtime_data.chunk_ids)
                if isinstance(runtime_data, RagSearchRuntimeData)
                else 0
            )

            # Block search if the cumulative RAG token budget is exhausted.
            if runtime.context.total_tokens_spent >= runtime.context.critical_tokens_cap:
                logger.info(
                    "rag_search: blocked for file_id=%s — total_tokens_spent=%d >= %d",
                    file_id, runtime.context.total_tokens_spent, runtime.context.critical_tokens_cap,
                )
                doc_name = doc.original_filename or file_id
                return (
                    f"[RAG SEARCH IS BLOCKED TO PREVENT CONTEXT WINDOW EXPLOSION. "
                    f"USE WHAT YOU'VE GOT ALREADY AND TELL USER THAT YOU NEED ONE MORE RUN TO SEARCH {doc_name}]"
                )

            top_k = _top_k_for_seen_chunks(seen_count)
            tokens_before = runtime.context.total_tokens_spent
            critical_cap = runtime.context.critical_tokens_cap

        logger.info(
            "rag_search execute: file_id=%s query=%r seen_chunks=%d top_k=%d tokens_before=%d cap=%d",
            file_id, query, seen_count, top_k, tokens_before, critical_cap,
        )

        # RAG search may hit storage/vector backends, so keep it outside the runtime lock.
        chunks = await _rag_service.search_concrete_in_doc(
            file_id=file_id,
            query=query,
            top_k=top_k,
        )

        if not chunks:
            logger.info(
                "rag_search done: file_id=%s query=%r chunks=0 top_k=%d tokens_before=%d",
                file_id, query, top_k, tokens_before,
            )
            return f"No relevant content found in '{doc.original_filename or file_id}'."

        lines = [f"## {doc.original_filename or file_id}"]
        for chunk in chunks:
            idx = f"chunk_index={chunk.chunk_index}" if chunk.chunk_index else ""
            lines.append(f"\n[{chunk.source_type}, {idx}] {chunk.chunk_text}")

        result = "\n".join(lines)
        spent = count_tokens(result)

        async with runtime.context.lock:
            # Re-check the cap before committing output because parallel tools may have spent tokens.
            if runtime.context.total_tokens_spent >= runtime.context.critical_tokens_cap:
                logger.info(
                    "rag_search: blocked after search for file_id=%s — total_tokens_spent=%d >= %d",
                    file_id, runtime.context.total_tokens_spent, runtime.context.critical_tokens_cap,
                )
                doc_name = doc.original_filename or file_id
                return (
                    f"[RAG SEARCH IS BLOCKED TO PREVENT CONTEXT WINDOW EXPLOSION. "
                    f"USE WHAT YOU'VE GOT ALREADY AND TELL USER THAT YOU NEED ONE MORE RUN TO SEARCH {doc_name}]"
                )

            runtime_data = runtime.context.rag_search_runtime_data
            _remember_seen_chunks(runtime_data, chunks)
            runtime.context.total_tokens_spent += spent
            logger.info(
                "rag_search done: file_id=%s query=%r chunks=%d chunk_indexes=%s top_k=%d output_tokens=%d tokens_before=%d tokens_after=%d",
                file_id,
                query,
                len(chunks),
                [chunk.chunk_index for chunk in chunks],
                top_k,
                spent,
                tokens_before,
                runtime.context.total_tokens_spent,
            )
            return result
    except Exception as e:
        logger.error(f"rag_search failed: {e}", exc_info=True)
        if isinstance(e, ValueError) and "badly formed hexadecimal UUID string" in str(e):
            return f"Invalid UUID in input: {e}"
        return _TOOL_ERROR_RESULT


rag_search = StructuredTool.from_function(
    coroutine=_rag_search,
    name="rag_search",
    description=(
        "Search for relevant chunks within a specific document. "
        "Use list_documents or list_own_documents first to find the document ID, then call this tool."
    ),
    args_schema=RagSearchInput,
    response_format="content",
)
