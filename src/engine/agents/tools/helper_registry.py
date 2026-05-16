"""
Helper Registry

Hardcoded mapping of helper names to their tool lists.
Add or remove helpers here directly.
Prompts live in prompts/helpers/<name>.md.
"""
import logging
from typing import Dict, List, Optional, Tuple
from langchain_core.tools import BaseTool

from .registry import ToolRegistry

logger = logging.getLogger("rugpt.agents.tools.helper_registry")

# ---------------------------------------------------------------------------
# Hardcoded helpers: name → tool name list
# Add new helpers here.
# ---------------------------------------------------------------------------
def _declare_helpers() -> Dict[str, List[str]]:
    return {
        "doc_search_helper": ["list_own_documents", "list_documents", "rag_search", "expand_chunk_context", "table_rows_search"],
    }


class HelperRegistry:
    """
    Read-only registry of helpers built from the hardcoded _build_helpers() map.
    Resolves tool names to BaseTool instances via ToolRegistry at construction time.

    Usage:
        registry = HelperRegistry(tool_registry)
        tools, doc = registry.get_tools("my_helper")
    """

    def __init__(self, tool_registry: ToolRegistry):
        raw = _declare_helpers()
        self._helpers: Dict[str, Tuple[List[BaseTool], str]] = {}
        for name, tool_names in raw.items():
            tools, doc = tool_registry.resolve(tool_names)
            self._helpers[name] = (tools, doc)
            logger.info("Loaded helper: %s (%d tools)", name, len(tools))

    def get_tools(self, name: str) -> Optional[Tuple[List[BaseTool], str]]:
        """Return (tools, tools_doc) for a helper, or None if not known."""
        return self._helpers.get(name)

    @property
    def available_helpers(self) -> List[str]:
        """List all helper names."""
        return list(self._helpers.keys())

    def __len__(self) -> int:
        return len(self._helpers)
