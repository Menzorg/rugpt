"""
Call Helper Tool

LangChain tool that delegates a query to a named helper agent via HelperExecutor.
Returns the helper's response text, or an error message on failure.

Service lifecycle: call init_call_helper_tool(executor) once during engine startup.
"""
import logging
from typing import Annotated, Optional

from langchain_core.runnables import RunnableConfig
from langchain_core.tools import tool, InjectedToolArg

from ..helper_executor import HelperExecutor

logger = logging.getLogger("rugpt.agents.tools.call_helper")

_helper_executor: Optional[HelperExecutor] = None


def init_call_helper_tool(executor: HelperExecutor) -> None:
    """Set the shared HelperExecutor instance."""
    global _helper_executor
    _helper_executor = executor
    logger.info("call_helper tool initialized")


@tool(response_format="content")
async def call_helper(
    helper_name: str,
    query: str,
    config: Annotated[RunnableConfig, InjectedToolArg],
) -> str:
    """Delegate a query to a named internal helper agent and return its response.
    Args:
        helper_name: Name of the helper to call.
        query: The question or task to pass to the helper.
    """
    if _helper_executor is None:
        logger.error("call_helper: executor not initialized, call init_call_helper_tool() at startup")
        return "call_helper unavailable: executor not initialized."

    configurable = config.get("configurable", {})

    logger.info(
        "call_helper: helper=%s org_id=%s caller_user_id=%s query_len=%d",
        helper_name,
        configurable.get("org_id", ""),
        configurable.get("caller_user_id") or configurable.get("user_id", ""),
        len(query),
    )

    try:
        result = await _helper_executor.execute(
            helper_name=helper_name,
            user_message=query,
            configurable=configurable,
        )
        if result.error:
            logger.error("call_helper: helper=%s returned error: %s", helper_name, result.error)
            return "[Helper вернул ошибку. Попробуй переформулировать запрос.]"
        return result.content
    except Exception as e:
        logger.error("call_helper: execution failed for helper=%s: %s", helper_name, e, exc_info=True)
        return "[call_helper завершился с ошибкой. Попробуй переформулировать запрос.]"
