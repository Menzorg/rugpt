"""
Table Rows Tool

LangChain tool for fetching table rows from a document by row index range.
Delegates all DB access to RAGService.

org_id and caller_user_id are injected via RunnableConfig — LLM sees only file_id,
row_start, row_end.

Service lifecycle: call init_table_rows_service(service, file_storage) once
during engine startup.
"""

from src.engine.unified_logger import get_logger
from typing import Optional
from uuid import UUID

from langchain_core.tools import tool
from langchain_core.runnables import RunnableConfig
from langgraph.prebuilt import ToolRuntime

from ..runtime import RuntimeContext
from ...services.rag_service import RAGService
from ...storage.chat_storage import ChatStorage
from ...storage.user_file_storage import UserFileStorage
from ...utils.token_counter import count_tokens

logger = get_logger("agents")
_TOOL_ERROR_RESULT = "Tool execution caused errors. No result"

_rag_service: Optional[RAGService] = None
_user_file_storage: Optional[UserFileStorage] = None
_chat_storage: Optional[ChatStorage] = None

def init_table_rows_service(
    service: RAGService,
    file_storage: Optional[UserFileStorage] = None,
    chat_storage: Optional[ChatStorage] = None,
) -> None:
    """Set the shared RAGService and UserFileStorage for table rows tool calls."""
    global _rag_service, _user_file_storage, _chat_storage
    _rag_service = service
    _user_file_storage = file_storage
    _chat_storage = chat_storage
    logger.info("Table rows tool service initialized")

async def _can_access_file(file_id: str, org_id: str, user_id: str, chat_id: Optional[str] = None) -> bool:
    if _user_file_storage is None:
        logger.error("table_rows_search: file storage not initialized")
        return False
    try:
        org_uuid = UUID(org_id)
        user_uuid = UUID(user_id)
        file_uuid = UUID(file_id)
    except ValueError:
        return False
    if chat_id and _chat_storage is not None:
        try:
            attachment_ids = await _chat_storage.get_attachments(UUID(chat_id))
            if file_uuid in attachment_ids:
                return True
        except Exception:
            pass
    all_files = await _user_file_storage.list_by_org(org_uuid)
    return any(
        f.id == file_uuid and (f.uploaded_by_user_id == user_uuid or f.is_public)
        for f in all_files
    )

@tool(response_format="content")
async def table_rows_search(
    file_id: str,
    row_start: int,
    row_end: int,
    config: RunnableConfig,
    runtime: ToolRuntime[RuntimeContext] = None,
) -> str:
    """Fetch rows from a table document by row index range. Maximum 50 rows returned per call.
    Args:
        file_id: Document ID of the table file.
        row_start: First row index to include (inclusive).
        row_end: Last row index to include (inclusive). Capped so at most 50 rows are returned.
    """
    try:
        _MAX_ROWS = 50

        configurable = config.get("configurable", {})
        org_id = configurable["org_id"]
        user_id = configurable["caller_user_id"]
        chat_id = configurable.get("chat_id")

        row_end = min(row_end, row_start + _MAX_ROWS - 1)

        logger.info(
            "tool table_rows_search: file_id=%s row_start=%d row_end=%d",
            file_id, row_start, row_end,
        )
        logger.info(
            "table_rows_search identity: caller_user_id=%s callee_user_id=%s org_id=%s is_admin=%s invocation=%s",
            configurable.get("caller_user_id", ""),
            configurable.get("callee_user_id", ""),
            configurable.get("org_id", ""),
            bool(configurable.get("is_admin", False)),
            configurable.get("invocation_kind", ""),
        )

        if _rag_service is None:
            logger.error("table_rows_search: service not initialized")
            return "Table rows search unavailable: service not initialized."

        if not await _can_access_file(file_id, org_id, user_id, chat_id):
            return "You don't have access to that document."

        if _user_file_storage is not None:
            doc = await _user_file_storage.get_by_id(UUID(file_id))
        else:
            doc = None
        doc_name = (doc.original_filename if doc else None) or file_id

        if runtime is not None:
            async with runtime.context.lock:
                tokens_before = runtime.context.total_tokens_spent
                critical_cap = runtime.context.critical_tokens_cap

            if tokens_before >= critical_cap:
                logger.info(
                    "table_rows_search: blocked for file_id=%s — total_tokens_spent=%d >= %d",
                    file_id, tokens_before, critical_cap,
                )
                return (
                    f"[TABLE ROWS SEARCH IS BLOCKED TO PREVENT CONTEXT WINDOW EXPLOSION. "
                    f"USE WHAT YOU'VE GOT ALREADY AND TELL USER THAT YOU NEED ONE MORE RUN TO SEARCH {doc_name}]"
                )

        rows = await _rag_service.get_table_rows_by_range(
            file_id=file_id,
            row_start=row_start,
            row_end=row_end,
        )

        if not rows:
            return f"No rows found in file '{file_id}' between row {row_start} and {row_end}."

        lines = [f"Rows returned: {len(rows)}"]
        lines += [f"[row {row_start + i}] {r.chunk_text}" for i, r in enumerate(rows)]
        result = "\n".join(lines)
        spent = count_tokens(result)

        if runtime is not None:
            async with runtime.context.lock:
                if runtime.context.total_tokens_spent >= runtime.context.critical_tokens_cap:
                    logger.info(
                        "table_rows_search: blocked after fetch for file_id=%s — total_tokens_spent=%d >= %d",
                        file_id, runtime.context.total_tokens_spent, runtime.context.critical_tokens_cap,
                    )
                    return (
                        f"[TABLE ROWS SEARCH IS BLOCKED TO PREVENT CONTEXT WINDOW EXPLOSION. "
                        f"USE WHAT YOU'VE GOT ALREADY AND TELL USER THAT YOU NEED ONE MORE RUN TO SEARCH {doc_name}]"
                    )
                runtime.context.total_tokens_spent += spent
                logger.info(
                    "table_rows_search done: file_id=%s rows=%d output_tokens=%d tokens_after=%d",
                    file_id, len(rows), spent, runtime.context.total_tokens_spent,
                )
        return result
    except Exception as e:
        logger.error(f"table_rows_search failed: {e}", exc_info=True)
        return _TOOL_ERROR_RESULT
