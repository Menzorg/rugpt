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

from langchain_core.tools import tool
from langchain_core.runnables import RunnableConfig

from ...services.rag_service import RAGService
from ...storage.chat_storage import ChatStorage
from ...storage.user_file_storage import UserFileStorage
from .util.file_access import can_access_file

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

@tool(response_format="content")
async def table_rows_search(
    file_id: str,
    row_start: int,
    row_end: int,
    config: RunnableConfig,
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
        caller_user_id = configurable["caller_user_id"]
        callee_user_id = configurable.get("callee_user_id", "")
        org_id = configurable["org_id"]
        is_admin = bool(configurable.get("is_admin", False))
        # In mention calls callee differs from caller — restrict to callee's public docs only.
        owner_user_id = callee_user_id or caller_user_id
        mention_mode = bool(callee_user_id and callee_user_id != caller_user_id)
        chat_id = configurable.get("chat_id")

        row_end = min(row_end, row_start + _MAX_ROWS - 1)

        logger.info(
            "table_rows_search called: file_id=%s, row_start=%d, row_end=%d, org_id=%s, user_id=%s, mention_mode=%s, chat_id=%s",
            file_id, row_start, row_end, org_id, owner_user_id, mention_mode, chat_id,
        )

        if _rag_service is None:
            logger.error("table_rows_search: service not initialized")
            return "Table rows search unavailable: service not initialized."

        if _user_file_storage is None:
            logger.error("table_rows_search: file storage not initialized")
            return "Table rows search unavailable: file storage not initialized."

        if not await can_access_file(
            file_id, org_id, owner_user_id, _user_file_storage,
            mention_mode=mention_mode, is_admin=is_admin,
            chat_storage=_chat_storage, chat_id=chat_id,
        ):
            return "You don't have access to that document."

        rows = await _rag_service.get_table_rows_by_range(
            file_id=file_id,
            row_start=row_start,
            row_end=row_end,
        )

        if not rows:
            return f"No rows found in file '{file_id}' between row {row_start} and {row_end}."

        lines = [f"Rows returned: {len(rows)}"]
        lines += [f"[row {row_start + i}] {r.chunk_text}" for i, r in enumerate(rows)]
        return "\n".join(lines)
    except Exception as e:
        logger.error(f"table_rows_search failed: {e}", exc_info=True)
        return _TOOL_ERROR_RESULT
