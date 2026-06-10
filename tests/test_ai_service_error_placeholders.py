"""
Integration tests for _call_llm error message dispatch.

Two real-error scenarios covered:

1. Context window exceeded (300 000-word prompt → 80 001+ tokens):
   TYPE: openai.BadRequestError
   STR: Error code: 400 - {'error': {'message': 'litellm.ContextWindowExceededError: ...'}}
   → "ContextWindowExceededError" present in str(e) → friendly overload message.

2. Generic error (invalid API key → 401):
   TYPE: openai.AuthenticationError
   STR: Error code: 401 - {'error': {'message': 'Authentication Error, LiteLLM Virtual Key expected ...'}}
   → no "ContextWindowExceededError" → generic support message.

The graphs catch both as `except Exception as e` and set `error=str(e)`.

Marked @integration — skipped when the LiteLLM proxy at Config.LLM_BASE_URL is
unreachable or the model is not loaded.
"""
import asyncio
import logging

import httpx
import pytest

from src.engine.agents.graphs.simple import _direct_llm_call
from src.engine.agents.result import AgentResult
from src.engine.agents.executor import ChatOpenAI
from src.engine.config import Config
from src.engine.models.role import Role
from src.engine.services.ai_service import AIService
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

logger = logging.getLogger(__name__)

WORD_COUNT = 300_000
EXPECTED_CONTENT_CONTEXT_WINDOW = (
    "Ой, эта задача оказалась слишком большой для меня — "
    "я просто не могу удержать всё это в голове за один раз. "
    "Давайте попробуем разбить запрос на несколько этапов и выполнить по очереди."
)
EXPECTED_CONTENT_GENERIC_ERROR = (
    "Что-то пошло не так на моей стороне — я столкнулся с технической ошибкой "
    "и не смог ответить. Попробуйте ещё раз чуть позже, а если не поможет — "
    "напишите в поддержку, они разберутся."
)


def _proxy_reachable() -> bool:
    try:
        resp = httpx.get(
            f"{Config.LLM_BASE_URL}/models",
            headers={"Authorization": f"Bearer {Config.LLM_API_KEY}"},
            timeout=3,
        )
        models = [m["id"] for m in resp.json().get("data", [])]
        reachable = Config.DEFAULT_MODEL in models
        logger.info(
            "proxy reachability check: url=%s model=%s reachable=%s available_models=%s",
            Config.LLM_BASE_URL,
            Config.DEFAULT_MODEL,
            reachable,
            models,
        )
        return reachable
    except Exception as e:
        logger.warning("proxy reachability check failed: %s", e)
        return False


integration = pytest.mark.skipif(
    not _proxy_reachable(),
    reason=f"LiteLLM proxy not reachable at {Config.LLM_BASE_URL}",
)


def _make_big_prompt() -> str:
    prompt = " ".join(["слово"] * WORD_COUNT)
    logger.info(
        "generated prompt: word_count=%d char_count=%d approx_bytes=%d",
        WORD_COUNT,
        len(prompt),
        len(prompt.encode()),
    )
    return prompt


def _make_llm() -> ChatOpenAI:
    return ChatOpenAI(
        base_url=Config.LLM_BASE_URL,
        api_key=Config.LLM_API_KEY,
        model=Config.DEFAULT_MODEL,
        temperature=0.3,
        timeout=60.0,
    )


def _make_service_with_error_result(error_result: AgentResult) -> AIService:
    svc = AIService(
        role_storage=AsyncMock(),
        user_storage=AsyncMock(),
        chat_storage=AsyncMock(),
        message_storage=AsyncMock(),
        agent_run_storage=AsyncMock(),
        kafka_producer=None,
    )
    executor = MagicMock()
    executor.execute = AsyncMock(return_value=(error_result, {}))
    svc.agent_executor = executor
    return svc


@integration
def test_direct_llm_call_context_window_sets_error_finish_reason():
    """
    _direct_llm_call must catch the real openai.BadRequestError from LiteLLM
    and return finish_reason='error' with 'ContextWindowExceededError' in error.
    """
    async def go():
        llm = _make_llm()
        prompt = _make_big_prompt()
        messages = [{"role": "user", "content": prompt}]

        logger.info(
            "calling _direct_llm_call: base_url=%s model=%s message_count=%d",
            Config.LLM_BASE_URL,
            Config.DEFAULT_MODEL,
            len(messages),
        )

        result = await _direct_llm_call(llm, messages)

        logger.info(
            "result: finish_reason=%r error=%r content_preview=%r",
            result.finish_reason,
            result.error,
            result.content[:120] if result.content else None,
        )
        logger.info(
            "assertion: expected finish_reason='error', got %r",
            result.finish_reason,
        )
        assert result.finish_reason == "error", (
            f"Expected finish_reason='error', got {result.finish_reason!r}. "
            f"Full error: {result.error!r}"
        )

        logger.info(
            "assertion: 'ContextWindowExceededError' in error string. error=%r",
            result.error,
        )
        assert result.error and "ContextWindowExceededError" in result.error, (
            f"Expected 'ContextWindowExceededError' in error string, got: {result.error!r}"
        )

    asyncio.run(go())


