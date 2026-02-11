"""
Multi-Agent Graph

LangGraph StateGraph built from agent_config["graph"].
Enables complex workflows with conditional routing between nodes.

This is the most advanced agent type — Phase 5 will add UI for editing.
For now, the graph config is a JSON structure defining nodes and edges.
"""
import logging
from typing import List, Optional, TypedDict, Annotated
import operator

from langchain_core.messages import SystemMessage, HumanMessage, BaseMessage
from langchain_core.tools import BaseTool
from langchain_ollama import ChatOllama
from langgraph.graph import StateGraph, END

from ..result import AgentResult

logger = logging.getLogger("rugpt.agents.graphs.multi_agent")


class MultiAgentState(TypedDict):
    """State passed between graph nodes"""
    messages: Annotated[list[BaseMessage], operator.add]
    current_output: str
    step_outputs: dict


async def run_multi_agent(
    llm: ChatOllama,
    system_prompt: str,
    messages: List[dict],
    agent_config: dict,
    tools: Optional[List[BaseTool]] = None,
) -> AgentResult:
    """
    Run multi-agent graph from agent_config["graph"].

    Graph config structure:
    {
        "graph": {
            "nodes": [
                {"id": "analyze", "instruction": "Analyze the question"},
                {"id": "respond", "instruction": "Generate final response"}
            ],
            "edges": [
                {"from": "analyze", "to": "respond"},
                {"from": "respond", "to": "__end__"}
            ],
            "entry": "analyze"
        }
    }

    Args:
        llm: ChatOllama instance
        system_prompt: Base system prompt
        messages: Conversation history
        agent_config: Must contain "graph" with nodes/edges/entry
        tools: Optional tools (reserved)

    Returns:
        AgentResult with final output
    """
    graph_config = agent_config.get("graph", {})
    nodes = graph_config.get("nodes", [])
    edges = graph_config.get("edges", [])
    entry_point = graph_config.get("entry", "")

    if not nodes or not entry_point:
        logger.warning("Multi-agent called with empty graph, falling back to simple")
        from .simple import run_simple_agent
        return await run_simple_agent(llm, system_prompt, messages)

    try:
        # Build the StateGraph
        builder = StateGraph(MultiAgentState)

        # Add nodes
        for node in nodes:
            node_id = node["id"]
            instruction = node.get("instruction", "")
            # Create a closure for each node
            builder.add_node(node_id, _make_node_fn(llm, system_prompt, instruction))

        # Add edges
        for edge in edges:
            from_node = edge["from"]
            to_node = edge["to"]
            if to_node == "__end__":
                builder.add_edge(from_node, END)
            else:
                builder.add_edge(from_node, to_node)

        # Set entry point
        builder.set_entry_point(entry_point)

        graph = builder.compile()

        # Build initial state
        lc_messages = []
        for msg in messages:
            role = msg.get("role", "user")
            content = msg.get("content", "")
            if role == "user":
                lc_messages.append(HumanMessage(content=content))

        initial_state = {
            "messages": lc_messages,
            "current_output": "",
            "step_outputs": {},
        }

        # Run the graph
        result = await graph.ainvoke(initial_state)

        final_output = result.get("current_output", "")
        if not final_output:
            # Try to get from last message
            msgs = result.get("messages", [])
            if msgs:
                last = msgs[-1]
                final_output = last.content if hasattr(last, 'content') else str(last)

        return AgentResult(
            content=final_output,
            model=llm.model,
            agent_type="multi_agent",
            finish_reason="stop",
        )

    except Exception as e:
        logger.error(f"Multi-agent graph failed: {e}")
        return AgentResult(
            content=f"[Error: {e}]",
            model=llm.model,
            agent_type="multi_agent",
            finish_reason="error",
            error=str(e),
        )


def _make_node_fn(llm: ChatOllama, system_prompt: str, instruction: str):
    """Create an async node function for the StateGraph"""
    async def node_fn(state: MultiAgentState) -> dict:
        context = state.get("current_output", "")
        step_messages = [
            SystemMessage(content=system_prompt),
            HumanMessage(content=(
                f"{context}\n\n--- Instruction: {instruction} ---\n"
                f"Respond based on the context and instruction above."
            )),
        ]
        # Include original user messages
        step_messages = state["messages"] + step_messages

        response = await llm.ainvoke(step_messages)
        output = response.content if hasattr(response, 'content') else str(response)

        return {
            "messages": [],  # don't duplicate
            "current_output": output,
            "step_outputs": {**state.get("step_outputs", {}), instruction[:20]: output},
        }

    return node_fn
