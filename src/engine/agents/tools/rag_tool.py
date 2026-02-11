"""
RAG Tool

LangChain tool for document search via RAG collection.
"""
import logging
from langchain_core.tools import tool

logger = logging.getLogger("rugpt.agents.tools.rag")


@tool
def rag_search(query: str, collection: str = "") -> str:
    """Search documents in the knowledge base.
    Args:
        query: Search query
        collection: Optional RAG collection name to search in
    """
    # Future: will call vector store search
    logger.info(f"rag_search called: query={query}, collection={collection}")
    return f"No documents found for '{query}'. (RAG search will be active when vector store is configured)"
