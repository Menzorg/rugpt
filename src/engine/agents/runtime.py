"""Per-run runtime context shared with agent tools."""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class ListDocumentsRuntimeData:
    seen_ids: set[str] = field(default_factory=set)


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
