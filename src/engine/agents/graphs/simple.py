"""
Simple Agent Graph

Two modes:
- No tools: prompt -> LLM -> response (direct LLM call)
- With tools: ReAct agent (LLM decides when to call tools)
"""

from src.engine.unified_logger import get_logger
from typing import Any, List, Optional

from langchain.agents import create_agent
from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.runnables import RunnableConfig
from langchain_core.tools import BaseTool
from langchain_openai import ChatOpenAI

from ..result import AgentResult, ToolCall
from ...utils.token_logger import log_llm_tokens, log_token_summary

logger = get_logger("agents")

class ReasoningLoggerCallback(BaseCallbackHandler):
    def on_llm_end(self, response, **_kwargs):
        try:
            for gen_list in response.generations:
                for gen in gen_list:
                    info = gen.generation_info or {}
                    msg = getattr(gen, "message", None)
                    ak = getattr(msg, "additional_kwargs", {}) if msg else {}
                    rm = getattr(msg, "response_metadata", {}) if msg else {}

                    # Reasoning may come from generation_info, additional_kwargs,
                    # or response_metadata depending on provider/vLLM parser.
                    reasoning = info.get("reasoning") or ak.get("reasoning") or rm.get("reasoning")
                    if reasoning:
                        logger.info("reasoning: %s", reasoning.strip())
                    else:
                        logger.info("reasoning: none")

                    for tc in getattr(msg, "tool_calls", None) or []:
                        logger.info(
                            "tool_call: name=%s args=%s id=%s",
                            tc.get("name"), tc.get("args"), tc.get("id"),
                        )

                    logger.debug(
                        "generation_info: %r | additional_kwargs: %r",
                        info, ak,
                    )
        except Exception as e:
            logger.warning("ReasoningLoggerCallback error: %s", e)


async def run_simple_agent(
    llm: ChatOpenAI,
    system_prompt: str,
    messages: List[dict],
    tools: Optional[List[BaseTool]] = None,
    config: Optional[RunnableConfig] = None,
    context_schema: Optional[Any] = None,
    middleware: Optional[List[Any]] = None,
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
        llm_think = llm.bind(
            extra_body={
                "chat_template_kwargs": {
                    "enable_thinking": True
                }
            })
        
        # ReAct agent with tools
        return await _react_agent_call(
            llm_think,
            lc_messages,
            system_prompt,
            tools,
            config,
            context_schema,
            middleware,
        )

async def _direct_llm_call(
    llm: ChatOpenAI,
    messages: list,
) -> AgentResult:
    """Direct LLM invocation without tools"""
    try:
        response = await llm.ainvoke(messages)
        content = response.content if hasattr(response, 'content') else str(response)

        total = log_llm_tokens(response, label="simple.direct_llm_call", logger=logger, messages=messages)
        log_token_summary("simple.direct_llm_call", total, logger=logger)

        return AgentResult(
            content=content,
            model=llm.model,
            agent_type="simple",
            finish_reason="stop",
            tokens_used=total,
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
    messages: list,
    system_prompt: str,
    tools: List[BaseTool],
    config: Optional[RunnableConfig] = None,
    context_schema: Optional[Any] = None,
    extra_middleware: Optional[List[Any]] = None,
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
        middleware = extra_middleware or []
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
            config={**(config or {}), "recursion_limit": 50 * (len(middleware) + 1)},
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

        grand_total = log_token_summary(
            "simple.react_agent_call",
            logger=logger,
            messages=output_messages,
        )

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
            tokens_used=grand_total,
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
