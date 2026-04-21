"""
List Documents Tools

LangChain tools for listing uploaded documents from UserFileStorage.

org_id and user_id are injected via RunnableConfig — LLM sees no identity params.
"""
import logging
from typing import Annotated
from uuid import UUID

from langchain_core.tools import tool, InjectedToolArg
from langchain_core.runnables import RunnableConfig

from ...services.engine_service import get_engine_service

logger = logging.getLogger("rugpt.agents.tools.list_documents")


@tool
async def list_documents(
    config: Annotated[RunnableConfig, InjectedToolArg],
) -> str:
    """List documents that belong to the current user."""
    configurable = config.get("configurable", {})
    user_id = configurable.get("user_id", "")

    if not user_id:
        logger.error("list_my_documents: user_id not found in config")
        return "Unknown user"

    engine = get_engine_service()
    files = await engine.user_file_storage.list_by_user(UUID(user_id))

    if not files:
        return "No documents found for this user."

    lines = [f"- {f.original_filename} ({f.file_type}, {f.rag_status})" for f in files]
    return "\n".join(lines)
