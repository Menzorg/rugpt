"""
Web Search Tool

LangChain tool for web search.
"""
import logging
from langchain_core.tools import tool

logger = logging.getLogger("rugpt.agents.tools.web")


@tool
def web_search(query: str) -> str:
    """Search the web for current information.
    Args:
        query: Search query
    """
    # Future: will call web search API
    logger.info(f"web_search called: query={query}")
    return f"No results for '{query}'. (Web search will be active when configured)"
