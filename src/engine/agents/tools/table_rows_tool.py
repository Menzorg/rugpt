"""
Table Rows Tool

LangChain tool for fetching table rows from a document by row index range.
Delegates all DB access to RAGService.

org_id and caller_user_id are injected via RunnableConfig — LLM sees only file_id,
sheet_name, row_start, row_end.

Service lifecycle: call init_table_rows_service(service, file_storage) once
during engine startup.
"""

from src.engine.unified_logger import get_logger
from typing import Optional
from uuid import UUID

from langchain_core.tools import tool
from langchain_core.runnables import RunnableConfig

from ...services.rag_service import RAGService
from ...storage.chat_storage import ChatStorage
from ...storage.user_file_storage import UserFileStorage

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

async def _sheet_hint(file_id: str, prefix: str) -> str:
    """Return prefix + available sheet list, or prefix alone if none found."""
    if _rag_service is not None:
        available = await _rag_service._store.get_sheet_names(file_id)
        if available:
            return f"{prefix} Available sheets: {', '.join(available)}. Call the tool again with the correct sheet_name."
    return prefix


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
    sheet_name: str = "",
) -> str:
    """Fetch rows from a table document by sheet name and row index range. Maximum 50 rows returned per call.

    Each returned row is formatted as:
        Sheet:<sheet_name>, A:<header>=<value>, B:<header>=<value>, ...
    Column letters follow Excel convention (A, B, ..., Z, AA, ...) and correspond to
    actual spreadsheet column coordinates. Row indexes are 1-based Excel row numbers,
    so you can reference a cell as e.g. «column B, row 5» or «B5».

    Args:
        file_id: Document ID of the table file.
        sheet_name: Name of the sheet to query. Must match exactly one of the sheet names listed in the document summary under «Структура таблицы». Required — call will fail with available sheet list if omitted or left empty.
        row_start: First row index to include (inclusive). Uses 1-based Excel row numbers.
        row_end: Last row index to include (inclusive). Capped so at most 50 rows are returned.
    """
    try:
        _MAX_ROWS = 50

        configurable = config.get("configurable", {})
        org_id = configurable["org_id"]
        user_id = configurable["caller_user_id"]
        chat_id = configurable.get("chat_id")

        if not sheet_name:
            return await _sheet_hint(file_id, "sheet_name was not provided.")

        row_end = min(row_end, row_start + _MAX_ROWS - 1)

        logger.info(
            "table_rows_search called: file_id=%s, sheet=%s, row_start=%d, row_end=%d, org_id=%s, user_id=%s, chat_id=%s",
            file_id, sheet_name, row_start, row_end, org_id, user_id, chat_id,
        )

        if _rag_service is None:
            logger.error("table_rows_search: service not initialized")
            return "Table rows search unavailable: service not initialized."

        if not await _can_access_file(file_id, org_id, user_id, chat_id):
            return "You don't have access to that document."

        rows = await _rag_service.get_table_rows_by_range(
            file_id=file_id,
            row_start=row_start,
            row_end=row_end,
            sheet_name=sheet_name,
        )

        if not rows:
            return await _sheet_hint(
                file_id,
                f"No rows found for sheet '{sheet_name}' between row {row_start} and {row_end}.",
            )

        lines = [f"Rows returned: {len(rows)}"]
        lines += [
            f"[row {r.chunk_index}] {r.chunk_text}" if r.chunk_index is not None
            else f"[row index unavailable] {r.chunk_text}"
            for r in rows
        ]
        return "\n".join(lines)
    except Exception as e:
        logger.error(f"table_rows_search failed: {e}", exc_info=True)
        return _TOOL_ERROR_RESULT
