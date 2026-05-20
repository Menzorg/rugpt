"""Regression: RunnableConfig and ToolRuntime must be injected into tools.

If langchain/langgraph changes how InjectedToolArg works (e.g. a version bump
that breaks injection), these tests will fail before any real tool is affected.
"""
import uuid
from typing import Annotated, Any, Iterator

import pytest
from langchain.agents import create_agent
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.runnables import RunnableConfig
from langchain_core.tools import InjectedToolArg, StructuredTool
from langgraph.prebuilt import ToolRuntime
from pydantic import BaseModel

from src.engine.agents.runtime import RuntimeContext


_received: dict[str, Any] = {}


class _PingInput(BaseModel):
    value: str


async def _ping(
    value: str,
    config: RunnableConfig = None,
    runtime: ToolRuntime[RuntimeContext] = None,
) -> str:
    _received["config"] = config
    _received["runtime"] = runtime
    return "pong"


_ping_tool = StructuredTool.from_function(
    coroutine=_ping,
    name="ping",
    description="test injection canary",
    args_schema=_PingInput,
)


class _OnceLLM(BaseChatModel):
    """Fake LLM: first call emits a ping tool call, second call stops."""

    _call_count: int = 0

    @property
    def _llm_type(self) -> str:
        return "once"

    def bind_tools(self, tools, **kwargs):
        return self

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        self._call_count += 1
        if self._call_count == 1:
            msg = AIMessage(
                content="",
                tool_calls=[
                    {"name": "ping", "args": {"value": "hi"}, "id": "tc-1", "type": "tool_call"}
                ],
            )
        else:
            msg = AIMessage(content="done")
        return ChatResult(generations=[ChatGeneration(message=msg)])

    def _stream(self, messages, stop=None, run_manager=None, **kwargs) -> Iterator:
        yield from []


@pytest.mark.asyncio
async def test_runnable_config_injected():
    _received.clear()
    org_id = str(uuid.uuid4())
    caller_id = str(uuid.uuid4())

    llm = _OnceLLM()
    agent = create_agent(llm, tools=[_ping_tool], context_schema=RuntimeContext)

    config: RunnableConfig = {
        "configurable": {
            "org_id": org_id,
            "caller_user_id": caller_id,
        },
        "recursion_limit": 10,
    }
    await agent.ainvoke(
        {"messages": [{"role": "user", "content": "ping"}]},
        config=config,
        context=RuntimeContext(),
    )

    assert _received.get("config") is not None, "config was not injected into tool"
    injected_configurable = (_received["config"] or {}).get("configurable", {})
    assert injected_configurable.get("org_id") == org_id
    assert injected_configurable.get("caller_user_id") == caller_id


@pytest.mark.asyncio
async def test_tool_runtime_injected():
    _received.clear()
    runtime_ctx = RuntimeContext(available_tools_count=3)

    llm = _OnceLLM()
    agent = create_agent(llm, tools=[_ping_tool], context_schema=RuntimeContext)

    await agent.ainvoke(
        {"messages": [{"role": "user", "content": "ping"}]},
        config={"recursion_limit": 10},
        context=runtime_ctx,
    )

    assert _received.get("runtime") is not None, "runtime was not injected into tool"
    assert isinstance(_received["runtime"].context, RuntimeContext)
    assert _received["runtime"].context.available_tools_count == 3
