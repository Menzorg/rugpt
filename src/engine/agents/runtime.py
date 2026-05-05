"""Per-run runtime context shared with agent tools."""
from __future__ import annotations

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

    list_documents_runtime_data: ListDocumentsRuntimeData = field(
        default_factory=ListDocumentsRuntimeData,
    )
    rag_search_runtime_data: RagSearchRuntimeData = field(
        default_factory=RagSearchRuntimeData,
    )
    # Number of tools available to this agent; used by token counter to account
    # for tool schema overhead in the context window estimate.
    available_tools_count: int = 0
    # Cumulative tokens spent this run (prompt messages + RAG retrieval output).
    # Checked before each rag_search / list_documents call to prevent context overflow.
    total_tokens_spent: int = 0
    # Critical token budget cap for this run. Once reached, RAG tools are blocked
    # and summarization middleware starts compacting conversation state.
    critical_tokens_cap: int = 20_000
