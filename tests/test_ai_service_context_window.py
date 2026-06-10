"""
Test that _call_llm returns the context-window-exceeded friendly message
when the executor surfaces a ContextWindowExceededError from LiteLLM.

The error string below is the exact str() of openai.BadRequestError raised
by LiteLLM for a 400 ContextWindowExceededError, as captured by a live probe:

    TYPE: <class 'openai.BadRequestError'>
    ---STR---
    litellm.ContextWindowExceededError: litellm.BadRequestError: ContextWindowExceededError: Request Failed
    Error Code: 400
    Message: litellm.ContextWindowExceededError
"""
import asyncio
from unittest.mock import AsyncMock, MagicMock, patch
import httpx
import openai
from uuid import uuid4

from langchain_openai import ChatOpenAI

from src.engine.agents.graphs.simple import _direct_llm_call
from src.engine.agents.result import AgentResult
from src.engine.models.role import Role
from src.engine.services.ai_service import AIService

_REAL_ERROR_STR = (
    "litellm.ContextWindowExceededError: litellm.BadRequestError: "
    "ContextWindowExceededError: Request Failed\n"
    "Error Code: 400\n"
    "Message: litellm.ContextWindowExceededError"
)

_CTX_BODY = {
    "error": {
        "message": _REAL_ERROR_STR,
        "type": "invalid_request_error",
        "param": None,
        "code": "context_length_exceeded",
    }
}

_CONTEXT_WINDOW_RESPONSE = (
    "Кажется, я перегрузился от такого объёма задачи и сломался. "
    "Давайте разобьём её на отдельные шаги и попробуем снова."
)


def _make_service_with_executor_returning(result: AgentResult) -> AIService:
    svc = AIService(
        role_storage=AsyncMock(),
        user_storage=AsyncMock(),
        chat_storage=AsyncMock(),
        message_storage=AsyncMock(),
        agent_run_storage=AsyncMock(),
        kafka_producer=None,
    )
    executor = MagicMock()
    executor.execute = AsyncMock(return_value=(result, {}))
    svc.agent_executor = executor
    return svc


def test_direct_llm_call_context_window_returns_error_result():
    """_direct_llm_call catches openai.BadRequestError and sets finish_reason=error."""
    async def go():
        llm = ChatOpenAI(
            base_url="http://192.168.1.80:4000/v1",
            api_key="sk-dummy",
            model="google/gemma-4-31B-it",
            temperature=0.3,
            timeout=30.0,
        )

        # 300 000-word prompt — the realistic trigger for context overflow
        big_content = " ".join(["слово"] * 300_000)

        fake_exc = openai.BadRequestError(
            message=_REAL_ERROR_STR,
            response=httpx.Response(
                400,
                json=_CTX_BODY,
                request=httpx.Request(
                    "POST", "http://192.168.1.80:4000/v1/chat/completions"
                ),
            ),
            body=_CTX_BODY["error"],
        )

        with patch.object(llm.async_client, "create", side_effect=fake_exc):
            result = await _direct_llm_call(
                llm, [{"role": "user", "content": big_content}]
            )

        assert result.finish_reason == "error"
        assert "ContextWindowExceededError" in result.error

    asyncio.run(go())


def test_call_llm_context_window_exceeded_returns_friendly_message():
    """_call_llm maps ContextWindowExceededError to the friendly Russian message."""
    async def go():
        error_result = AgentResult(
            content="",
            model="test",
            agent_type="simple",
            finish_reason="error",
            error=_REAL_ERROR_STR,
        )
        svc = _make_service_with_executor_returning(error_result)
        role = Role(id=uuid4(), code="agent", is_active=True)

        big_prompt = " ".join(["слово"] * 300_000)
        conv_messages = [{"role": "user", "content": big_prompt}]

        result, metadata = await svc._call_llm(
            role=role,
            conv_messages=conv_messages,
            caller_user_id=uuid4(),
        )

        assert result.finish_reason == "error"
        assert result.content == _CONTEXT_WINDOW_RESPONSE
        assert metadata == {}

    asyncio.run(go())
