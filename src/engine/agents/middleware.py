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
    Count tokens for a list of messages, preferring API-reported usage_metadata
    on AIMessage objects and falling back to the local tokenizer for everything else.

    Returns (token_count, source) where source is "api" if every AI message had
    usage_metadata, "mixed" if some did and some didn't, or "estimator" if none did.
    """
    api_total = 0
    est_total = 0
    ai_msgs_with_api = 0
    ai_msgs_total = 0

    non_ai_text_parts = []

    for m in messages:
        if isinstance(m, AIMessage):
            ai_msgs_total += 1
            meta = getattr(m, "usage_metadata", None) or {}
            if meta:
                ai_msgs_with_api += 1
                api_total += meta.get("total_tokens", 0) or (
                    meta.get("input_tokens", 0) + meta.get("output_tokens", 0)
                )
            else:
                # No API data — estimate this message's content
                content = m.content
                if isinstance(content, list):
                    text = "\n".join(
                        item["text"] if isinstance(item, dict) and "text" in item else str(item)
                        for item in content
                    )
                else:
                    text = str(content)
                est_total += count_tokens(text)
        else:
            # HumanMessage, ToolMessage, SystemMessage — always estimate
            content = getattr(m, "content", "") or ""
            if isinstance(content, list):
                text = "\n".join(
                    item["text"] if isinstance(item, dict) and "text" in item else str(item)
                    for item in content
                )
            else:
                text = str(content)
            non_ai_text_parts.append(text)

    if non_ai_text_parts:
        est_total += count_tokens("\n".join(non_ai_text_parts))

    total = api_total + est_total

    if ai_msgs_total == 0 or ai_msgs_with_api == 0:
        source = "estimator"
    elif ai_msgs_with_api == ai_msgs_total:
        source = "api+estimator" if non_ai_text_parts else "api"
    else:
        source = "mixed"

    return total, source

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

    Use this for agents that have document-heavy tools (list_global_documents, list_private_documents, rag_search)
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

    async def abefore_model(self, state, runtime) -> dict[str, Any] | None:
        messages: list = state["messages"]
        loop_messages = [m for m in messages if not isinstance(m, SystemMessage)]

        token_count, token_source = _count_tokens_messages_with_api_fallback(loop_messages)
        if token_count < self._trigger_tokens:
            return None

        keep = self._keep_last
        to_summarise = loop_messages[:-keep]
        to_keep = loop_messages[-keep:]

        if not to_summarise:
            return None

        self._ensure_ids(messages)

        logger.info(
            "compaction middleware: %d tokens [source=%s] >= %d, summarising %d messages, keeping %d",
            token_count, token_source, self._trigger_tokens, len(to_summarise), keep,
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
