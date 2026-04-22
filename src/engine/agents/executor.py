"""
Agent Executor

Main router: dispatches execution to the right graph based on role.agent_type.
"""
import asyncio
import logging
from typing import List, Optional, TYPE_CHECKING
from uuid import UUID

from langchain_core.runnables import RunnableConfig
from langchain_openai import ChatOpenAI

from ..config import Config
from ..models.role import Role
from ..services.prompt_cache import PromptCache
from .result import AgentResult
from .tools.registry import ToolRegistry
from .graphs.simple import run_simple_agent
from .graphs.chain import run_chain_agent
from .graphs.multi_agent import run_multi_agent

if TYPE_CHECKING:
    from ..services.memory_service import MemoryService

logger = logging.getLogger("rugpt.agents.executor")


class AgentExecutor:
    """
    Central agent executor.

    Routes requests to the appropriate graph based on role.agent_type:
    - "simple": direct LLM or ReAct agent (if tools present)
    - "chain": sequential steps from agent_config["steps"]
    - "multi_agent": LangGraph StateGraph from agent_config["graph"]
    """

    def __init__(
        self,
        base_url: str,
        default_model: str,
        prompt_cache: PromptCache,
        tool_registry: Optional[ToolRegistry] = None,
        timeout: float = 300.0,
        api_key: Optional[str] = None,
        memory_service: Optional["MemoryService"] = None,
    ):
        self.base_url = base_url
        self.default_model = default_model
        self.prompt_cache = prompt_cache
        self.tool_registry = tool_registry or ToolRegistry()
        self.timeout = timeout
        self.api_key = api_key or Config.LLM_API_KEY
        self.memory_service = memory_service

    def _create_llm(self, model: str, temperature: float = 0.7) -> ChatOpenAI:
        """Create a ChatOpenAI instance pointed at the LiteLLM proxy."""
        return ChatOpenAI(
            base_url=self.base_url,
            api_key=self.api_key,
            model=model,
            temperature=temperature,
            timeout=self.timeout,
        )

    async def execute(
        self,
        role: Role,
        messages: List[dict],
        temperature: float = 0.7,
        max_tokens: int = 2048,
        user_id: Optional[UUID] = None,
        chat_id: Optional[UUID] = None,
    ) -> AgentResult:
        """
        Execute agent for a role.

        Args:
            role: Role with agent_type, agent_config, tools, prompt_file
            messages: Conversation as [{"role": "user"/"assistant", "content": "..."}]
            temperature: Sampling temperature
            max_tokens: Max tokens in response
            user_id: User ID for RAG scope (owner of the conversation)

        Returns:
            AgentResult with response
        """
        model = role.model_name or self.default_model

        # Fetch org_context for injection into system prompt
        from ..services.engine_service import get_engine_service
        engine = get_engine_service()

        # Resolve the INITIATOR's org — that's the scope tools should operate in.
        # role.org_id is typically the RuGPT system org for cross-org roles (PM,
        # reasoner, doc_search, web_search) and would point tools at the wrong
        # place (no real users / files there). Fall back to role.org_id only
        # when there is no initiator (e.g. scheduler-driven calls).
        scope_org_id = role.org_id
        if user_id is not None:
            initiator = await engine.user_storage.get_by_id(user_id)
            if initiator and initiator.org_id:
                scope_org_id = initiator.org_id

        org = await engine.org_storage.get_by_id(scope_org_id)
        org_context = org.org_context if org else ""
        system_prompt = self.prompt_cache.get_prompt(role, org_context=org_context)
        tools = self.tool_registry.resolve(role.tools) if role.tools else []
        llm = self._create_llm(model, temperature)

        # RunnableConfig carries initiator's org_id/user_id for tools.
        config = RunnableConfig(configurable={
            "org_id": str(scope_org_id) if scope_org_id else "",
            "user_id": str(user_id) if user_id else "",
        })

        summary = ""
        # Memory: inject summary into the last user message and schedule re-summarisation.
        if chat_id is not None and self.memory_service is not None and messages:
            summary = await self.memory_service.get_summary_for_chat(chat_id)
            if summary:
                logger.info("memory: summary found for chat=%s (%d chars)", chat_id, len(summary))
                last = messages[-1]
                messages = messages[:-1] + [{
                    "role": last["role"],
                    "content": f"Сводка диалога: {summary}\n\nСообщение пользователя:\n{last['content']}",
                }]
                logger.info("memory: summary injected into last message for chat=%s", chat_id)
            else:
                logger.info("memory: no summary for chat=%s", chat_id)

            resummary_needed = await self.memory_service.check_resummary_needed(chat_id)
            if resummary_needed:
                logger.info("memory: starting background update_summary for chat=%s", chat_id)
                asyncio.create_task(
                    self.memory_service.update_summary(chat_id, messages)
                )
            else:
                logger.info("memory: re-summarisation not needed for chat=%s", chat_id)

        # Inject rule to not tell user that memory is injected into his prompt
        if summary != "":
            system_prompt += "\n\n В запросе пользователя тебе будет дана сводка диалога. пользователь о ней не знает и говорить о ней пользователю не надо"
        # TODO: Load correction rules via RAG and append to system_prompt
        # When RAG is implemented, this will search for relevant rules
        # based on the user's question and inject them into the prompt:
        #
        # rules = await self.correction_rule_service.search_relevant(role.id, messages)
        # if rules:
        #     rules_block = "\n".join(f"- {r.rule_text}" for r in rules if r.rule_text)
        #     system_prompt += f"\n\n## Correction Rules\n{rules_block}"

        logger.info(
            f"Executing agent: role={role.code}, type={role.agent_type}, "
            f"model={model}, tools={len(tools)}, chat_id={chat_id}, has_memory_summary={'yes' if chat_id and summary else 'no'}"
        )

        try:
            if role.agent_type == "simple":
                return await run_simple_agent(
                    llm=llm,
                    system_prompt=system_prompt,
                    messages=messages,
                    tools=tools if tools else None,
                    max_tokens=max_tokens,
                    temperature=temperature,
                    config=config,
                )

            elif role.agent_type == "chain":
                return await run_chain_agent(
                    llm=llm,
                    system_prompt=system_prompt,
                    messages=messages,
                    agent_config=role.agent_config,
                    tools=tools if tools else None,
                    config=config,
                )

            elif role.agent_type == "multi_agent":
                return await run_multi_agent(
                    llm=llm,
                    system_prompt=system_prompt,
                    messages=messages,
                    agent_config=role.agent_config,
                    tools=tools if tools else None,
                    config=config,
                )

            else:
                logger.warning(f"Unknown agent_type '{role.agent_type}', falling back to simple")
                return await run_simple_agent(
                    llm=llm,
                    system_prompt=system_prompt,
                    messages=messages,
                    max_tokens=max_tokens,
                    temperature=temperature,
                    config=config,
                )

        except Exception as e:
            logger.error(f"Agent execution failed: {e}")
            return AgentResult(
                content=f"[Error: {e}]",
                model=model,
                agent_type=role.agent_type,
                finish_reason="error",
                error=str(e),
            )
