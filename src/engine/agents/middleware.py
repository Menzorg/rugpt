"""Custom LangChain agent middleware."""

import logging
from typing import Any, Sequence

from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import BaseMessage, SystemMessage, ToolMessage

from ..utils.token_counter import count_tokens

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


def token_counter(messages: Sequence[Any]) -> int:
    return count_tokens("\n".join(_message_text(message) for message in messages))


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


class TokenBudgetMiddleware(AgentMiddleware):
    """Stop tool use once an agent run approaches the model context limit."""

    def __init__(
        self,
        max_context_tokens: int = MAX_CONTEXT_TOKENS,
        ratio: float = TOKEN_BUDGET_RATIO,
    ):
        self.max_context_tokens = max_context_tokens
        self.ratio = ratio

    def _ratio_used(self, messages: Sequence[Any]) -> float:
        return token_counter(messages) / self.max_context_tokens

    def wrap_model_call(self, request, handler):
        ratio = self._ratio_used(request.messages)

        if ratio >= self.ratio:
            logger.warning(
                "token budget middleware: blocking model tools at %.2f%% context",
                ratio * 100,
            )
            request = request.override(
                tools=[],
                system_message=append_to_system(
                    request.system_message,
                    TOKEN_BUDGET_SYSTEM_BLOCK,
                ),
            )

        return handler(request)

    async def awrap_model_call(self, request, handler):
        ratio = self._ratio_used(request.messages)

        if ratio >= self.ratio:
            logger.warning(
                "token budget middleware: blocking model tools at %.2f%% context",
                ratio * 100,
            )
            request = request.override(
                tools=[],
                system_message=append_to_system(
                    request.system_message,
                    TOKEN_BUDGET_SYSTEM_BLOCK,
                ),
            )

        return await handler(request)

    def wrap_tool_call(self, request, handler):
        ratio = self._ratio_used(request.state["messages"])

        if ratio >= self.ratio:
            logger.warning(
                "token budget middleware: blocking tool call %s at %.2f%% context",
                request.tool_call.get("name"),
                ratio * 100,
            )
            return make_blocked_tool_message(request.tool_call)

        return handler(request)

    async def awrap_tool_call(self, request, handler):
        ratio = self._ratio_used(request.state["messages"])

        if ratio >= self.ratio:
            logger.warning(
                "token budget middleware: blocking tool call %s at %.2f%% context",
                request.tool_call.get("name"),
                ratio * 100,
            )
            return make_blocked_tool_message(request.tool_call)

        return await handler(request)
