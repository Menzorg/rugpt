"""
Supervisor Agent Graph

LangGraph supervisor entrypoint. Subagent construction is intentionally
stubbed for now and will be filled in when multiagency roles are defined.
"""
import logging
from typing import Any, List, Optional

from langchain.agents import create_agent
from langchain_core.runnables import RunnableConfig
from langchain_core.tools import BaseTool
from langchain_openai import ChatOpenAI

from ..result import AgentResult, ToolCall
from ...models.role import Role
from ...utils.token_logger import log_llm_tokens, log_token_summary

logger = logging.getLogger("rugpt.agents.graphs.supervisor")


async def run_supervisor_agent(
    llm: ChatOpenAI,
    system_prompt: str,
    messages: List[dict],
    supervisor_role: Role,
    tools: Optional[List[BaseTool]] = None,
    config: Optional[RunnableConfig] = None,
    context_schema: Optional[Any] = None,
    middleware: Optional[List[Any]] = None,
    agent_config: Optional[dict] = None,
    subagent_context: str = "",
) -> AgentResult:
    """
    Run supervisor agent.

    Mirrors the simple graph call pattern, but uses langgraph-supervisor's
    create_supervisor instead of LangChain's ReAct create_agent.
    """
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

    llm_think = llm.bind(
        extra_body={
            "chat_template_kwargs": {
                "enable_thinking": True,
            }
        }
    )

    return await _supervisor_agent_call(
        llm_think,
        lc_messages,
        system_prompt,
        tools or [],
        supervisor_role,
        config,
        context_schema,
        middleware,
        agent_config or {},
        subagent_context,
    )


async def _supervisor_agent_call(
    llm: ChatOpenAI,
    messages: list,
    system_prompt: str,
    tools: List[BaseTool],
    supervisor_role: Role,
    config: Optional[RunnableConfig] = None,
    context_schema: Optional[Any] = None,
    extra_middleware: Optional[List[Any]] = None,
    agent_config: Optional[dict] = None,
    subagent_context: str = "",
) -> AgentResult:
    """Supervisor graph call with tool and future subagent support."""
    try:
        from langgraph_supervisor import create_handoff_tool, create_supervisor

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
        subagents, subagent_descriptions = await _build_subagents(
            supervisor_role=supervisor_role,
            context_schema=schema,
            subagent_context=subagent_context,
        )
        handoff_tools = [
            create_handoff_tool(
                agent_name=agent.name,
                description=subagent_descriptions.get(agent.name),
            )
            for agent in subagents
        ]
        supervisor_tools = [*(tools or []), *handoff_tools]
        supervisor_name = (agent_config or {}).get("supervisor_name", "supervisor")

        workflow = create_supervisor(
            subagents,
            model=llm,
            tools=supervisor_tools or None,
            prompt=system_prompt,
            context_schema=schema,
            output_mode="last_message",
            supervisor_name=supervisor_name,
            parallel_tool_calls=False,
        )
        agent = workflow.compile(name=supervisor_name)

        input_messages = (
            messages[1:]
            if messages and messages[0].get("role") == "system"
            else messages
        )

        result = await agent.ainvoke(
            {"messages": input_messages},
            config={**(config or {}), "recursion_limit": 50 * (len(extra_middleware or []) + 1)},
            context=context,
        )

        output_messages = result.get("messages", [])
        tool_calls = []
        final_content = ""
        grand_total = 0

        for msg in output_messages:
            if hasattr(msg, "tool_calls") and msg.tool_calls:
                for tc in msg.tool_calls:
                    tool_calls.append(
                        ToolCall(
                            tool_name=tc.get("name", ""),
                            tool_input=tc.get("args", {}),
                        )
                    )

            if hasattr(msg, "content") and msg.type == "ai" and not getattr(msg, "tool_calls", None):
                final_content = msg.content

            if hasattr(msg, "usage_metadata") and msg.type == "ai":
                step_label = (
                    f"supervisor.step[tool={'yes' if getattr(msg, 'tool_calls', None) else 'no'}]"
                )
                spent = log_llm_tokens(
                    msg,
                    label=step_label,
                    logger=logger,
                    running_total=grand_total,
                )
                grand_total += spent

        log_token_summary("supervisor.agent_call", grand_total, logger=logger)

        if not final_content and output_messages:
            last = output_messages[-1]
            final_content = last.content if hasattr(last, "content") else str(last)

        return AgentResult(
            content=final_content,
            model=llm.model,
            agent_type="supervisor",
            tool_calls=tool_calls,
            finish_reason="stop",
            tokens_used=grand_total,
        )

    except Exception as e:
        logger.error(f"Supervisor agent failed: {e}")
        return AgentResult(
            content=f"[Error: {e}]",
            model=llm.model,
            agent_type="supervisor",
            finish_reason="error",
            error=str(e),
        )


async def _build_subagents(
    supervisor_role: Role,
    context_schema: Optional[Any],
    subagent_context: str,
) -> tuple[list, dict[str, str]]:
    """Build allowed subagents for a supervisor role."""
    from ...services.engine_service import get_engine_service

    engine = get_engine_service()
    subagent_roles = await engine.role_subagent_service.get_available_subagent_roles(
        supervisor_role.id,
    )

    subagents = []
    subagent_descriptions = {}
    for role in subagent_roles:
        agent_name = _agent_name(role.code)
        subagent_descriptions[agent_name] = (
            role.as_subagent_description
            or role.description
            or f"Ask role '{role.name or role.code}' for help"
        )
        subagent_prompt = engine.prompt_cache.get_prompt(role, is_subagent=True)

        role_tools, tools_doc = (
            engine.tool_registry.resolve(role.tools)
            if role.tools
            else ([], "")
        )
        subagent_prompt = subagent_prompt.replace("{tools}", tools_doc)
        wrapped_subagent_context = (
            f"<context>\n{subagent_context}\n</context>"
            if subagent_context
            else ""
        )
        subagent_prompt = "\n\n".join(
            part
            for part in [
                subagent_prompt,
                wrapped_subagent_context,
                _SUBAGENT_DELEGATION_REMARK,
            ]
            if part
        )
        subagent_llm = engine.agent_executor._create_llm(role.model_name)
        subagents.append(
            create_agent(
                model=subagent_llm,
                tools=role_tools,
                system_prompt=subagent_prompt,
                context_schema=context_schema,
                name=agent_name,
            )
        )

    return subagents, subagent_descriptions


def _agent_name(role_code: str) -> str:
    return "".join(ch if ch.isalnum() or ch == "_" else "_" for ch in role_code).lower()


_SUBAGENT_DELEGATION_REMARK = (
    "Этот вызов сделал AI-ассистент пользователя, которому адресовано обращение. "
    "Ответь максимально полно, конкретно и самодостаточно, чтобы ассистенту не пришлось "
    "вызывать тебя повторно с тем же запросом."
)
