"""
Agent Executor

Main router: dispatches execution to the right graph based on role.agent_type.
"""
import asyncio

from src.engine.unified_logger import get_logger
from typing import Any, List, Optional, TYPE_CHECKING
from uuid import UUID

from langchain.agents.middleware import ToolCallLimitMiddleware
from langchain_core.runnables import RunnableConfig
from langchain_openai import ChatOpenAI as _ChatOpenAI
from langchain_core.language_models import LanguageModelInput


class ChatOpenAI(_ChatOpenAI):
    """ChatOpenAI with a workaround for vLLM chat templates that can't handle
    content=null on any message (e.g. Gemma 4 Jinja template crashes on None)."""

    def _get_request_payload(
        self,
        input_: LanguageModelInput,
        *,
        stop: list[str] | None = None,
        **kwargs,
    ) -> dict:
        payload = super()._get_request_payload(input_, stop=stop, **kwargs)
        for msg in payload.get("messages", []):
            if msg.get("content") is None:
                msg["content"] = ""
        return payload

from ..config import Config
from ..models.role import Role
from ..models.user import User
from ..services.prompt_cache import PromptCache
from ..utils.token_counter import count_tokens
from ..utils.token_logger import log_token_summary
from .middleware import HistoryCompactionMiddleware
from .result import AgentResult
from .runtime import RuntimeContext
from .metadata import append_extra_body_key, build_initial_extra_body, resolve_litellm_session_id
from .tools.registry import ToolRegistry
from .graphs.simple import run_simple_agent
from .graphs.supervisor import run_supervisor_agent

if TYPE_CHECKING:
    from ..services.memory_service import MemoryService
    from ..services.correction_rule_service import CorrectionRuleService

logger = get_logger("agents")

_TOTAL_TOOL_CALL_LIMIT = 35
_RAG_SEARCH_TOOL_CALL_LIMIT = 20

_MEMORY_PROMPT_BLOCK = """\n\nВ запросе пользователя тебе будет дана сводка диалога. В квадратных скобках единицы информации пронумерованы согласно их давности (номер меньше = информация свежее) 
Не говори пользователю о существовании сводки. 
История чата актуальнее сводки"""