@integration
def test_call_llm_context_window_returns_friendly_message():
    """
    Full path: real LiteLLM proxy raises context window error →
    _direct_llm_call wraps it into AgentResult(finish_reason='error') →
    _call_llm detects 'ContextWindowExceededError' and sets the friendly content.
    """
    async def go():
        llm = _make_llm()
        prompt = _make_big_prompt()
        messages = [{"role": "user", "content": prompt}]

        logger.info(
            "step 1 — calling _direct_llm_call to get real error result: "
            "base_url=%s model=%s",
            Config.LLM_BASE_URL,
            Config.DEFAULT_MODEL,
        )
        raw_result = await _direct_llm_call(llm, messages)
        logger.info(
            "real error result: finish_reason=%r error=%r",
            raw_result.finish_reason,
            raw_result.error,
        )

        # Feed the real error result into _call_llm via a stubbed executor
        svc = _make_service_with_error_result(raw_result)
        role = Role(id=uuid4(), code="agent", is_active=True)

        logger.info("step 2 — passing error result through _call_llm")
        result, metadata = await svc._call_llm(
            role=role,
            conv_messages=messages,
            caller_user_id=uuid4(),
        )

        logger.info(
            "final result: finish_reason=%r content=%r metadata=%r",
            result.finish_reason,
            result.content,
            metadata,
        )
        logger.info(
            "assertion: expected content=%r, got %r",
            EXPECTED_CONTENT_CONTEXT_WINDOW,
            result.content,
        )
        assert result.finish_reason == "error", (
            f"Expected finish_reason='error', got {result.finish_reason!r}"
        )
        assert result.content == EXPECTED_CONTENT_CONTEXT_WINDOW, (
            f"Expected friendly message:\n  {EXPECTED_CONTENT_CONTEXT_WINDOW!r}\n"
            f"Got:\n  {result.content!r}\n"
            f"Raw error was:\n  {raw_result.error!r}"
        )
        assert metadata == {}, f"Expected empty metadata, got {metadata!r}"

    asyncio.run(go())


@integration
def test_call_llm_generic_error_returns_friendly_message():
    """
    Full path: real LiteLLM proxy returns 401 (invalid API key) →
    _direct_llm_call wraps openai.AuthenticationError into AgentResult(finish_reason='error') →
    _call_llm sees no 'ContextWindowExceededError' and sets the generic support message.

    Real error captured:
        TYPE: openai.AuthenticationError
        STR: Error code: 401 - {'error': {'message': "Authentication Error, LiteLLM Virtual
             Key expected. Received=inva****xist, expected to start with 'sk-'."}}
    """
    async def go():
        llm_bad_key = ChatOpenAI(
            base_url=Config.LLM_BASE_URL,
            api_key="invalid-key-that-does-not-exist",
            model=Config.DEFAULT_MODEL,
            temperature=0.3,
            timeout=10.0,
        )
        messages = [{"role": "user", "content": "привет"}]

        logger.info(
            "step 1 — calling _direct_llm_call with invalid API key: base_url=%s model=%s",
            Config.LLM_BASE_URL,
            Config.DEFAULT_MODEL,
        )
        raw_result = await _direct_llm_call(llm_bad_key, messages)
        logger.info(
            "real error result: finish_reason=%r error=%r",
            raw_result.finish_reason,
            raw_result.error,
        )
        logger.info(
            "'ContextWindowExceededError' present in error: %s",
            "ContextWindowExceededError" in (raw_result.error or ""),
        )

        svc = _make_service_with_error_result(raw_result)
        role = Role(id=uuid4(), code="agent", is_active=True)

        logger.info("step 2 — passing error result through _call_llm")
        result, metadata = await svc._call_llm(
            role=role,
            conv_messages=messages,
            caller_user_id=uuid4(),
        )

        logger.info(
            "final result: finish_reason=%r content=%r metadata=%r",
            result.finish_reason,
            result.content,
            metadata,
        )
        logger.info(
            "assertion: expected content=%r, got %r",
            EXPECTED_CONTENT_GENERIC_ERROR,
            result.content,
        )
        assert result.finish_reason == "error", (
            f"Expected finish_reason='error', got {result.finish_reason!r}"
        )
        assert result.content == EXPECTED_CONTENT_GENERIC_ERROR, (
            f"Expected generic support message:\n  {EXPECTED_CONTENT_GENERIC_ERROR!r}\n"
            f"Got:\n  {result.content!r}\n"
            f"Raw error was:\n  {raw_result.error!r}"
        )
        assert metadata == {}, f"Expected empty metadata, got {metadata!r}"

    asyncio.run(go())