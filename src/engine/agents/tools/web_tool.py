"""
Web Search Tool — Perplexity API

Uses Perplexity sonar-pro model which combines web search with LLM reasoning
and returns an answer with citations (source URLs). The agent's own LLM then
frames the result for the user in conversational context.

Tool is async so the ReAct loop awaits it directly in the same event loop
(no worker-thread pool contention, no event-loop gymnastics).
"""
import logging
import httpx
from langchain_core.tools import tool

from ...config import Config

logger = logging.getLogger("rugpt.agents.tools.web")

_PERPLEXITY_URL = "https://api.perplexity.ai/chat/completions"
_MODEL = "sonar"
_TIMEOUT_SECONDS = 60.0


@tool
async def web_search(query: str) -> str:
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

    logger.info(f"web_search: query={query!r}")

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
    except httpx.HTTPStatusError as e:
        logger.error(f"Perplexity HTTP {e.response.status_code}: {e.response.text[:200]}")
        return f"Ошибка веб-поиска: код {e.response.status_code}."
    except httpx.RequestError as e:
        logger.error(f"Perplexity request error: {e}")
        return "Не удалось достучаться до Perplexity (сетевая ошибка)."
    except Exception as e:
        logger.exception(f"Perplexity unexpected error: {e}")
        return f"Непредвиденная ошибка веб-поиска: {e}"

    try:
        answer = data["choices"][0]["message"]["content"]
    except (KeyError, IndexError):
        logger.error(f"Perplexity returned unexpected shape: {data}")
        return "Perplexity вернул ответ в непонятном формате."

    citations = data.get("citations") or []
    if citations:
        sources_block = "\n\nИсточники:\n" + "\n".join(
            f"[{i}] {url}" for i, url in enumerate(citations, 1)
        )
    else:
        sources_block = ""

    return answer + sources_block
