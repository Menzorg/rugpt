"""Per-run runtime context shared with agent tools."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field


@dataclass
class ListDocumentsRuntimeData:
    seen_ids: set[str] = field(default_factory=set)
    # Tokens already spent on summaries in previous list_documents calls this run.
    # Only untruncated summaries that were actually shown to the model count.
    spent_summary_tokens: int = 0


@dataclass
class RagSearchRuntimeData:
    chunk_ids: set[str] = field(default_factory=set)


@dataclass
class RuntimeContext:
    """Mutable scratch state scoped to a single agent execution."""

    # Number of tools available to this agent; used by token counter to account
    # for tool schema overhead in the context window estimate.
    available_tools_count: int = 0
    # Synchronizes small runtime-state reads/writes across parallel tool calls.
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    list_documents_runtime_data: ListDocumentsRuntimeData = field(
        default_factory=ListDocumentsRuntimeData,
    )
    list_invoices_summary_tokens_spent: int = 0
    called_modals: list[dict] = field(default_factory=list)
    rag_search_runtime_data: RagSearchRuntimeData = field(
        default_factory=RagSearchRuntimeData,
    )
    # Cumulative tokens spent this run (prompt messages + RAG retrieval output).
    # Checked before each rag_search / list_documents call to prevent context overflow.
    total_tokens_spent: int = 0
    # Critical token budget cap for this run. Once reached, RAG tools are blocked
    # and summarization middleware starts compacting conversation state.
    critical_tokens_cap: int = 60000

    def is_budget_exhausted(self) -> bool:
        """Return True if the token cap has been reached. Call inside self.lock."""
        return self.total_tokens_spent >= self.critical_tokens_cap

    async def try_commit(self, spent: int, blocked_msg: str) -> str | None:
        """Atomically commit *spent* tokens if the budget allows.

        Acquires self.lock, re-checks the cap (parallel tools may have spent
        tokens since the last check), and either increments total_tokens_spent
        and returns None, or returns *blocked_msg* without committing.
        """
        async with self.lock:
            if self.is_budget_exhausted():
                return blocked_msg
            self.total_tokens_spent += spent
            return None

    async def try_reserve(self, tokens: int) -> bool:
        """Atomically reserve *tokens* if the budget allows.

        Returns True and commits the tokens when they fit within the cap.
        Returns False (without modifying the counter) when the budget is exhausted.
        """
        async with self.lock:
            if self.is_budget_exhausted():
                return False
            self.total_tokens_spent += tokens
            return True
