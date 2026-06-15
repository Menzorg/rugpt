"""Custom LangChain agent middleware."""

import logging
import uuid
from pathlib import Path
from typing import Any, Sequence

from langchain.agents.middleware import AgentMiddleware
from langchain.agents.middleware.summarization import REMOVE_ALL_MESSAGES, RemoveMessage
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_openai import ChatOpenAI

from ..utils.token_counter import count_tokens, count_tokens_messages
from ..utils.token_logger import log_llm_tokens, log_token_summary
from .runtime import RuntimeContext


def _count_tokens_messages_with_api_fallback(messages: Sequence[Any]) -> tuple[int, str]:
    """
    Estimate how many tokens the current message list occupies in the context window.

    Strategy: find the last AIMessage that carries usage_metadata. Its input_tokens
    already represents the entire prompt fed to the model at that turn, so we use
    input_tokens + output_tokens as the baseline and add text-estimated tokens only
    for messages that appear after it (they have not been seen by the model yet).

    Summing total_tokens across all turns was wrong: each turn's input_tokens already
    includes everything before it, causing O(n²) inflation across tool-call rounds.

    Returns (token_count, source) where source is "api+tail" when a baseline was
    found, or "estimator" when no AIMessage had usage_metadata.
    """
    messages = list(messages)

    # Find the last AIMessage with usage_metadata to use as baseline.
    last_api_idx = -1
    for i in range(len(messages) - 1, -1, -1):
        m = messages[i]
        if isinstance(m, AIMessage) and (getattr(m, "usage_metadata", None) or {}):
            last_api_idx = i
            break

    if last_api_idx == -1:
        # No API data at all — fall back to full text estimation.
        parts = []
        for m in messages:
            content = getattr(m, "content", "") or ""
            if isinstance(content, list):
                parts.append("\n".join(
                    item["text"] if isinstance(item, dict) and "text" in item else str(item)
                    for item in content
                ))
            else:
                parts.append(str(content))
        return count_tokens("\n".join(parts)), "estimator"

    meta = getattr(messages[last_api_idx], "usage_metadata", {}) or {}
    base_tokens = meta.get("input_tokens", 0) + meta.get("output_tokens", 0)

    # Estimate tokens for messages added after the last API-reported turn.
    tail_parts = []
    for m in messages[last_api_idx + 1:]:
        content = getattr(m, "content", "") or ""
        if isinstance(content, list):
            tail_parts.append("\n".join(
                item["text"] if isinstance(item, dict) and "text" in item else str(item)
                for item in content
            ))
        else:
            tail_parts.append(str(content))
    tail_tokens = count_tokens("\n".join(tail_parts)) if tail_parts else 0

    return base_tokens + tail_tokens, "api+tail"

_COMPACTION_PROMPT = (Path(__file__).parent.parent / "prompts" / "agent_compaction_summary.md").read_text(encoding="utf-8")

logger = logging.getLogger("rugpt.agents.middleware")

MAX_CONTEXT_TOKENS = 30_000
TOKEN_BUDGET_RATIO = 0.85

TOKEN_BUDGET_SYSTEM_BLOCK = """You are near the context limit.

No more tools are available. Give the best possible final answer now
using only the information already present in the conversation.

If the task is incomplete, say what remains and ask the user whether
they want to continue in the next step."""

BLOCKED_TOOL_MESSAGE = (
    "Tool call blocked: context is nearly full. Answer from existing context."
)


def _content_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict) and isinstance(item.get("text"), str):
                parts.append(item["text"])
        return "\n".join(parts)
    return str(content)


def _message_text(message: Any) -> str:
    if isinstance(message, BaseMessage):
        role = getattr(message, "type", "message")
        content = _content_text(message.content)
    elif isinstance(message, dict):
        role = message.get("role") or message.get("type") or "message"
        content = _content_text(message.get("content", ""))
    else:
        role = getattr(message, "type", "message")
        content = _content_text(getattr(message, "content", message))
    return f"{role}: {content}"


def append_to_system(system_message: Any, block: str) -> Any:
    if system_message is None:
        return SystemMessage(content=block)
    if isinstance(system_message, SystemMessage):
        return SystemMessage(content=f"{system_message.content}\n\n{block}")
    return SystemMessage(content=f"{system_message}\n\n{block}")


def make_blocked_tool_message(tool_call: dict[str, Any]) -> ToolMessage:
    return ToolMessage(
        content=BLOCKED_TOOL_MESSAGE,
        name=tool_call.get("name"),
        tool_call_id=tool_call.get("id", ""),
    )


