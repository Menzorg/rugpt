"""
Tool Registry

Central registry mapping tool names to LangChain tool functions.
Tools are registered at startup; agents resolve them by name from role.tools list.
"""
import logging
from pathlib import Path
from typing import Dict, List, Optional
from langchain_core.tools import BaseTool

logger = logging.getLogger("rugpt.agents.tools.registry")

_TOOLS_DOCS_DIR = Path(__file__).parent.parent.parent / "prompts" / "tools"


class ToolRegistry:
    """
    Registry of available tools for agents.

    Usage:
        registry = ToolRegistry()
        registry.register("calendar_create", calendar_create_tool)
        tools = registry.resolve(["calendar_create", "rag_search"])
    """

    def __init__(self):
        self._tools: Dict[str, BaseTool] = {}

    def register(self, name: str, tool: BaseTool):
        """Register a tool by name"""
        self._tools[name] = tool
        logger.info(f"Registered tool: {name}")

    def get(self, name: str) -> Optional[BaseTool]:
        """Get a tool by name"""
        return self._tools.get(name)

    def resolve(self, tool_names: List[str]) -> tuple[List[BaseTool], str]:
        """
        Resolve a list of tool names to tool instances and their combined doc string.

        Returns (tools, doc) where doc is the concatenated markdown from
        prompts/tools/{tool_name}.md for each resolved tool, empty string if none found.
        Skips unknown tool names with a warning; logs an error for missing doc files.
        """
        tools = []
        doc_parts = []
        for name in tool_names:
            tool = self._tools.get(name)
            if tool:
                tools.append(tool)
            else:
                logger.warning(f"Unknown tool requested: {name}")
                continue

            doc_path = _TOOLS_DOCS_DIR / f"{name}.md"
            if doc_path.exists():
                doc_parts.append(doc_path.read_text(encoding="utf-8").strip())
            else:
                logger.error(f"Tool doc not found: {doc_path}")

        return tools, "\n\n---\n\n".join(doc_parts)

    @property
    def available_tools(self) -> List[str]:
        """List all registered tool names"""
        return list(self._tools.keys())

    def __len__(self) -> int:
        return len(self._tools)
