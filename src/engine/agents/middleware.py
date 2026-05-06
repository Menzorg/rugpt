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
from .runtime import RuntimeContext

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
        tokens = count_tokens("\n".join(_message_text(m) for m in messages), tool_count)
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
        parts = []
        for m in messages:
            role = getattr(m, "type", "message")
            content = _content_text(m.content)
            parts.append(f"{role}: {content}")
        return _COMPACTION_PROMPT.replace("{messages}", "\n\n".join(parts))

    async def _acreate_summary(self, messages: list[BaseMessage]) -> str:
        prompt = self._format_for_summary(messages)
        result = await self._llm.ainvoke(prompt)
        return str(result.content).strip()

    async def abefore_model(self, state, runtime) -> dict[str, Any] | None:
        messages: list = state["messages"]
        loop_messages = [m for m in messages if not isinstance(m, SystemMessage)]

        token_count = count_tokens_messages(loop_messages)
        if token_count < self._trigger_tokens:
            return None

        keep = self._keep_last
        to_summarise = loop_messages[:-keep]
        to_keep = loop_messages[-keep:]

        if not to_summarise:
            return None

        self._ensure_ids(messages)

        logger.info(
            "compaction middleware: %d tokens >= %d, summarising %d messages, keeping %d",
            token_count, self._trigger_tokens, len(to_summarise), keep,
        )
        try:
            summary_text = await self._acreate_summary(to_summarise)
            summary_msg = AIMessage(
                content=f"[CONVERSATION SUMMARY]\n{summary_text}",
                id=str(uuid.uuid4()),
            )
            replacement_messages = [summary_msg, *to_keep]
            new_token_count = count_tokens_messages(replacement_messages)
            if hasattr(runtime, "context") and isinstance(runtime.context, RuntimeContext):
                runtime.context.total_tokens_spent = new_token_count
                logger.info(
                    "compaction middleware: total_tokens_spent recalculated to %d after compaction",
                    new_token_count,
                )
            logger.info(
                "compaction middleware: compacted %d → %d messages (%d tokens → %d, %d chars summary)",
                len(loop_messages), len(replacement_messages), token_count, new_token_count, len(summary_text),
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
