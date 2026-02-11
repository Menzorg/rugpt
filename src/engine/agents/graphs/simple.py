"""
Simple Agent Graph

Two modes:
- No tools: prompt -> LLM -> response (equivalent to old OllamaProvider flow)
- With tools: ReAct agent (LLM decides when to call tools)
"""
import logging
from typing import List, Optional

from langchain_core.messages import SystemMessage, HumanMessage, AIMessage
from langchain_core.tools import BaseTool
from langchain_ollama import ChatOllama
from langgraph.prebuilt import create_react_agent

from ..result import AgentResult, ToolCall

logger = logging.getLogger("rugpt.agents.graphs.simple")


async def run_simple_agent(
    llm: ChatOllama,
    system_prompt: str,
    messages: List[dict],
    tools: Optional[List[BaseTool]] = None,
    max_tokens: int = 2048,
    temperature: float = 0.7,
) -> AgentResult:
    """
    Run simple agent.

    Without tools: direct LLM call (like old OllamaProvider).
    With tools: LangGraph ReAct agent that can call tools.

    Args:
        llm: ChatOllama instance
        system_prompt: System prompt text
        messages: Conversation history as list of {"role": str, "content": str}
        tools: Optional list of LangChain tools
        max_tokens: Max tokens in response
        temperature: Sampling temperature

    Returns:
        AgentResult with response content
    """
    # Build LangChain message objects
    lc_messages = []
    if system_prompt:
        lc_messages.append(SystemMessage(content=system_prompt))

    for msg in messages:
        role = msg.get("role", "user")
        content = msg.get("content", "")
        if role == "user":
            lc_messages.append(HumanMessage(content=content))
        elif role == "assistant":
            lc_messages.append(AIMessage(content=content))
        # system messages already handled above

    if not tools:
        # Direct LLM call — no tools, no agent overhead
        return await _direct_llm_call(llm, lc_messages)
    else:
        # ReAct agent with tools
        return await _react_agent_call(llm, lc_messages, system_prompt, tools)


async def _direct_llm_call(
    llm: ChatOllama,
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
    llm: ChatOllama,
    messages: list,
    system_prompt: str,
    tools: List[BaseTool],
) -> AgentResult:
    """ReAct agent with tool calling"""
    try:
        agent = create_react_agent(llm, tools, prompt=system_prompt)

        # The last message should be the user input
        # ReAct agent expects {"messages": [...]}
        result = await agent.ainvoke({"messages": messages})

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
