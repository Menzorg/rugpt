"""
Web Search Tool — Perplexity API

Uses Perplexity sonar-pro model which combines web search with LLM reasoning
and returns an answer with citations (source URLs). The agent's own LLM then
frames the result for the user in conversational context.

Tool is async so the ReAct loop awaits it directly in the same event loop
(no worker-thread pool contention, no event-loop gymnastics).
"""

from src.engine.unified_logger import get_logger
import httpx
from langchain_core.runnables import RunnableConfig
from langchain_core.tools import tool
from langgraph.prebuilt import ToolRuntime

from ..runtime import RuntimeContext
from ...config import Config
from ...utils.token_counter import count_tokens

logger = get_logger("agents")
_TOOL_ERROR_RESULT = "Tool execution caused errors. No result"

_PERPLEXITY_URL = "https://api.perplexity.ai/chat/completions"
_MODEL = "sonar"
_TIMEOUT_SECONDS = 60.0

@tool
async def web_search(
    query: str,
    config: RunnableConfig = None,
    runtime: ToolRuntime[RuntimeContext] = None,
) -> str:
    """Search the web for current information via Perplexity.

    Returns an answer synthesised from live web results, plus a numbered list
    of source URLs. Use for questions about recent events, current facts,
    external companies, or anything not in the organisation's own documents.

    Args:
        query: Search query. Formulate it concretely — include relevant context
            (industry, city, year) rather than passing the user's raw message.
    """
    api_key = Config.PERPLEXITY_API_KEY
    if not api_key:
        logger.warning("web_search called but PERPLEXITY_API_KEY is not set")
        return (
            "Веб-поиск временно недоступен: не настроен ключ Perplexity API. "
            "Сообщите об этом администратору."
        )

    logger.info("tool web_search: query=%r", query)
    configurable = (config or {}).get("configurable", {})
    logger.info(
        "web_search identity: caller_user_id=%s callee_user_id=%s org_id=%s is_admin=%s invocation=%s",
        configurable.get("caller_user_id", ""),
        configurable.get("callee_user_id", ""),
        configurable.get("org_id", ""),
        bool(configurable.get("is_admin", False)),
        configurable.get("invocation_kind", ""),
    )

    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT_SECONDS) as client:
            response = await client.post(
                _PERPLEXITY_URL,
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": _MODEL,
                    "messages": [{"role": "user", "content": query}],
                },
            )
            response.raise_for_status()
            data = response.json()

        answer = data["choices"][0]["message"]["content"]
        citations = data.get("citations") or []
        if citations:
            sources_block = "\n\nИсточники:\n" + "\n".join(
                f"[{i}] {url}" for i, url in enumerate(citations, 1)
            )
        else:
            sources_block = ""

        result = answer + sources_block
        if runtime is not None:
            async with runtime.context.lock:
                runtime.context.total_tokens_spent += count_tokens(result)
        return result
    except Exception as e:
        logger.error(f"web_search failed: {e}", exc_info=True)
        return _TOOL_ERROR_RESULT
