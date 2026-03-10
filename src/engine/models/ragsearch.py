from __future__ import annotations

from datetime import date, datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict


class RelatedDocSearchResult(BaseModel):
    """Result row for `search_related_docs`."""

    model_config = ConfigDict(extra="ignore")

    doc_id: UUID
    org_id: UUID
    user_id: UUID | None = None
    doc_title: str
    summary: str
    uploaded_at: datetime | None = None
    created_at: date | None = None
    vec_dist: float | None = None
    tsv_score: float | None = None
    mode_used: str


class ChunkSearchResult(BaseModel):
    """Result row for `search_rag` wrapper outputs."""

    model_config = ConfigDict(extra="ignore")

    chunk_id: UUID
    doc_id: UUID
    chunk_text: str
    vec_dist: float | None = None
    tsv_score: float | None = None
    r_vec: int | None = None
    r_tsv: int | None = None
    final_rank: float | None = None
    source_type: str | None = None

