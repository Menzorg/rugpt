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

from ...models.rag import RelatedDoc
from ...models.user_file import UserFile
from ...services.rag_service import RAGService
from ...storage.user_file_storage import UserFileStorage

logger = logging.getLogger("rugpt.agents.tools.document")

_MAX_RESULTS = 30
_SUMMARY_CHARS_BUDGET = 3000  # with 30 docs each gets at least 100 chars of summary

_user_file_storage: Optional[UserFileStorage] = None
_rag_service: Optional[RAGService] = None


def init_document_service(
    storage: UserFileStorage,
    rag_service: Optional[RAGService] = None,
) -> None:
    """Set the shared UserFileStorage and RAGService for all document tool calls."""
    global _user_file_storage, _rag_service
    _user_file_storage = storage
    _rag_service = rag_service
    logger.info("Document tool storage initialized")


def _format_user_file(f: UserFile, summary_max_chars: int) -> str:
    if f.rag_status == "indexed" and f.summary:
        s = f.summary[:summary_max_chars]
        if len(f.summary) > summary_max_chars:
            s += "..."
        summary_part = f'summary: "{s}"'
    else:
        summary_part = "summary: —"
    return (
        f"- {f.original_filename} (id={f.id}, created_at={f.created_at}, rag={f.rag_status}, "
        f"is_table={f.is_table}, size={f.file_size / 1_000_000:.2f}MB, {summary_part})"
    )



@tool(response_format="content")
async def list_documents(
    config: Annotated[RunnableConfig, InjectedToolArg],
    name_query: Optional[str] = None,
    summary_query: Optional[str] = None,
) -> str:
    """List documents visible to the caller in their organization.
    Use when the user asks what files are available, to browse the catalog,
    or before calling rag_search to check if the needed document exists.
    Args:
        name_query: Substring filter on filename, case-insensitive. Omit or pass empty to skip.
        summary_query: Vector search query on document summaries. Omit or pass empty to skip.
        If any query is provided, only search results are returned (merged and deduplicated).
        If both are omitted, the full document list is returned.
    """
    logger.info(f"tool list_documents: name_query={name_query!r}, summary_query={summary_query!r}")

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

        has_name_query = bool(name_query and name_query.strip())
        has_summary_query = bool(summary_query and summary_query.strip())

        all_files = await _user_file_storage.list_by_org(org_id)
        visible: list[UserFile] = [
            f for f in all_files
            if f.uploaded_by_user_id == user_id or f.is_public
        ]

        if not visible:
            return "No documents in your scope."

        # --- Search mode: one or both queries provided ---
        if has_name_query or has_summary_query:
            matched_ids: set[str] = set()

            if has_name_query:
                q = name_query.strip().lower()
                for f in visible:
                    if q in (f.original_filename or "").lower():
                        matched_ids.add(str(f.id))

            if has_summary_query:
                if _rag_service is None:
                    return "list_documents: summary_query unavailable (RAG service not initialized)."
                docs: list[RelatedDoc] = await _rag_service.find_docs(
                    org_id=org_id_str,
                    user_id=user_id_str,
                    query=summary_query.strip(),
                    top_k=_MAX_RESULTS,
                )
                for d in docs:
                    matched_ids.add(d.file_id)

            results: list[UserFile] = [f for f in visible if str(f.id) in matched_ids][:_MAX_RESULTS]

            if not results:
                return "No documents matched your query."

            summary_max_chars = max(1, _SUMMARY_CHARS_BUDGET // len(results))
            lines = [_format_user_file(f, summary_max_chars) for f in results]
            return f"Documents found ({len(lines)}):\n" + "\n".join(lines)

        # --- List mode: no queries ---
        total = len(visible)
        visible = visible[:_MAX_RESULTS]
        summary_max_chars = max(1, _SUMMARY_CHARS_BUDGET // len(visible))

        lines = [_format_user_file(f, summary_max_chars) for f in visible]
        more = f" (showing first {_MAX_RESULTS})" if total > _MAX_RESULTS else ""
        return f"Documents ({total} total{more}):\n" + "\n".join(lines)

    except Exception as e:
        logger.error(f"list_documents failed: {e}")
        return f"Failed to list documents: {e}"