class AgentExecutor:
    """
    Central agent executor.

    Routes requests to the appropriate graph based on role.agent_type:
    - "simple": direct LLM or ReAct agent (if tools present)
    - "supervisor"/"multi_agent": LangGraph supervisor graph
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

    def _create_llm(
        self,
        model: str,
        temperature: float = 0.7,
        max_tokens: int = 4096,
        model_kwargs: Optional[dict] = None,
        extra_body: Optional[dict] = None,
    ) -> ChatOpenAI:
        """Create a ChatOpenAI instance pointed at the LiteLLM proxy."""
        kwargs = {
            "base_url": self.base_url,
            "api_key": self.api_key,
            "model": model,
            "temperature": temperature,
            "timeout": self.timeout,
            # "max_tokens": max_tokens,
        }
        if model_kwargs is not None:
            kwargs["model_kwargs"] = model_kwargs
        if extra_body is not None:
            kwargs["extra_body"] = extra_body
        return ChatOpenAI(**kwargs)

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

    async def _build_user_info_block(
        self,
        engine: Any,
        user: Any,
        title: str,
        attachments_block: Optional[str] = None,
    ) -> str:
        """Build injected user identity context."""
        user_lines = [
            f"ID: {user.id}",
            f"Имя: {user.name}",
            f"Логин: @{user.username}",
            f"Email: {user.email}" if user.email else None,
            f"Администратор: да" if user.is_admin else "Администратор: нет",
        ]
        if user.department_id:
            dept = await engine.department_storage.get_by_id(user.department_id)
            if dept:
                user_lines.append(f"Отдел: {dept.name}")
                user_lines.append(f"Руководитель отдела: {'да' if user.is_head else 'нет'}")
        if attachments_block:
            user_lines.append(attachments_block)
        block = f"{title}:\n" + "\n".join(line for line in user_lines if line)
        block += "\nНе раскрывать пользователю его ID."
        return block

    async def _build_caller_callee_blocks(
        self,
        engine: Any,
        caller: Optional[Any],
        callee: Optional[Any],
        *,
        chat_id: Optional[UUID],
    ) -> tuple[Optional[str], Optional[str]]:
        caller_block = None
        callee_block = None

        if caller:
            attachments_block = None
            if chat_id is not None:
                attachments_block = await self._build_chat_attachments_block(engine, chat_id)
            caller_block = await self._build_user_info_block(
                engine,
                caller,
                "Информация о пользователе, который произвёл вызов (caller)",
                attachments_block=attachments_block,
            )

        if callee:
            callee_block = await self._build_user_info_block(
                engine,
                callee,
                "Информация о пользователе, которому адресован вызов (callee)",
            )

        return caller_block, callee_block

    async def _build_memory_context_block(
        self,
        chat_id: Optional[UUID],
        messages: List[dict],
    ) -> tuple[str, str]:
        """Fetch chat memory and return raw summary plus formatted context block."""
        if chat_id is None or self.memory_service is None or not messages:
            return "", ""

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

        if not summary:
            return "", ""

        memory_block = (
            "Сводка истории диалога (нумерация пунктов по возрастающей давности информации):\n"
            f"{summary}"
        )
        logger.info("memory: prepared summary injected message for chat=%s", chat_id)
        return summary, memory_block

    async def _build_correction_rules_block(
        self,
        chat_id: Optional[UUID],
        messages: List[dict],
        memory_text: str,
        role_id: Optional[UUID],
    ) -> str:
        """Search correction rules and return a formatted system-prompt block."""
        if chat_id is None or self.correction_rule_service is None or not messages:
            return ""

        raw_content = messages[-1].get("content", "")
        # content can be a list of blocks when message contains both text and image
        if isinstance(raw_content, list):
            last_content = " ".join(
                part.get("text", "") if isinstance(part, dict) else str(part)
                for part in raw_content
            )
        else:
            last_content = raw_content or ""

        try:
            rules = await self.correction_rule_service.search_corrections(
                user_prompt=last_content,
                memory_text=memory_text or last_content,
                role_id=role_id,
            )
            lessons = [r.extracted_lesson for r in rules if r.extracted_lesson]
            if lessons:
                logger.info(
                    "corrections: found %d rules, %d usable lessons for chat=%s",
                    len(rules),
                    len(lessons),
                    chat_id,
                )
            else:
                logger.info(
                    "corrections: found %d rules, 0 usable lessons for chat=%s",
                    len(rules),
                    chat_id,
                )
                return ""
        except Exception:
            logger.exception("corrections: search failed for chat=%s", chat_id)
            return ""

        rules_block = "\n".join(f"- {lesson}" for lesson in lessons)
        logger.info("corrections: prepared %d lessons for chat=%s", len(lessons), chat_id)
        return (
            "\n\n## Корректировки поведения со стороны пользователя по предыдущим подобным обращениям:\n"
            f"{rules_block}"
        )

    def _resolve_middleware(self, tools: List[Any]) -> list[Any]:
        middleware: list[Any] = [
            ToolCallLimitMiddleware(
                run_limit=_TOTAL_TOOL_CALL_LIMIT,
                exit_behavior="continue",
            )
        ]
        logger.info("total tool call limit: run_limit=%d", _TOTAL_TOOL_CALL_LIMIT)
        if any(tool.name == "rag_search" for tool in tools):
            middleware.append(
                ToolCallLimitMiddleware(
                    tool_name="rag_search",
                    run_limit=_RAG_SEARCH_TOOL_CALL_LIMIT,
                    exit_behavior="continue",
                )
            )
            logger.info("rag_search tool call limit: run_limit=%d", _RAG_SEARCH_TOOL_CALL_LIMIT)
        return middleware

    async def route(
        self,
        messages: List[dict],
        users: List[User],
        sender_id: UUID,
        default_responder: User,
        last_active_responder: Optional[User] = None,
    ) -> User:
        """
        Ask the LLM to pick which system user should respond to the conversation.

        Resolves each user's role internally (mirror → sender's role).
        Users without a resolvable role are excluded.
        Always returns a user: the LLM choice on success, default_user on any failure.

        last_active_user: hints the router that this agent was last used in the chat.
        default_user: primary system user — used as fallback on routing failure.
        """
        import json
        from pathlib import Path
        from typing import Literal, Tuple

        from pydantic import create_model

        if not users:
            return default_responder

        from ..services.engine_service import get_engine_service
        engine = get_engine_service()

        async def _resolve(user: Any) -> Optional[Role]:
            if user.role_id:
                return await engine.role_storage.get_by_id(user.role_id)
            if user.is_system:
                sender = await engine.user_storage.get_by_id(sender_id)
                if sender and sender.role_id:
                    return await engine.role_storage.get_by_id(sender.role_id)
            return None

        candidates: List[Tuple[Any, Role]] = []
        for u in users:
            r = await _resolve(u)
            if r is not None:
                candidates.append((u, r))

        if not candidates:
            return default_responder

        system_prompt = (Path(__file__).parent.parent / "prompts" / "router.md").read_text(encoding="utf-8").strip()

        agent_codes = [r.code for _, r in candidates]
        last_active_id = last_active_responder.id if last_active_responder is not None else None
        agents_dict = {}
        for u, r in candidates:
            desc = r.agent_scope_description
            if u.id == default_responder.id:
                desc = desc + "\nЭта роль используется в чате по умолчанию"
            if last_active_id is not None and u.id == last_active_id:
                desc = desc + "\nЭта роль была последней активной в этом чате"
            agents_dict[r.code] = {"name": r.name, "description": desc}
        agents_json = json.dumps(agents_dict, ensure_ascii=False, indent=2)

        history_lines = []
        for msg in messages[-10:]:
            label = "User" if msg.get("role") == "user" else "Assistant"
            content = msg.get("content", "")
            if isinstance(content, list):
                content = " ".join(
                    p.get("text", "") if isinstance(p, dict) else str(p) for p in content
                )
            history_lines.append(f"{label}: {str(content)[:300]}")

        routing_messages = [
            {"role": "system", "content": system_prompt},
            {
                "role": "user",
                "content": (
                    f"Agents:\n{agents_json}\n\n"
                    "Conversation:\n" + "\n".join(history_lines) + "\n\nChoose agent code:"
                ),
            },
        ]

        # Constrain LLM output to exactly the known agent codes — no parsing, no clamping.
        RouterDecision = create_model(
            "RouterDecision",
            agent=(Literal[tuple(agent_codes)], ...),  # type: ignore[valid-type]
        )

        llm = self._create_llm(
            self.default_model,
            temperature=0.0,
            max_tokens=16,
            extra_body={"chat_template_kwargs": {"enable_thinking": False}},
        )
        try:
            decision = await llm.with_structured_output(RouterDecision).ainvoke(routing_messages)
            chosen_code = decision.agent
            chosen_user, chosen_role = next(
                (u, r) for u, r in candidates if r.code == chosen_code
            )
            logger.info(
                "route: selected agent=%s user=%s from %d candidates",
                chosen_code, chosen_user.id, len(candidates),
            )
            return chosen_user
        except Exception:
            logger.exception("route: failed for %d users, falling back to default_user", len(candidates))
            return default_responder

    async def execute(
        self,
        role: Role,
        messages: List[dict],
        caller_user_id: UUID,
        callee_user_id: Optional[UUID] = None,
        temperature: float = 0.7,
        max_tokens: int = 2048, # For now it breaks tool calls if set too low, so keeping it high and relying on individual tool limits and HistoryCompactionMiddleware to control token usage.
        invocation_kind: str = "direct",
        chat_id: Optional[UUID] = None,
    ) -> tuple[AgentResult, dict]:
        """
        Execute agent for a role.

        Args:
            role: Role with agent_type, agent_config, tools, prompt_file
            messages: Conversation as [{"role": "user"/"assistant", "content": "..."}]
            temperature: Sampling temperature
            max_tokens: Max tokens in response
            caller_user_id: User ID that triggered the agent run
            callee_user_id: Mentioned/responding user ID for mention calls; defaults to caller when absent
            invocation_kind: "direct", "mention", or "system"

        Returns:
            Tuple of (AgentResult, metadata). Metadata is a dict suitable for
            message persistence, e.g. {"modal": <last show_modal payload>}.
        """
        if role is None:
            raise ValueError("AgentExecutor.execute: role is None — cannot run without a role scope")
        if caller_user_id is None:
            raise ValueError("AgentExecutor.execute: caller_user_id is None — refusing to run without caller identity")

        model = role.model_name or self.default_model
        
        # Qwen's chat template requires the first non-system message to be a user
        # turn. Injected context blocks use "user" role for Qwen models so the
        # template doesn't raise "No user query found in messages".
        _is_qwen = "qwen" in model.lower()
        _inject_role = "user" if _is_qwen else "assistant"
        
        litellm_session_id = resolve_litellm_session_id()
        litellm_extra_body = build_initial_extra_body(
            litellm_session_id=litellm_session_id,
            agent_name=role.code,
            chat_id=chat_id,
        )

        # Fetch org_context for message injection
        from ..services.engine_service import get_engine_service
        engine = get_engine_service()
        
        if not chat_id is not None:
            invocation_kind = "system"
        elif invocation_kind != "mention" or callee_user_id is None:
            invocation_kind = "direct"
            
        # In direct calls callee == caller so tools always have a valid target without None checks.
        effective_callee_user_id = callee_user_id if invocation_kind == "mention" else caller_user_id
        callee = None
        if invocation_kind == "mention":
            callee = await engine.user_storage.get_by_id(effective_callee_user_id)
            
        # Resolve the CALLER's org — that's the scope tools should operate in.
        # so tools don't try to run in system org scope because role_org for system roles is 00000000-0000-0000-0000-000000000000.
        scope_org_id = role.org_id
        caller = await engine.user_storage.get_by_id(caller_user_id)
        if caller and caller.org_id:
            scope_org_id = caller.org_id
        org = await engine.org_storage.get_by_id(scope_org_id)
        
        system_prompt = self.prompt_cache.get_prompt(role, timezone=org.timezone if org else "Europe/Moscow")
        
        # Tool resolving and tool docs injection
        tools, tools_doc = self.tool_registry.resolve(role.tools) if role.tools else ([], "")
        system_prompt = system_prompt.replace("{tools}", tools_doc)
        
        llm = self._create_llm(
            model,
            temperature,
            model_kwargs=(
                {"parallel_tool_calls": role.agent_type != "supervisor"} # Supervisors are not allowed to call tools in parallel to not create whole swarm of agents at once
            ),
            extra_body=litellm_extra_body,
        )
        

        
        runtime_context = RuntimeContext(available_tools_count=len(tools))

        # RunnableConfig carries initiator/called identity for tools.
        config = RunnableConfig(
            max_concurrency=2,
            configurable={
                "org_id": str(scope_org_id) if scope_org_id else role.org_id,
                "caller_user_id": str(caller_user_id),
                "callee_user_id": str(effective_callee_user_id),
                "invocation_kind": invocation_kind,
                "is_admin": bool(caller.is_admin) if caller else False,
                "timezone": org.timezone if org else "Europe/Moscow",
                "role": role,
            },
        )

        # --- Retrieval phase ---
 
        
        org_timezone = org.timezone if org else "Europe/Moscow"
        org_context = org.org_context if org else ""
        org_context += f"\nЧасовой пояс организации: {org_timezone}"
 
        summary, memory_block = await self._build_memory_context_block(
            chat_id,
            messages,
        )
        correction_rules_block = await self._build_correction_rules_block(
            chat_id,
            messages,
            summary,
            role.id,
        )
        caller_block, callee_block = await self._build_caller_callee_blocks(
            engine,
            caller,
            callee,
            chat_id=chat_id,
        )

        # --- Injection phase ---


        context_blocks: list[str] = []
        subagent_context_blocks: list[str] = []

        
        for identity_block in (caller_block, callee_block):
            if identity_block:
                context_blocks.append(identity_block)
                subagent_context_blocks.append(identity_block)

        if org_context:
            context_blocks.append(f"Контекст организации:\n{org_context}")
            logger.info("org_context: prepared injected message for org=%s", scope_org_id)

        if memory_block:
            context_blocks.append(memory_block)
            subagent_context_blocks.append(memory_block)
            system_prompt += _MEMORY_PROMPT_BLOCK

        if correction_rules_block:
            context_blocks.append(correction_rules_block)

        context_block = "\n\n".join(context_blocks)
        subagent_context_block = "\n\n".join(subagent_context_blocks)

        # Build and inject context message
        if context_block:
            messages = [
                {
                    "role": _inject_role,
                    "content": f"<context>\n{context_block}\n</context>",
                }
            ] + messages

        # Final postfix for all prompts injection
        rag_limit_line = (
            f" Инструмент rag_search можно вызвать не более {_RAG_SEARCH_TOOL_CALL_LIMIT} раз."
            if any(tool.name == "rag_search" for tool in tools)
            else ""
        )
        system_prompt += (
            "\n\n##ВАЖНЫЕ ОГРАНИЧЕНИЯ\n"
            "- любое текстовое сообщение пользователю считается финальным ответом текущего обращения.\n"
            "- во всех случаях, когда ты применяешь числовой формат времени, всегда обязательно указывай пользователю, в каком часовом поясе ты работаешь. (на русском языке)\n"
            "- у тебя есть конкретный точный набор инструментов. Не выдумывай себе функционал. Тебе запрещено говорить пользователю, что ты умеешь делать то, что явно не позволяют твои инструменты.\n"
            f"- лимит вызовов инструментов за один запрос: не более {_TOTAL_TOOL_CALL_LIMIT} суммарно.{rag_limit_line}"
            f"\n\nВ чате сообщения ассистентов маркируются по системному имени отправителя. Твоё имя: {role.name}"
        )

        # Count tokens for the full prompt (flat text estimate + 150 per tool).
        messages_blob = "\n".join(
            f"{msg.get('role', '')}: {msg.get('content', '')}"
            for msg in messages
        )
        initial_tokens = count_tokens(messages_blob, tool_count=len(tools))
        logger.info(
            "executor: initial prompt token count=%d (model=%s, tools=%d)",
            initial_tokens, model, len(tools),
        )
        runtime_context.total_tokens_spent += initial_tokens
        
        # --- Middleware stage ---


        llm_summarizer = llm.bind(
            extra_body=append_extra_body_key(
                litellm_extra_body,
                "chat_template_kwargs",
                {"enable_thinking": True},
            )
        )

        agent_middleware = [
            HistoryCompactionMiddleware(
                llm_summarizer,
                trigger_tokens=23000,
                keep_last=3,
            )
        ]
        agent_middleware.extend(self._resolve_middleware(tools))

        logger.info(
            f"Executing agent: role={role.code}, type={role.agent_type}, "
            f"model={model}, tools={len(tools)}, chat_id={chat_id}, has_memory_summary={'yes' if chat_id and summary else 'no'}"
        )

        try:
            if role.agent_type == "simple":
                result = await run_simple_agent(
                    llm=llm,
                    system_prompt=system_prompt,
                    messages=messages,
                    tools=tools if tools else None,
                    config=config,
                    context_schema=runtime_context,
                    middleware=agent_middleware,
                    llm_extra_body=litellm_extra_body,
                )
            elif role.agent_type in {"supervisor"}:
                result = await run_supervisor_agent(
                    llm=llm,
                    system_prompt=system_prompt,
                    messages=messages,
                    supervisor_role=role,
                    tools=tools if tools else None,
                    config=config,
                    context_schema=runtime_context,
                    middleware=agent_middleware,
                    agent_config=role.agent_config,
                    subagent_context=subagent_context_block,
                    litellm_session_id=litellm_session_id,
                    chat_id=chat_id,
                    llm_extra_body=litellm_extra_body,
                )
            else:
                logger.warning(f"Unknown agent_type '{role.agent_type}', falling back to simple")
                result = await run_simple_agent(
                    llm=llm,
                    system_prompt=system_prompt,
                    messages=messages,
                    config=config,
                    context_schema=runtime_context,
                    middleware=agent_middleware,
                    llm_extra_body=litellm_extra_body,
                )

            log_token_summary(
                f"executor[role={role.code},type={role.agent_type}]",
                result.tokens_used,
                logger=logger,
            )
            # Use the runtime context's called_modals list directly
            called_modals = runtime_context.called_modals
            metadata = ({"modal": called_modals[-1]} if called_modals else {})
            logger.info(
                "executor: metadata after execution role=%s chat_id=%s metadata=%s",
                role.code,
                chat_id,
                metadata,
            )
            return result, metadata

        except Exception as e:
            logger.error(f"Agent execution failed: {e}")
            return (
                AgentResult(
                    content=f"[Error: {e}]",
                    model=model,
                    agent_type=role.agent_type,
                    finish_reason="error",
                    error=str(e),
                ),
                {},
            )
