"""
Tool Registry

Central registry mapping tool names to LangChain tool functions.
Tools are registered at startup; agents resolve them by name from role.tools list.
"""
import logging
from typing import Dict, List, Optional
from langchain_core.tools import BaseTool

from .rag_tool import rag_search

logger = logging.getLogger("rugpt.agents.tools.registry")


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

    def resolve(self, tool_names: List[str]) -> List[BaseTool]:
        """
        Resolve a list of tool names to tool instances.
        Skips unknown names with a warning.
        """
        tools = []
        for name in tool_names:
            tool = self._tools.get(name)
            if tool:
                tools.append(tool)
            else:
                logger.warning(f"Unknown tool requested: {name}")
        return tools

    @property
    def available_tools(self) -> List[str]:
        """List all registered tool names"""
        return list(self._tools.keys())

    def __len__(self) -> int:
        return len(self._tools)
