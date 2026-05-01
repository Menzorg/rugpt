from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Optional


@dataclass
class RelatedDoc:
    """Document-level search result from search_related_docs()."""
    file_id: str
    org_id: str
    user_id: Optional[str]
    doc_title: str
    summary: str
    uploaded_at: Optional[datetime]
    created_at: Optional[date]
    vec_dist: float
    tsv_score: float
    mode_used: str


@dataclass
class ChunkSearchResult:
    """Chunk- or table-row-level search result from search_rag()."""
    chunk_id: str
    file_id: str
    chunk_text: str
    chunk_index: Optional[int]
    vec_dist: float
    tsv_score: float
    r_vec: Optional[int]
    r_tsv: Optional[int]
    final_rank: Optional[float]
    source_type: str  # 'chunk' | 'table_row'


@dataclass
class ChunkRow:
    """Raw row from the chunks table."""
    id: str
    file_id: str
    chunk_text: str
    metadata: dict[str, Any]
    chunk_index: Optional[int]
