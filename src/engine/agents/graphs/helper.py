"""
Helper Agent Graph

Lightweight copy of simple graph with hardcoded safety limits:
- tool call limit per tool: 5
- recursion limit: 30

Used by HelperExecutor for internal sub-agents (helpers).
"""
import logging
from typing import Any, List, Optional

from langchain.agents import create_agent
from langchain.agents.middleware import ToolCallLimitMiddleware
from langchain_core.runnables import RunnableConfig
from langchain_core.tools import BaseTool
from langchain_openai import ChatOpenAI

from ..result import AgentResult, ToolCall
from ..runtime import RuntimeContext
from ...utils.token_logger import log_llm_tokens, log_token_summary

logger = logging.getLogger("rugpt.agents.graphs.helper")

_HELPER_TOOL_CALL_LIMIT = 5
_HELPER_RECURSION_LIMIT = 30


async def run_helper_agent(
    llm: ChatOpenAI,
    system_prompt: str,
    messages: List[dict],
    tools: Optional[List[BaseTool]] = None,
    config: Optional[RunnableConfig] = None,
    context_schema: Optional[Any] = None,
) -> AgentResult:
    """
    Run a helper agent with hardcoded tool call and recursion limits.

    Without tools: direct LLM call.
    With tools: ReAct agent capped at _HELPER_TOOL_CALL_LIMIT calls per tool
    and _HELPER_RECURSION_LIMIT graph steps.
    """
    lc_messages = []
    if system_prompt:
        lc_messages.append({"role": "system", "content": system_prompt})
    for msg in messages:
        role = msg.get("role", "user")
        content = msg.get("content", "")
        if role in ("user", "assistant"):
            lc_messages.append({"role": role, "content": content})

    if not tools:
        return await _direct_llm_call(llm, lc_messages)

    llm_think = llm.bind(
        extra_body={
            "chat_template_kwargs": {
                "enable_thinking": True,
            }
        }
    )
    return await _react_agent_call(llm_think, lc_messages, system_prompt, tools, config, context_schema)


async def _direct_llm_call(llm: ChatOpenAI, messages: list) -> AgentResult:
    try:
        response = await llm.ainvoke(messages)
        content = response.content if hasattr(response, "content") else str(response)
        total = log_llm_tokens(response, label="helper.direct_llm_call", logger=logger, messages=messages)
        log_token_summary("helper.direct_llm_call", total, logger=logger)
        return AgentResult(
            content=content,
            model=llm.model,
            agent_type="helper",
            finish_reason="stop",
            tokens_used=total,
        )
    except Exception as e:
        logger.error("Helper direct LLM call failed: %s", e)
        return AgentResult(
            content=f"[Error: {e}]",
            model=llm.model,
            agent_type="helper",
            finish_reason="error",
            error=str(e),
        )


async def _react_agent_call(
    llm: ChatOpenAI,
    messages: list,
    system_prompt: str,
    tools: List[BaseTool],
    config: Optional[RunnableConfig] = None,
    context_schema: Optional[Any] = None,
) -> AgentResult:
    try:
        context = (
            None
            if context_schema is None or isinstance(context_schema, type)
            else context_schema
        )
        schema = (
            context_schema
            if context_schema is None or isinstance(context_schema, type)
            else type(context_schema)
        )

        middleware = [
            ToolCallLimitMiddleware(
                run_limit=_HELPER_TOOL_CALL_LIMIT,
                exit_behavior="continue",
            )
        ]

        agent = create_agent(
            llm,
            tools=tools,
            system_prompt=system_prompt,
            middleware=middleware,
            context_schema=schema,
        )

        input_messages = (
            messages[1:]
            if messages and messages[0].get("role") == "system"
            else messages
        )

        result = await agent.ainvoke(
            {"messages": input_messages},
            config={
                **(config or {}),
                "recursion_limit": _HELPER_RECURSION_LIMIT * (len(middleware) + 1),
            },
            context=context,
        )

        output_messages = result.get("messages", [])
        tool_calls = []
        final_content = ""
        grand_total = 0

        for msg in output_messages:
            if hasattr(msg, "tool_calls") and msg.tool_calls:
                for tc in msg.tool_calls:
                    tool_calls.append(ToolCall(
                        tool_name=tc.get("name", ""),
                        tool_input=tc.get("args", {}),
                    ))
            if hasattr(msg, "content") and msg.type == "ai" and not getattr(msg, "tool_calls", None):
                final_content = msg.content
            if hasattr(msg, "usage_metadata") and msg.type == "ai":
                step_label = f"helper.react_step[tool={'yes' if getattr(msg, 'tool_calls', None) else 'no'}]"
                spent = log_llm_tokens(msg, label=step_label, logger=logger, running_total=grand_total)
                grand_total += spent

        log_token_summary("helper.react_agent_call", grand_total, logger=logger)

        if not final_content and output_messages:
            last = output_messages[-1]
            final_content = last.content if hasattr(last, "content") else str(last)

        return AgentResult(
            content=final_content,
            model=llm.model,
            agent_type="helper",
            tool_calls=tool_calls,
            finish_reason="stop",
            tokens_used=grand_total,
        )

    except Exception as e:
        logger.error("Helper ReAct agent failed: %s", e)
        return AgentResult(
            content=f"[Error: {e}]",
            model=llm.model,
            agent_type="helper",
            finish_reason="error",
            error=str(e),
        )
