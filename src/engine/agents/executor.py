"""
Agent Executor

Main router: dispatches execution to the right graph based on role.agent_type.
"""
import asyncio

from src.engine.models.task import Task
from src.engine.models.task_poll import TaskPoll
from src.engine.unified_logger import get_logger
from typing import Any, Dict, List, Optional, TYPE_CHECKING
from uuid import UUID
from ..config import Config

from langchain.agents.middleware import ToolCallLimitMiddleware
from langchain_core.runnables import RunnableConfig
from langchain_openai import ChatOpenAI as _ChatOpenAI
from langchain_core.language_models import LanguageModelInput

MAX_CONCURRENCY = 2  # imported by tests; mirrors RunnableConfig(max_concurrency=...)


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
            # LLM starts talking bullshit to user when it adds internal content along with tool calls.
            if msg.get("tool_calls") and msg.get("content"):
                msg["content"] = ""
            if msg.get("content") is None:
                msg["content"] = ""
        return payload

from ..config import Config
from ..models.chat import ChatType
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

    async def resolve_chat_type(self, engine: Any, chat_id: UUID) -> Optional[ChatType]:
        """Return the ChatType for the given chat_id, or None if not found."""
        chat = await engine.chat_storage.get_by_id(chat_id)
        return chat.type if chat else None

    async def _build_chat_context(
        self,
        engine: Any,
        chat_id: UUID,
        attachments_limit: int = 10,
    ) -> tuple[Optional[str], Optional[str], Optional[str]]:
        """Return (chat_type_block, participants_block, attachments_block) for the chat."""
        chat = await engine.chat_storage.get_by_id(chat_id)
        if not chat:
            return None, None, None

        # --- Chat type block ---
        lines = [f"Тип чата: {chat.type.value}"]
        match chat.type:
            case ChatType.TASK:
                if chat.task_id:
                    task = await engine.task_storage.get_by_id(chat.task_id)
                    if task:
                        lines += [
                            f"  ID задачи: {task.id}",
                            f"  Название: {task.title}",
                        ]
                        if task.description:
                            lines.append(f"  Описание: {task.description}")
                        lines.append(f"  Статус: {task.status}")
                        if task.deadline:
                            lines.append(f"  Дедлайн: {task.deadline.isoformat()}")
                        if task.project_id:
                            project = await engine.project_storage.get_by_id(task.project_id)
                            if project:
                                lines.append(f"  Проект: {project.name} (ID: {project.id})")
                                if project.description:
                                    lines.append(f"  Описание проекта: {project.description}")
            case ChatType.PROJECT:
                if chat.project_id:
                    project = await engine.project_storage.get_by_id(chat.project_id)
                    if project:
                        lines += [f"  ID проекта: {project.id}", f"  Название: {project.name}"]
                        if project.description:
                            lines.append(f"  Описание: {project.description}")
            case ChatType.SUPPORT:
                if chat.support_ticket_id:
                    ticket = await engine.support_ticket_storage.get_by_id(chat.support_ticket_id)
                    if ticket:
                        def _format_dt(value: Any) -> str:
                            return value.isoformat() if value else "нет"

                        def _format_optional(value: Any) -> str:
                            return str(value) if value else "нет"

                        lines += [
                            f"  ID тикета: {ticket.id}",
                            f"  Заголовок: {_format_optional(ticket.title)}",
                            f"  Категория: {ticket.category.value}",
                            f"  Статус: {ticket.status.value}",
                            f"  ID заявителя: {ticket.requester_user_id}",
                            f"  ID организации заявителя: {ticket.requester_org_id}",
                            f"  ID оператора: {_format_optional(ticket.assignee_user_id)}",
                            f"  Первый ответ AI: {_format_dt(ticket.ai_first_response_at)}",
                            f"  Передано оператору: {_format_dt(ticket.ai_handoff_at)}",
                            f"  Закрыто: {_format_dt(ticket.closed_at)}",
                            f"  Закрыл пользователь: {_format_optional(ticket.closed_by_user_id)}",
                            f"  Закрыто ролью: {_format_optional(ticket.closed_by_role.value if ticket.closed_by_role else None)}",
                            f"  Создано: {ticket.created_at.isoformat()}",
                            f"  Обновлено: {ticket.updated_at.isoformat()}",
                        ]

                        requester = await engine.user_storage.get_by_id(ticket.requester_user_id)
                        if requester:
                            requester_details = f"{requester.name} (@{requester.username}, id: {requester.id})"
                            if requester.email:
                                requester_details += f", email: {requester.email}"
                            lines.append(f"  Заявитель: {requester_details}")

                        requester_org = await engine.org_storage.get_by_id(ticket.requester_org_id)
                        if requester_org:
                            lines.append(f"  Организация заявителя: {requester_org.name} (ID: {requester_org.id})")

                        if ticket.assignee_user_id:
                            assignee = await engine.user_storage.get_by_id(ticket.assignee_user_id)
                            if assignee:
                                assignee_details = f"{assignee.name} (@{assignee.username}, id: {assignee.id})"
                                if assignee.email:
                                    assignee_details += f", email: {assignee.email}"
                                lines.append(f"  Оператор: {assignee_details}")
                            else:
                                lines.append("  Оператор: не найден")
                        else:
                            lines.append("  Оператор: не назначен")
                    else:
                        lines.append(f"  ID тикета: {chat.support_ticket_id} (тикет не найден)")
                else:
                    lines.append("  ID тикета: не указан")
            case ChatType.POLL:
                if chat.poll_id:
                    poll :TaskPoll = await engine.task_poll_storage.get_by_id(chat.poll_id)
                    if poll:
                        lines += [
                            f"  ID опроса: {poll.id}",
                            f"  Дата опроса: {poll.poll_date.isoformat()}",
                            f"  Статус: {poll.status}",
                            f"  Создано: {poll.created_at.isoformat()}",
                        ]
                        if poll.expires_at:
                            lines.append(f"  Истекает: {poll.expires_at.isoformat()}")
                        if poll.completed_at:
                            lines.append(f"  Завершено: {poll.completed_at.isoformat()}")
                        if poll.summary:
                            lines.append(f"  Сводка: {poll.summary}")
                            
                        if poll.task_ids:
                            tasks: Dict[UUID, Task] = await engine.task_storage.get_many_by_ids(poll.task_ids)
                            for _, task in tasks.items():
                                lines.append(f"  Задача: {task.title} Описание: {task.description} Статус: {task.status} Дедлайн: {task.deadline.isoformat() if task.deadline else 'нет'}")
                    else:
                        lines.append(f"  ID опроса: {chat.poll_id} (опрос не найден)")
                else:
                    lines.append("  ID опроса: не указан")
        chat_type_block = "\n".join(lines)

        # --- Participants block ---
        participants_block: Optional[str] = None
        if chat.participants:
            participant_lines = ["Участники чата:"]
            for p_id in chat.participants:
                user = await engine.user_storage.get_by_id(p_id)
                if user is None:
                    continue
                dept = f", отдел: {user.department_name}" if user.department_name else ""
                participant_lines.append(f"  - {user.name} (code: {user.username}, id: {user.id}{dept})")
            if len(participant_lines) > 1:
                participants_block = "\n".join(participant_lines)

        # --- Attachments block ---
        attachments_block: Optional[str] = None
        recent_attachment_ids = await engine.chat_storage.get_attachments(
            chat_id,
            limit=attachments_limit,
        )
        if recent_attachment_ids:
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
            if attachment_lines:
                attachments_block = f"Вложения чата (последние {attachments_limit}):\n" + "\n".join(attachment_lines)

        return chat_type_block, participants_block, attachments_block

    async def _build_user_info_block(
        self,
        engine: Any,
        user: Any,
        title: str,
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
        block = f"{title}:\n" + "\n".join(line for line in user_lines if line)
        block += "\nНе раскрывать пользователю его ID."
        return block

    async def _build_caller_callee_blocks(
        self,
        engine: Any,
        caller: Optional[Any],
        callee: Optional[Any],
    ) -> tuple[Optional[str], Optional[str]]:
        caller_block = None
        callee_block = None

        if caller:
            caller_block = await self._build_user_info_block(
                engine,
                caller,
                "Информация о пользователе, который произвёл вызов (caller)",
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
        agent_name: Optional[str] = None,
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
            agent_name: Username marker for this AI sender; defaults to role.code for internal calls

        Returns:
            Tuple of (AgentResult, metadata). Metadata is a dict suitable for
            message persistence, e.g. {"modal": <last show_modal payload>}.
        """
        if role is None:
            raise ValueError("AgentExecutor.execute: role is None — cannot run without a role scope")
        if caller_user_id is None:
            raise ValueError("AgentExecutor.execute: caller_user_id is None — refusing to run without caller identity")

        model = role.model_name or self.default_model
        prompt_agent_name = agent_name or role.code
        
        # Qwen's chat template requires the first non-system message to be a user
        # turn. Injected context blocks use "user" role for Qwen models so the
        # template doesn't raise "No user query found in messages".
        _is_qwen = "qwen" in model.lower()
        _inject_role = "user" if _is_qwen else "assistant"
        
        litellm_session_id = resolve_litellm_session_id()
        litellm_extra_body = build_initial_extra_body(
            litellm_session_id=litellm_session_id,
            agent_name=prompt_agent_name,
            chat_id=chat_id,
        )

        # Fetch org_context for message injection
        from ..services.engine_service import get_engine_service
        engine = get_engine_service()
        
        if not chat_id is not None:
            invocation_kind = "system"
        elif invocation_kind != "mention" or callee_user_id is None:
            invocation_kind = "direct"
        # Resolve the CALLER's org — that's the scope tools should operate in.
        # so tools don't try to run in system org scope because role_org for system roles is 00000000-0000-0000-0000-000000000000.
        scope_org_id = role.org_id
        caller = await engine.user_storage.get_by_id(caller_user_id)
        if caller and caller.org_id:
            scope_org_id = caller.org_id

        callee = None
        if invocation_kind == "mention" and callee_user_id is not None:
            callee = await engine.user_storage.get_by_id(callee_user_id)

        # In direct AI chat (when callee is None) or system roles mentions tools should have caller's priveleges. 
        callee_is_system = callee is not None and callee.org_id == Config.SYSTEM_ORG_ID
        if callee is None or callee_is_system:
            callee = caller

        logger.info(f"Resolved callee id={callee.id} (username={callee.username}) for invocation_kind={invocation_kind}. Original callee_user_id={callee_user_id}."
                    f"Caller id={caller.id} (username={caller.username}) org_id={caller.org_id if caller.org_id else 'None'}")

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
            max_concurrency=MAX_CONCURRENCY,
            configurable={
                "org_id": str(scope_org_id) if scope_org_id else role.org_id,
                "caller_user_id": str(caller.id),
                "callee_user_id": str(callee.id),
                "invocation_kind": invocation_kind,
                "is_admin": bool(caller.is_admin) if caller else False,
                "timezone": org.timezone if org else "Europe/Moscow",
                "role": role,
                "chat_id": str(chat_id) if chat_id else None,
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
        )
        chat_type_block, participants_block, attachments_block = (None, None, None)
        if chat_id is not None:
            chat_type_block, participants_block, attachments_block = await self._build_chat_context(engine, chat_id)

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

        for chat_block in (chat_type_block, participants_block, attachments_block):
            if chat_block:
                context_blocks.append(chat_block)

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
        
        who_is_agent_in_chat: str = "" 
        match invocation_kind:
            case "direct":
                who_is_agent_in_chat = ("- ты являешься основным агентом в этом чате. Пользователь может подключить других только при помощи упоминания их ассистентов."
                                        " Ты сам никого больше в чат вызывать не можешь." if role.agent_type != "supervisor" else "Те ассистенты, которых можешь вызвать ты - не могут общаться с пользователем. С ними работаешь только ты."
                )
            case "mention":
                who_is_agent_in_chat = "- ты не являешься основным агентом в этом чате. Тебя сюда пригласили для конкретной работы"
        
        system_prompt += (
            "\n\n##ВАЖНЫЕ ОГРАНИЧЕНИЯ НА УРОВНЕ СИСТЕМЫ\n"
            "- любое текстовое сообщение пользователю считается финальным ответом текущего обращения.\n"
            "- при любом упоминании времени, обязательно указывай пользователю, в каком часовом поясе ты пишешь время. На русском языке. Но не пиши время без повода.\n"
            "- у тебя есть конкретный точный набор инструментов. Не выдумывай себе функционал. Тебе запрещено говорить пользователю, что ты умеешь делать то, что явно не позволяют твои инструменты.\n"
            f"- лимит вызовов инструментов за один запрос: не более {_TOTAL_TOOL_CALL_LIMIT} суммарно.{rag_limit_line}\n"
            f"- в чате сообщения разных ассистентов маркируются по системному имени отправителя. Твоё имя: {prompt_agent_name}.\n"
            + ("- mirror означает твои собственные ответы.\n" if prompt_agent_name == "mirror" else "\n")
            + f"{who_is_agent_in_chat}\n"
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
                trigger_tokens=40000,
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
