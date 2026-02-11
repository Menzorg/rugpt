"""
Chain Agent Graph

Sequential steps from agent_config["steps"].
Each step has its own prompt/instruction processed by the LLM,
with the output of step N feeding into step N+1.
"""
import logging
from typing import List, Optional

from langchain_core.messages import SystemMessage, HumanMessage
from langchain_core.tools import BaseTool
from langchain_ollama import ChatOllama

from ..result import AgentResult

logger = logging.getLogger("rugpt.agents.graphs.chain")


async def run_chain_agent(
    llm: ChatOllama,
    system_prompt: str,
    messages: List[dict],
    agent_config: dict,
    tools: Optional[List[BaseTool]] = None,
) -> AgentResult:
    """
    Run chain agent — sequential steps from agent_config["steps"].

    Each step:
    {
        "instruction": "Analyze the legal aspects of the question",
        "output_key": "legal_analysis"   (optional, for reference)
    }

    The user's original message + accumulated step outputs are
    passed to each step as context.

    Args:
        llm: ChatOllama instance
        system_prompt: Base system prompt
        messages: Conversation history
        agent_config: Must contain "steps" list
        tools: Optional tools (reserved for future, not used per-step yet)

    Returns:
        AgentResult with final step's output
    """
    steps = agent_config.get("steps", [])
    if not steps:
        logger.warning("Chain agent called with empty steps, falling back to direct call")
        from .simple import run_simple_agent
        return await run_simple_agent(llm, system_prompt, messages)

    # Extract user's original question (last user message)
    user_question = ""
    for msg in reversed(messages):
        if msg.get("role") == "user":
            user_question = msg.get("content", "")
            break

    accumulated_context = f"User question: {user_question}\n"
    last_output = ""

    for i, step in enumerate(steps):
        instruction = step.get("instruction", "")
        output_key = step.get("output_key", f"step_{i+1}")

        step_messages = [
            SystemMessage(content=system_prompt),
            HumanMessage(content=(
                f"{accumulated_context}\n"
                f"--- Step {i+1}/{len(steps)}: {instruction} ---\n"
                f"Respond to the instruction above based on the context."
            )),
        ]

        try:
            response = await llm.ainvoke(step_messages)
            last_output = response.content if hasattr(response, 'content') else str(response)
            accumulated_context += f"\n[{output_key}]: {last_output}\n"
            logger.info(f"Chain step {i+1}/{len(steps)} ({output_key}) completed")
        except Exception as e:
            logger.error(f"Chain step {i+1} failed: {e}")
            return AgentResult(
                content=f"[Error at step {i+1}: {e}]",
                model=llm.model,
                agent_type="chain",
                finish_reason="error",
                error=str(e),
            )

    return AgentResult(
        content=last_output,
        model=llm.model,
        agent_type="chain",
        finish_reason="stop",
    )
