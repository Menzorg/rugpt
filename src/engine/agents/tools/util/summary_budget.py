"""Shared helpers for tool outputs that include text summaries."""

from dataclasses import dataclass
from typing import Optional

from ....utils.token_counter import count_tokens, cut_text_by_token_count


@dataclass(frozen=True)
class SummaryBudgetResult:
    summary_part: str
    tokens_spent: int
    remaining_tokens: int


def per_summary_token_limit(
    remaining_tokens: int,
    summary_items_count: int,
    max_tokens_per_item: Optional[int] = None,
) -> int:
    """Return a fair per-item summary allowance for the current batch."""
    if summary_items_count <= 0:
        limit = remaining_tokens
    else:
        limit = max(1, remaining_tokens // summary_items_count)
    if max_tokens_per_item is not None:
        limit = min(limit, max_tokens_per_item)
    return limit


def format_summary_part_with_budget(
    summary: Optional[str],
    remaining_tokens: int,
    per_item_limit: int,
) -> SummaryBudgetResult:
    """Format one `summary: ...` field and account for displayed tokens."""
    if not summary:
        return SummaryBudgetResult(
            summary_part="summary: -",
            tokens_spent=0,
            remaining_tokens=remaining_tokens,
        )

    if remaining_tokens <= 0:
        return SummaryBudgetResult(
            summary_part="summary: [BUDGET EXHAUSTED]",
            tokens_spent=0,
            remaining_tokens=remaining_tokens,
        )

    summary_text = summary
    raw_tokens = count_tokens(summary_text)
    effective_limit = min(per_item_limit, remaining_tokens)

    if raw_tokens > effective_limit:
        ellipsis_tokens = count_tokens("...")
        cut_limit = max(1, effective_limit - ellipsis_tokens)
        summary_text = cut_text_by_token_count(summary_text, cut_limit).rstrip() + "..."

    tokens_for_this = count_tokens(summary_text)
    if tokens_for_this > remaining_tokens:
        return SummaryBudgetResult(
            summary_part="summary: [TOKEN BUDGET EXHAUSTED]",
            tokens_spent=0,
            remaining_tokens=remaining_tokens,
        )

    return SummaryBudgetResult(
        summary_part=f'summary: "{summary_text}"',
        tokens_spent=tokens_for_this,
        remaining_tokens=remaining_tokens - tokens_for_this,
    )
