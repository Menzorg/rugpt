"""
Helper Executor

Lightweight executor for internal helper agents.
No conversation history, no memory, no correction rules — only system prompt,
user info injection, and the single user message.
"""
import logging
from typing import Any, Dict, List, Optional, TYPE_CHECKING
from uuid import UUID

from langchain_core.runnables import RunnableConfig
from langchain_openai import ChatOpenAI

from ..config import Config
from ..services.prompt_cache import PromptCache
from ..utils.token_counter import count_tokens
from ..utils.token_logger import log_token_summary
from .result import AgentResult
from .runtime import RuntimeContext
from .tools.helper_registry import HelperRegistry
from .graphs.helper import run_helper_agent

if TYPE_CHECKING:
    pass

logger = logging.getLogger("rugpt.agents.helper_executor")


class HelperExecutor:
    """
    Executor for named helper agents.

    Each helper has:
    - a system prompt loaded from prompts/helpers/<name>.md via PromptCache
    - a fixed list of tools registered in HelperRegistry

    No conversation, no memory, no corrections. Caller provides exactly
    one user message and optional caller_user_id for user-info injection.
    """

    def __init__(
        self,
        base_url: str,
        default_model: str,
        prompt_cache: PromptCache,
        helper_registry: HelperRegistry,
        timeout: float = 300.0,
        api_key: Optional[str] = None,
    ):
        self.base_url = base_url
        self.default_model = default_model
        self.prompt_cache = prompt_cache
        self.helper_registry = helper_registry
        self.timeout = timeout
        self.api_key = api_key or Config.LLM_API_KEY

    def _create_llm(self, model: str, temperature: float = 0.7) -> ChatOpenAI:
        return ChatOpenAI(
            base_url=self.base_url,
            api_key=self.api_key,
            model=model,
            temperature=temperature,
            timeout=self.timeout,
            model_kwargs={"parallel_tool_calls": True},
        )

    async def execute(
        self,
        helper_name: str,
        user_message: str,
        configurable: Optional[Dict[str, Any]] = None,
        model: Optional[str] = None,
        temperature: float = 0.7,
    ) -> AgentResult:
        """
        Run a named helper agent.

        Args:
            helper_name: Name of the helper (must be registered in HelperRegistry).
            user_message: The single user turn to process.
            configurable: RunnableConfig configurable dict forwarded from the parent agent.
                          Tools use it for org_id / user_id / is_admin exactly as in AgentExecutor.
            model: Override model; falls back to default_model.
            temperature: Sampling temperature.

        Returns:
            AgentResult with the helper's response.
        """
        helper = self.helper_registry.get_tools(helper_name)
        if helper is None:
            return AgentResult(
                content=f"[Error: unknown helper '{helper_name}']",
                model=model or self.default_model,
                agent_type="helper",
                finish_reason="error",
                error=f"unknown helper '{helper_name}'",
            )
        tools, tools_doc = helper

        system_prompt = self.prompt_cache.get_helper_prompt(helper_name)
        system_prompt = system_prompt.replace("{tools}", tools_doc)
        resolved_model = model or self.default_model
        llm = self._create_llm(resolved_model, temperature)

        cfg = configurable or {}
        config = RunnableConfig(max_concurrency=2, configurable=cfg)

        raw_caller_id = cfg.get("caller_user_id") or cfg.get("user_id", "")
        caller_user_id: Optional[UUID] = None
        if raw_caller_id:
            try:
                caller_user_id = UUID(raw_caller_id)
            except ValueError:
                pass

        from ..services.engine_service import get_engine_service
        engine = get_engine_service()

        initiator = None
        if caller_user_id is not None:
            initiator = await engine.user_storage.get_by_id(caller_user_id)

        _is_qwen = "qwen" in resolved_model.lower()
        _inject_role = "user" if _is_qwen else "assistant"

        injected: list[dict] = []
        if initiator is not None:
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
            user_block = "Информация о пользователе:\n" + "\n".join(l for l in user_lines if l)
            user_block += "\nНе раскрывать пользователю его ID."
            injected.append({"role": _inject_role, "content": user_block})

        messages: List[dict] = injected + [{"role": "user", "content": user_message}]

        runtime_context = RuntimeContext()
        runtime_context.available_tools_count = len(tools)

        messages_blob = "\n".join(
            f"{m.get('role', '')}: {m.get('content', '')}" for m in messages
        )
        initial_tokens = count_tokens(messages_blob, tool_count=len(tools))
        runtime_context.total_tokens_spent += initial_tokens
        logger.info(
            "helper_executor: name=%s org_id=%s model=%s tools=%d initial_tokens=%d",
            helper_name, cfg.get("org_id", ""), resolved_model, len(tools), initial_tokens,
        )

        try:
            result = await run_helper_agent(
                llm=llm,
                system_prompt=system_prompt,
                messages=messages,
                tools=tools if tools else None,
                config=config,
                context_schema=runtime_context,
            )
            log_token_summary(f"helper_executor[{helper_name}]", result.tokens_used, logger=logger)
            return result
        except Exception as e:
            logger.error("Helper execution failed for '%s': %s", helper_name, e)
            return AgentResult(
                content=f"[Error: {e}]",
                model=resolved_model,
                agent_type="helper",
                finish_reason="error",
                error=str(e),
            )
