"""
list_categories tool

Fuzzy trigram search over active content types (document categories) in the org.
Uses search_content_types() SQL function from migration 053.
Empty query lists all active categories.
"""

from src.engine.unified_logger import get_logger
from typing import Optional
from uuid import UUID

from langchain_core.runnables import RunnableConfig
from langchain_core.tools import StructuredTool
from langgraph.prebuilt import ToolRuntime
from pydantic import BaseModel, Field

from ..runtime import RuntimeContext
from ...storage.content_type_storage import ContentTypeStorage

logger = get_logger("agents")
_TOOL_ERROR_RESULT = "Tool execution caused errors. No result"

_content_type_storage: Optional[ContentTypeStorage] = None


def init_list_categories_storage(storage: ContentTypeStorage) -> None:
    global _content_type_storage
    _content_type_storage = storage


class ListCategoriesInput(BaseModel):
    query: str = Field(
        default="",
        description=(
            "Name or partial name of the content type to search for. "
            "Uses fuzzy trigram matching, so typos and partial matches are fine. "
            "Omit or pass empty string to list all active categories in the organization."
        ),
    )


async def _list_categories_async(
    config: RunnableConfig,
    runtime: ToolRuntime[RuntimeContext],
    query: str = "",
) -> str:
    tool_name = "list_categories"
    try:
        if _content_type_storage is None:
            return f"{tool_name} unavailable: storage not initialized."
        configurable = (config or {}).get("configurable", {})
        org_id_str = configurable.get("org_id", "")
        org_id = UUID(org_id_str)
        query = query.strip()
        if query:
            results = await _content_type_storage.search_by_name(org_id, query)
        else:
            results = await _content_type_storage.list_by_org(org_id)
        logger.info("%s: query=%r org=%s returned=%d", tool_name, query or "<all>", org_id, len(results))
        if not results:
            return "No categories found."
        lines = [
            f"- id={ct.id}  name={ct.name!r}  description={ct.description!r}"
            for ct in results
        ]
        return "\n".join(lines)
    except Exception as e:
        logger.error(f"{tool_name} failed: {e}", exc_info=True)
        if isinstance(e, ValueError):
            return f"Invalid UUID: {e}"
        return _TOOL_ERROR_RESULT


list_categories = StructuredTool.from_function(
    coroutine=_list_categories_async,
    name="list_categories",
    description=(
        "Search active document categories (content types) in the organization by name. "
        "Uses fuzzy trigram matching — partial names and typos are fine. "
        "Omit query or pass empty string to list all active categories. "
        "Returns id, name, and description for each match. "
        "Use the returned id as category_id in list_documents to filter documents by category."
    ),
    args_schema=ListCategoriesInput,
)
