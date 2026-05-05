"""
Agent Executor

Main router: dispatches execution to the right graph based on role.agent_type.
"""
import asyncio
import logging
from typing import Any, List, Optional, TYPE_CHECKING
from uuid import UUID

from langchain_core.runnables import RunnableConfig
from langchain_openai import ChatOpenAI

from ..config import Config
from ..models.role import Role
from ..services.prompt_cache import PromptCache
from ..utils.token_counter import count_tokens
from .result import AgentResult
from .runtime import RuntimeContext
from .tools.registry import ToolRegistry
from .graphs.simple import run_simple_agent
from .graphs.chain import run_chain_agent
from .graphs.multi_agent import run_multi_agent

if TYPE_CHECKING:
    from ..services.memory_service import MemoryService
    from ..services.correction_rule_service import CorrectionRuleService

logger = logging.getLogger("rugpt.agents.executor")

_MEMORY_PROMPT_BLOCK = """\n\nВ запросе пользователя тебе будет дана сводка диалога. В квадратных скобках единицы информации пронумерованы согласно их давности (номер меньше = информация свежее) 
Не говори пользователю о существовании сводки. 
История чата актуальнее сводки"""

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
        self.correction_rule_service: Optional["CorrectionRuleService"] = None

    def _create_llm(self, model: str, temperature: float = 0.7, max_tokens: int = 4096) -> ChatOpenAI:
        """Create a ChatOpenAI instance pointed at the LiteLLM proxy."""
        return ChatOpenAI(
            base_url=self.base_url,
            api_key=self.api_key,
            model=model,
            temperature=temperature,
            max_tokens=max_tokens,
            timeout=self.timeout,
        )

    async def _build_chat_attachments_block(
        self,
        engine: Any,
        chat_id: UUID,
        limit: int = 10,
    ) -> Optional[str]:
        """Build prompt context for recent chat attachments."""
        recent_attachment_ids = await engine.chat_storage.get_attachments(
            chat_id,
            limit=limit,
        )
        attachments_by_id = await engine.user_file_storage.get_many_by_ids(
            recent_attachment_ids,
        )

        attachment_lines = []
        for file_id in recent_attachment_ids:
            file = attachments_by_id.get(file_id)
            if file is None:
                continue
            if file.rag_status == "indexed":
                summary_text = file.summary.strip() if file.summary else "нет сводки"
                detail = f"summary: {summary_text[:100]}..."
            else:
                detail = f"status: {file.rag_status}"
            attachment_lines.append(f"- {file.original_filename} (id: {file.id}, {detail})")

        if not attachment_lines:
            return None
        return f"Вложения чата (последние {limit}):\n" + "\n".join(attachment_lines)

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

        # Fetch org_context for message injection
        from ..services.engine_service import get_engine_service
        engine = get_engine_service()

        # Resolve the INITIATOR's org — that's the scope tools should operate in.
        # role.org_id is typically the RuGPT system org for cross-org roles (PM,
        # reasoner, doc_search, web_search) and would point tools at the wrong
        # place (no real users / files there). Fall back to role.org_id only
        # when there is no initiator (e.g. scheduler-driven calls).
        scope_org_id = role.org_id
        initiator = None
        if user_id is not None:
            initiator = await engine.user_storage.get_by_id(user_id)
            if initiator and initiator.org_id:
                scope_org_id = initiator.org_id

        org = await engine.org_storage.get_by_id(scope_org_id)
        org_context = org.org_context if org else ""
        system_prompt = self.prompt_cache.get_prompt(role)
        tools, tools_doc = self.tool_registry.resolve(role.tools) if role.tools else ([], "")
        system_prompt = system_prompt.replace("{tools}", tools_doc)
        llm = self._create_llm(model, temperature)
        
        runtime_context = RuntimeContext()
        runtime_context.available_tools_count = len(tools)

        # RunnableConfig carries initiator's org_id/user_id for tools.
        config = RunnableConfig(
            max_concurrency=2,
            configurable={
                "org_id": str(scope_org_id) if scope_org_id else "",
                "user_id": str(user_id) if user_id else "",
                "is_admin": bool(initiator.is_admin) if initiator else False,
            },
        )

        # --- Retrieval phase ---

        summary = ""
        if chat_id is not None and self.memory_service is not None and messages:
            summary = await self.memory_service.get_summary_for_chat(chat_id)
            if summary:
                logger.info("memory: summary found for chat=%s (%d chars)", chat_id, len(summary))
            else:
                logger.info("memory: no summary for chat=%s", chat_id)

            resummary_needed = await self.memory_service.check_resummary_needed(chat_id)
            if resummary_needed:
                logger.info("memory: starting background update_summary for chat=%s", chat_id)
                asyncio.create_task(self.memory_service.update_summary(chat_id, messages))
            else:
                logger.info("memory: re-summarisation not needed for chat=%s", chat_id)

        lessons: list[str] = []
        if self.correction_rule_service is not None and messages:
            last_content = messages[-1].get("content", "")
            try:
                rules = await self.correction_rule_service.search_corrections(
                    user_prompt=last_content,
                    memory_text=summary or last_content,
                )
                lessons = [r.extracted_lesson for r in rules if r.extracted_lesson]
                if lessons:
                    logger.info("corrections: found %d lessons for chat=%s", len(lessons), chat_id)
                else:
                    logger.info("corrections: no lessons found for chat=%s", chat_id)
            except Exception:
                logger.exception("corrections: search failed for chat=%s", chat_id)

        # --- Injection phase ---

        if user_id is not None and initiator:
            user_lines = [
                f"ID: {initiator.id}",
                f"Имя: {initiator.name}",
                f"Логин: @{initiator.username}",
                f"Email: {initiator.email}" if initiator.email else None,
                f"Администратор: да" if initiator.is_admin else "Администратор: нет",
            ]
            if initiator.department_id:
                dept = await engine.department_storage.get_by_id(initiator.department_id)
                if dept:
                    user_lines.append(f"Отдел: {dept.name}")
                    user_lines.append(f"Руководитель отдела: {'да' if initiator.is_head else 'нет'}")
            if chat_id is not None:
                attachments_block = await self._build_chat_attachments_block(engine, chat_id)
                if attachments_block:
                    user_lines.append(attachments_block)
            user_block = "Информация о пользователе:\n" + "\n".join(l for l in user_lines if l)
            user_block += "\nНе раскрывать пользователю его ID."
            system_prompt += f"\n\n{user_block}"

        injected_messages: list[dict] = []
        if org_context:
            org_context_message = {"role": "user", "content": f"Контекст организации:\n{org_context}"}
            injected_messages.append(org_context_message)
            logger.info("org_context: prepared injected message for org=%s", scope_org_id)

        if summary:
            summary_message = {"role": "user", "content": f"Сводка истории диалога (нумерация пунктов по возрастающей давности информации):\n{summary}"}
            injected_messages.append(summary_message)
            logger.info("memory: prepared summary injected message for chat=%s", chat_id)
            system_prompt += _MEMORY_PROMPT_BLOCK

        if injected_messages:
            messages = injected_messages + messages

        if lessons:
            # Inject corrections 
            rules_block = "\n".join(f"- {lesson}" for lesson in lessons)
            system_prompt += f"\n\n## Инструкции в частных случаях:\n{rules_block}"
            logger.info("corrections: injected %d lessons for chat=%s", len(lessons), chat_id)

        # Track tokens spent by the full message list sent to the agent.
        messages_blob = "\n".join(
            f"{msg.get('role', '')}: {msg.get('content', '')}"
            for msg in messages
        )
        
        # Save number of tokens spent on the prompt + injected context, so that tools can check against the critical cap before running expensive retrievals.
        runtime_context.total_tokens_spent += count_tokens(messages_blob)

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
                    config=config,
                    context_schema=runtime_context,
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
                    config=config,
                    context_schema=runtime_context,
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
