"""
Document Tools

LangChain tool for listing documents available to an agent's initiator.

Visibility model mirrors RAG / /files endpoint: caller sees their own files
plus `is_public` files within the same org.

Service lifecycle: call init_document_service(storage) once during engine startup.
"""
import logging
from typing import Annotated, Optional
from uuid import UUID

from langchain_core.runnables import RunnableConfig
from langchain_core.tools import tool, InjectedToolArg

from ...storage.user_file_storage import UserFileStorage

logger = logging.getLogger("rugpt.agents.tools.document")

_MAX_RESULTS = 30
_SUMMARY_CHARS_BUDGET = 3000  # with 30 docs each gets at least 100 chars of summary

_user_file_storage: Optional[UserFileStorage] = None


def init_document_service(storage: UserFileStorage) -> None:
    """Set the shared UserFileStorage instance for all document tool calls."""
    global _user_file_storage
    _user_file_storage = storage
    logger.info("Document tool storage initialized")


@tool(response_format="content")
async def list_documents(
    config: Annotated[RunnableConfig, InjectedToolArg],
    name_query: Optional[str] = None,
) -> str:
    """List documents visible to the caller in their organization.
    Use when the user asks what files are available, to browse the catalog,
    or before calling rag_search to check if the needed document exists.
    Args:
        name_query: Substring filter on filename, case-insensitive. Empty string = all documents.
    """
    logger.info(f"tool list_documents: name_query={name_query!r}")

    if _user_file_storage is None:
        logger.error("list_documents: storage not initialized, call init_document_service() at startup")
        return "list_documents unavailable: storage not initialized."

    try:
        configurable = config.get("configurable", {})
        user_id_str = configurable.get("user_id", "")
        org_id_str = configurable.get("org_id", "")
        if not user_id_str or not org_id_str:
            return "list_documents unavailable: missing context."

        user_id = UUID(user_id_str)
        org_id = UUID(org_id_str)

        all_files = await _user_file_storage.list_by_org(org_id)
        visible = [
            f for f in all_files
            if f.uploaded_by_user_id == user_id or f.is_public
        ]

        if name_query:
            q = name_query.lower()
            visible = [f for f in visible if q in (f.original_filename or "").lower()]

        if not visible:
            return "No documents in your scope."

        total = len(visible)
        visible = visible[:_MAX_RESULTS]
        summary_max_chars = max(1, _SUMMARY_CHARS_BUDGET // len(visible))

        lines = []
        for f in visible:
            if f.rag_status == "indexed" and f.summary:
                summary = f.summary[:summary_max_chars]
                if len(f.summary) > summary_max_chars:
                    summary += "..."
                summary_part = f'summary: "{summary}"'
            else:
                summary_part = "summary: —"
            lines.append(
                f"- {f.original_filename} (id={f.id}, rag={f.rag_status}, {summary_part})"
            )

        more = f" (showing first {_MAX_RESULTS})" if total > _MAX_RESULTS else ""
        return f"Documents ({total} total{more}):\n" + "\n".join(lines)

    except Exception as e:
        logger.error(f"list_documents failed: {e}")
        return f"Failed to list documents: {e}"