class TokenBudgetToolBlockMiddleware(AgentMiddleware):
    """
    Block tool calls once the context approaches the limit.

    Use this for agents that have document-heavy tools (list_documents, list_own_documents, rag_search)
    where tool schema overhead matters.  Includes available_tools_count in the
    token estimate via RuntimeContext.
    """

    def __init__(
        self,
        max_context_tokens: int = MAX_CONTEXT_TOKENS,
        ratio: float = TOKEN_BUDGET_RATIO,
        runtime_context: RuntimeContext | None = None,
    ):
        self.max_context_tokens = max_context_tokens
        self.ratio = ratio
        self.runtime_context = runtime_context

    def _ratio_used(self, messages: Sequence[Any]) -> float:
        tool_count = self.runtime_context.available_tools_count if self.runtime_context is not None else 0
        tokens, source = _count_tokens_messages_with_api_fallback(messages)
        tokens += tool_count * 150  # tool schema overhead
        logger.debug(
            "tool-block middleware: token count=%d source=%s tool_overhead=%d",
            tokens, source, tool_count * 150,
        )
        return tokens / self.max_context_tokens

    def _block_request(self, request):
        return request.override(
            tools=[],
            system_message=append_to_system(
                request.system_message,
                TOKEN_BUDGET_SYSTEM_BLOCK,
            ),
        )

    def wrap_model_call(self, request, handler):
        ratio = self._ratio_used(request.messages)
        if ratio >= self.ratio:
            logger.warning(
                "tool-block middleware: blocking model tools at %.2f%% context", ratio * 100
            )
            request = self._block_request(request)
        return handler(request)

    async def awrap_model_call(self, request, handler):
        ratio = self._ratio_used(request.messages)
        if ratio >= self.ratio:
            logger.info(
                "tool-block middleware: blocking model tools at %.2f%% context", ratio * 100
            )
            request = self._block_request(request)
        return await handler(request)

    def wrap_tool_call(self, request, handler):
        ratio = self._ratio_used(request.state["messages"])
        if ratio >= self.ratio:
            logger.info(
                "tool-block middleware: blocking tool call %s at %.2f%% context",
                request.tool_call.get("name"), ratio * 100,
            )
            return make_blocked_tool_message(request.tool_call)
        return handler(request)

    async def awrap_tool_call(self, request, handler):
        ratio = self._ratio_used(request.state["messages"])
        if ratio >= self.ratio:
            logger.info(
                "tool-block middleware: blocking tool call %s at %.2f%% context",
                request.tool_call.get("name"), ratio * 100,
            )
            return make_blocked_tool_message(request.tool_call)
        return await handler(request)


class BudgetSyncMiddleware(AgentMiddleware):
    """
    Sync RuntimeContext.total_tokens_spent from API-reported usage_metadata before
    each model call.

    When the last AIMessage carries usage_metadata (source == "api+tail"), the
    API's input_tokens is ground truth and replaces the tiktoken-based accumulator.
    When no usage_metadata is present yet (source == "estimator"), the accumulator
    is left untouched so the tiktoken prefill estimate still guards early turns.
    """

    def __init__(self, runtime_context: RuntimeContext) -> None:
        self._runtime_context = runtime_context

    def _sync(self, messages: Sequence[Any]) -> None:
        for m in reversed(messages):
            if isinstance(m, AIMessage):
                meta = getattr(m, "usage_metadata", None) or {}
                input_tokens = meta.get("input_tokens", 0)
                if input_tokens:
                    self._runtime_context.total_tokens_spent = input_tokens
                    logger.debug("budget-sync middleware: total_tokens_spent synced to %d [usage_metadata]", input_tokens)
                    return

    def wrap_model_call(self, request, handler):
        self._sync(request.messages)
        return handler(request)

    async def awrap_model_call(self, request, handler):
        self._sync(request.messages)
        return await handler(request)


