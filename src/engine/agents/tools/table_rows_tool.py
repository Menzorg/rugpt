"""
Table Rows Tool

LangChain tool for fetching table rows from a document by row index range.
Delegates all DB access to RAGService.

org_id and user_id are injected via RunnableConfig — LLM sees only file_id,
row_start, row_end.

Service lifecycle: call init_table_rows_service(service, file_storage) once
during engine startup.
"""
import logging
from typing import Annotated, Optional
from uuid import UUID

from langchain_core.tools import tool, InjectedToolArg
from langchain_core.runnables import RunnableConfig

from ...services.rag_service import RAGService
from ...storage.user_file_storage import UserFileStorage

logger = logging.getLogger("rugpt.agents.tools.table_rows")

_rag_service: Optional[RAGService] = None
_user_file_storage: Optional[UserFileStorage] = None


def init_table_rows_service(
    service: RAGService,
    file_storage: Optional[UserFileStorage] = None,
) -> None:
    """Set the shared RAGService and UserFileStorage for table rows tool calls."""
    global _rag_service, _user_file_storage
    _rag_service = service
    _user_file_storage = file_storage
    logger.info("Table rows tool service initialized")


async def _can_access_file(file_id: str, org_id: str, user_id: str) -> bool:
    if _user_file_storage is None:
        logger.error("table_rows_search: file storage not initialized")
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
async def table_rows_search(
    file_id: str,
    row_start: int,
    row_end: int,
    config: Annotated[RunnableConfig, InjectedToolArg],
) -> str:
    """Fetch rows from a table document by row index range. Maximum 50 rows returned per call.
    Args:
        file_id: Document ID of the table file.
        row_start: First row index to include (inclusive).
        row_end: Last row index to include (inclusive). Capped so at most 50 rows are returned.
    """
    _MAX_ROWS = 50

    configurable = config.get("configurable", {})
    org_id = configurable.get("org_id", "")
    user_id = configurable.get("user_id", "")

    row_end = min(row_end, row_start + _MAX_ROWS - 1)

    logger.info(
        "table_rows_search called: file_id=%s, row_start=%d, row_end=%d, org_id=%s, user_id=%s",
        file_id, row_start, row_end, org_id, user_id,
    )

    if not org_id or not user_id:
        logger.error("table_rows_search: missing org_id or user_id in config")
        return "Table rows search unavailable: missing context."

    if _rag_service is None:
        logger.error("table_rows_search: service not initialized")
        return "Table rows search unavailable: service not initialized."

    if not await _can_access_file(file_id, org_id, user_id):
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
