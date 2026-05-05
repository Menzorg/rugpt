"""
Simple Agent Graph

Two modes:
- No tools: prompt -> LLM -> response (direct LLM call)
- With tools: ReAct agent (LLM decides when to call tools)
"""
import logging
from typing import Any, List, Optional

from langchain.agents import create_agent
from langchain_core.runnables import RunnableConfig
from langchain_core.tools import BaseTool
from langchain_openai import ChatOpenAI

from ..middleware import HistoryCompactionMiddleware, TokenBudgetToolBlockMiddleware
from ..result import AgentResult, ToolCall
from ..runtime import RuntimeContext

_TOOL_BLOCK_TOOL_NAMES = {"list_documents", "rag_search"}

logger = logging.getLogger("rugpt.agents.graphs.simple")


async def run_simple_agent(
    llm: ChatOpenAI,
    system_prompt: str,
    messages: List[dict],
    tools: Optional[List[BaseTool]] = None,
    config: Optional[RunnableConfig] = None,
    context_schema: Optional[Any] = None,
) -> AgentResult:
    """
    Run simple agent.

    Without tools: direct LLM call.
    With tools: LangGraph ReAct agent that can call tools.
    max_tokens is set on the llm instance by the caller (AgentExecutor._create_llm).
    """
    # Build OpenAI-style message dicts.
    lc_messages = []
    if system_prompt:
        lc_messages.append({"role": "system", "content": system_prompt})

    for msg in messages:
        role = msg.get("role", "user")
        content = msg.get("content", "")
        if role == "user":
            lc_messages.append({"role": "user", "content": content})
        elif role == "assistant":
            lc_messages.append({"role": "assistant", "content": content})
        # system messages already handled above

    if not tools:
        # Direct LLM call — no tools, no agent overhead
        return await _direct_llm_call(llm, lc_messages)
    else:
        llm_nothink = llm.bind(
            extra_body={
                "chat_template_kwargs": {
                    "enable_thinking": False,
                }
            })
        llm_thinking = llm.bind(
            extra_body={
                "chat_template_kwargs": {
                    "enable_thinking": True,
                }
            }
        )
        # ReAct agent with tools
        return await _react_agent_call(
            llm_nothink,
            llm_thinking,
            lc_messages,
            system_prompt,
            tools,
            config,
            context_schema,
        )


async def _direct_llm_call(
    llm: ChatOpenAI,
    messages: list,
) -> AgentResult:
    """Direct LLM invocation without tools"""
    try:
        response = await llm.ainvoke(messages)
        content = response.content if hasattr(response, 'content') else str(response)

        return AgentResult(
            content=content,
            model=llm.model,
            agent_type="simple",
            finish_reason="stop",
        )
    except Exception as e:
        logger.error(f"Direct LLM call failed: {e}")
        return AgentResult(
            content=f"[Error: {e}]",
            model=llm.model,
            agent_type="simple",
            finish_reason="error",
            error=str(e),
        )


async def _react_agent_call(
    llm: ChatOpenAI,
    summary_llm: ChatOpenAI,
    messages: list,
    system_prompt: str,
    tools: List[BaseTool],
    config: Optional[RunnableConfig] = None,
    context_schema: Optional[Any] = None,
) -> AgentResult:
    """ReAct agent with tool calling"""
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
        runtime_ctx = context if isinstance(context, RuntimeContext) else None
        # tool_names = {t.name for t in tools}
        # critical_tokens_cap = (
        #     runtime_ctx.critical_tokens_cap if runtime_ctx is not None else 25_000
        # )
        #TODO: Decide on one of these two middlewares or both
        # if tool_names & _TOOL_BLOCK_TOOL_NAMES:
        #     middleware = [TokenBudgetToolBlockMiddleware(runtime_context=runtime_ctx)]
        # else:
        middleware = [HistoryCompactionMiddleware(
            summary_llm,
            trigger_tokens=20000,
            keep_last=15,
        )]
        agent = create_agent(
            llm,
            tools=tools,
            system_prompt=system_prompt,
            middleware=middleware,
            context_schema=schema,
        )

        # create_agent receives system_prompt separately; keep the runtime
        # message state free of the duplicated system dict built above.
        input_messages = (
            messages[1:]
            if messages and messages[0].get("role") == "system"
            else messages
        )

        # The last message should be the user input
        # ReAct agent expects {"messages": [...]}
        # config carries org_id/user_id for tools like rag_search
        result = await agent.ainvoke(
            {"messages": input_messages},
            config={**(config or {}), "recursion_limit": 25},
            context=context,
        )

        # Extract final response from the result
        output_messages = result.get("messages", [])
        tool_calls = []
        final_content = ""

        for msg in output_messages:
            if hasattr(msg, 'tool_calls') and msg.tool_calls:
                for tc in msg.tool_calls:
                    tool_calls.append(ToolCall(
                        tool_name=tc.get("name", ""),
                        tool_input=tc.get("args", {}),
                    ))
            # Last AI message is the final response
            if hasattr(msg, 'content') and msg.type == "ai" and not getattr(msg, 'tool_calls', None):
                final_content = msg.content

        # If we didn't find a clean final message, use the last message
        if not final_content and output_messages:
            last = output_messages[-1]
            final_content = last.content if hasattr(last, 'content') else str(last)

        return AgentResult(
            content=final_content,
            model=llm.model,
            agent_type="simple+tools",
            tool_calls=tool_calls,
            finish_reason="stop",
        )

    except Exception as e:
        logger.error(f"ReAct agent failed: {e}")
        return AgentResult(
            content=f"[Error: {e}]",
            model=llm.model,
            agent_type="simple+tools",
            finish_reason="error",
            error=str(e),
        )
