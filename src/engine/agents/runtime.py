"""Per-run runtime context shared with agent tools."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field


@dataclass
class RuntimeContext:
    """Mutable scratch state scoped to a single agent execution."""

    # Number of tools available to this agent; used by token counter to account
    # for tool schema overhead in the context window estimate.
    available_tools_count: int = 0
    # Synchronizes small runtime-state reads/writes across parallel tool calls.
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    list_invoices_summary_tokens_spent: int = 0
    called_modals: list[dict] = field(default_factory=list)
    # Cumulative tokens spent this run (prompt messages + RAG retrieval output).
    # Checked before each rag_search / list_documents call to prevent context overflow.
    total_tokens_spent: int = 0
    # Critical token budget cap for this run. Once reached, RAG tools are blocked
    # and summarization middleware starts compacting conversation state.
    critical_tokens_cap: int = 24_000
