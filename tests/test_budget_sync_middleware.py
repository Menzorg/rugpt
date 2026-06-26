"""
Integration test: BudgetSyncMiddleware syncs total_tokens_spent from usage_metadata.

Makes a real call to the LiteLLM API and verifies that:
  1. The returned AIMessage carries usage_metadata.input_tokens.
  2. BudgetSyncMiddleware._sync replaces total_tokens_spent with that value.
  3. When no usage_metadata is present, _sync leaves the counter untouched.
"""
import pytest
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from langchain_openai import ChatOpenAI

from src.engine.agents.middleware import BudgetSyncMiddleware
from src.engine.agents.runtime import RuntimeContext
from src.engine.config import Config


@pytest.mark.asyncio
async def test_budget_sync_reads_usage_metadata_from_real_llm_call():
    llm = ChatOpenAI(
        model=Config.DEFAULT_MODEL,
        base_url=Config.LLM_BASE_URL,
        api_key=Config.LLM_API_KEY,
        max_tokens=16,
    )

    response = await llm.ainvoke([HumanMessage(content="Reply with one word.")])

    assert isinstance(response, AIMessage), "Expected AIMessage from LiteLLM"
    meta = getattr(response, "usage_metadata", None) or {}
    assert meta.get("input_tokens", 0) > 0, (
        f"Expected usage_metadata.input_tokens > 0, got: {meta}"
    )

    # Simulate the message list the middleware will see before the next model call:
    # prior turn is now in history, so the AIMessage is in the message list.
    messages = [
        HumanMessage(content="Reply with one word."),
        response,
    ]

    runtime_ctx = RuntimeContext()
    runtime_ctx.total_tokens_spent = 99999  # sentinel: must be replaced

    middleware = BudgetSyncMiddleware(runtime_ctx)
    middleware._sync(messages)

    assert runtime_ctx.total_tokens_spent == meta["input_tokens"], (
        f"Expected total_tokens_spent={meta['input_tokens']} (from usage_metadata), "
        f"got {runtime_ctx.total_tokens_spent}"
    )


def test_budget_sync_leaves_counter_untouched_without_usage_metadata():
    messages = [
        SystemMessage(content="You are helpful."),
        HumanMessage(content="Hello"),
        AIMessage(content="Hi there"),  # no usage_metadata
    ]

    runtime_ctx = RuntimeContext()
    runtime_ctx.total_tokens_spent = 12345

    middleware = BudgetSyncMiddleware(runtime_ctx)
    middleware._sync(messages)

    assert runtime_ctx.total_tokens_spent == 12345, (
        "Counter must not change when no AIMessage carries usage_metadata"
    )