class HistoryCompactionMiddleware(AgentMiddleware):
    """
    Summarise old messages when the context grows too large.

    When token count of state["messages"] exceeds *trigger_tokens*, keeps the
    last *keep_last* messages intact and replaces everything before them with a
    single HumanMessage containing a structured LLM-generated summary (user
    intent, facts, constraints, output format, all document IDs found).

    If total messages <= keep_last, keeps floor(keep_last / 2) so compaction
    always has something to summarise.
    """

    def __init__(
        self,
        llm: ChatOpenAI,
        trigger_tokens: int = 20_000,
        keep_last: int = 8,
    ) -> None:
        self._llm = llm
        self._trigger_tokens = trigger_tokens
        self._keep_last = keep_last

    def _ensure_ids(self, messages: list) -> None:
        for m in messages:
            if getattr(m, "id", None) is None:
                m.id = str(uuid.uuid4())

    def _format_for_summary(self, messages: list[BaseMessage]) -> str:
        # Qwen's chat template forbids leading assistant turns before the first
        # user message. Strip injected assistant context blocks from the front.
        if "qwen" in getattr(self._llm, "model", "").lower():
            while messages and isinstance(messages[0], AIMessage):
                messages = messages[1:]

        parts = []
        for m in messages:
            role = getattr(m, "type", "message")
            content = _content_text(m.content)
            parts.append(f"{role}: {content}")
        return _COMPACTION_PROMPT.replace("{messages}", "\n\n".join(parts))

    async def _acreate_summary(self, messages: list[BaseMessage]) -> str:
        prompt = self._format_for_summary(messages)
        result = await self._llm.ainvoke(prompt)
        total = log_llm_tokens(
            result,
            label="middleware.history_compaction_summary",
            logger=logger,
            messages=[{"content": prompt}],
        )
        log_token_summary("middleware.history_compaction_summary", total, logger=logger)
        return str(result.content).strip()

    def _prepend_latest_human_if_missing(
        self,
        loop_messages: list[BaseMessage],
        to_keep: list[BaseMessage],
    ) -> list[BaseMessage]:
        if any(isinstance(m, HumanMessage) for m in to_keep):
            return to_keep

        for m in reversed(loop_messages):
            if isinstance(m, HumanMessage):
                return [m, *to_keep]

        return to_keep

    def _log_prompt_parts(self, messages: list) -> None:
        system_msgs = [m for m in messages if isinstance(m, SystemMessage)]
        non_system_msgs = [m for m in messages if not isinstance(m, SystemMessage)]

        if system_msgs:
            system_tokens = sum(count_tokens(_content_text(m.content)) for m in system_msgs)
            logger.debug(
                "compaction middleware: system prompt tokens=%d (%d message(s))",
                system_tokens, len(system_msgs),
            )

        total_non_system = 0
        for m in non_system_msgs:
            role = getattr(m, "type", "message")
            tokens = count_tokens(_content_text(m.content))
            total_non_system += tokens
            logger.debug(
                "compaction middleware: message role=%s tokens=%d",
                role, tokens,
            )
        logger.debug(
            "compaction middleware: prompt parts total — system=%d messages=%d",
            sum(count_tokens(_content_text(m.content)) for m in system_msgs),
            total_non_system,
        )

    async def abefore_model(self, state, runtime) -> dict[str, Any] | None:
        messages: list = state["messages"]
        loop_messages = [m for m in messages if not isinstance(m, SystemMessage)]

        self._log_prompt_parts(messages)

        token_count, token_source = _count_tokens_messages_with_api_fallback(loop_messages)
        if token_count < self._trigger_tokens:
            return None

        keep = self._keep_last
        to_summarise = loop_messages[:-keep]
        to_keep = self._prepend_latest_human_if_missing(loop_messages, loop_messages[-keep:])

        if not to_summarise:
            return None

        self._ensure_ids(messages)

        logger.info(
            "compaction middleware: %d tokens [source=%s] >= %d, summarising %d messages, keeping %d",
            token_count, token_source, self._trigger_tokens, len(to_summarise), len(to_keep),
        )
        try:
            summary_text = await self._acreate_summary(to_summarise)
            
            if "qwen" in getattr(self._llm, "model", "").lower():
                summary_msg = HumanMessage(
                    content=f"[CONVERSATION SUMMARY]\n{summary_text}",
                    id=str(uuid.uuid4()),
                )
            else:
                summary_msg = AIMessage(
                    content=f"[CONVERSATION SUMMARY]\n{summary_text}",
                    id=str(uuid.uuid4()),
                )
                
            replacement_messages = [summary_msg, *to_keep]
            new_token_count, new_token_source = _count_tokens_messages_with_api_fallback(replacement_messages)
            if hasattr(runtime, "context") and isinstance(runtime.context, RuntimeContext):
                runtime.context.total_tokens_spent = new_token_count
                logger.info(
                    "compaction middleware: total_tokens_spent recalculated to %d [source=%s] after compaction",
                    new_token_count, new_token_source,
                )
            logger.info(
                "compaction middleware: compacted %d → %d messages (%d tokens [source=%s] → %d [source=%s], %d chars summary)",
                len(loop_messages), len(replacement_messages),
                token_count, token_source, new_token_count, new_token_source, len(summary_text),
            )
            return {
                "messages": [
                    RemoveMessage(id=REMOVE_ALL_MESSAGES),
                    *replacement_messages,
                ]
            }
        except Exception:
            logger.exception("compaction middleware: summarisation failed, skipping compaction")
            return None
