"""
Token spend logger.

Call log_llm_tokens() after every LangChain LLM invocation.
It extracts usage_metadata from the response (prompt_tokens,
completion_tokens, total_tokens) and writes a structured log line
at INFO level so every token spend is visible in the rugpt log stream.

Each line also includes a local estimate produced by token_counter so you
can see how far off the estimator is compared to the real server-reported
numbers.

Usage:
    response = await llm.ainvoke(messages)
    log_llm_tokens(response, label="some_label", logger=logger, messages=messages)
"""
import logging
from typing import Any

from .token_counter import count_tokens

_root_logger = logging.getLogger("rugpt.tokens")


def _estimate_from_messages(messages: list) -> int:
    """Estimate prompt tokens from a list of message dicts or BaseMessage objects."""
    parts = []
    for m in messages:
        if isinstance(m, dict):
            parts.append(str(m.get("content", "")))
        else:
            content = getattr(m, "content", "")
            if isinstance(content, list):
                parts.extend(
                    item["text"] if isinstance(item, dict) and "text" in item else str(item)
                    for item in content
                )
            else:
                parts.append(str(content))
    return count_tokens("\n".join(parts))


def _estimate_from_response(response: Any) -> int:
    """Estimate completion tokens from a LangChain response object."""
    content = getattr(response, "content", "") or ""
    if isinstance(content, list):
        text = "\n".join(
            item["text"] if isinstance(item, dict) and "text" in item else str(item)
            for item in content
        )
    else:
        text = str(content)
    return count_tokens(text)


def log_llm_tokens(
    response: Any,
    label: str,
    logger: logging.Logger | None = None,
    running_total: int | None = None,
    messages: list | None = None,
) -> int:
    """
    Extract usage from a LangChain AIMessage and emit one log line.

    Also logs a local token_counter estimate for prompt (when *messages* is
    provided) and completion so you can track estimator accuracy over time.

    Returns the server-reported total_tokens (or prompt+completion sum).
    Returns 0 when usage_metadata is absent.
    """
    lg = logger or _root_logger
    meta = getattr(response, "usage_metadata", None) or {}
    if not meta and hasattr(response, "response_metadata"):
        # Some providers nest usage inside response_metadata.token_usage
        nested = (response.response_metadata or {}).get("token_usage") or {}
        if nested:
            meta = {
                "input_tokens": nested.get("prompt_tokens", 0),
                "output_tokens": nested.get("completion_tokens", 0),
                "total_tokens": nested.get("total_tokens", 0),
            }

    api_prompt = meta.get("input_tokens", 0)
    api_completion = meta.get("output_tokens", 0)
    api_total = meta.get("total_tokens", 0) or (api_prompt + api_completion)
    api_available = bool(meta)

    # Local estimates from token_counter
    est_prompt = _estimate_from_messages(messages) if messages is not None else None
    est_completion = _estimate_from_response(response)
    est_total = (est_prompt or 0) + est_completion

    if api_available:
        # Server reported real numbers — show both sources clearly
        if est_prompt is not None:
            est_part = (
                f"  ||  [estimator]"
                f"  prompt={est_prompt:5d}"
                f"  completion={est_completion:5d}"
                f"  total={est_total:5d}"
                f"  err={est_total - api_total:+d}"
            )
        else:
            est_part = (
                f"  ||  [estimator]"
                f"  completion={est_completion:5d}"
                f"  err={est_completion - api_completion:+d}"
            )
        source_tag = "[api]     "
        prompt, completion, total = api_prompt, api_completion, api_total
    else:
        # API gave no usage — fall back to estimator only
        est_part = ""
        source_tag = "[estimator]"
        prompt = est_prompt or 0
        completion = est_completion
        total = est_total

    running_part = f"  |  running_total={running_total + total}" if running_total is not None else ""
    lg.info(
        "TOKEN_SPEND  label=%-35s  %s  prompt=%5d  completion=%5d  total=%5d%s%s",
        label,
        source_tag,
        prompt,
        completion,
        total,
        est_part,
        running_part,
    )
    return total


def log_token_summary(
    label: str,
    grand_total: int,
    logger: logging.Logger | None = None,
) -> None:
    """Emit a final cumulative token summary line."""
    lg = logger or _root_logger
    lg.info(
        "TOKEN_TOTAL  label=%-35s  grand_total=%d",
        label,
        grand_total,
    )
