"""
Document Tools

LangChain tool for listing documents available to an agent's initiator.

Visibility model mirrors RAG / /files endpoint: caller sees their own files
plus `is_public` files within the same org. No department visibility here —
file ownership is the access gate.

Async `StructuredTool.from_function(coroutine=...)` — invoked directly in
the running event loop alongside asyncpg pool.
"""
import logging
from typing import Annotated
from uuid import UUID

from langchain_core.runnables import RunnableConfig
from langchain_core.tools import StructuredTool, InjectedToolArg
from pydantic import BaseModel, Field

logger = logging.getLogger("rugpt.agents.tools.document")

_MAX_RESULTS = 30
_SUMMARY_CHARS_BUDGET = 3000 # in case of 30 documents, all have at least 100 chars of summary


class ListDocumentsInput(BaseModel):
    name_query: str = Field(
        default="",
        description="Optional substring filter on original filename (case-insensitive). Empty = all visible documents.",
    )


def create_document_tools(user_file_storage):
    """Create document tools wired to a UserFileStorage instance.

    Returns (list_documents_tool,).
    """

    async def _list_documents_async(
        name_query: str = "",
        config: Annotated[RunnableConfig, InjectedToolArg] = None,
    ) -> str:
        """List documents available to the caller.

        Args:
            name_query: Substring filter on filename, case-insensitive. Empty = all.
        """
        logger.info(f"tool list_documents: name_query={name_query!r}")
        try:
            configurable = (config or {}).get("configurable", {})
            user_id_str = configurable.get("user_id", "")
            org_id_str = configurable.get("org_id", "")
            if not user_id_str or not org_id_str:
                return "list_documents unavailable: missing context."

            user_id = UUID(user_id_str)
            org_id = UUID(org_id_str)

            all_files = await user_file_storage.list_by_org(org_id)
            # Visibility: uploaded by caller OR is_public.
            visible = [
                f for f in all_files
                if f.uploaded_by_user_id == user_id or f.is_public
            ]

            if name_query:
                q = name_query.lower()
                visible = [
                    f for f in visible
                    if q in (f.original_filename or "").lower()
                ]

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

    list_tool = StructuredTool.from_function(
        coroutine=_list_documents_async,
        name="list_documents",
        description=(
            "List documents visible to the caller in their organization. "
            "Use when the user asks what files are available, to browse the catalog, "
            "or before calling rag_search to check if the needed document exists."
        ),
        args_schema=ListDocumentsInput,
    )

    return (list_tool,)
