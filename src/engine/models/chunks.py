from __future__ import annotations

from datetime import date, datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

RagStatus = Literal["pending", "indexing", "finished", "failed"]


class UserFile(BaseModel):
    """Data model for table `user_files`."""

    model_config = ConfigDict(extra="ignore")

    id: UUID
    user_id: UUID
    org_id: UUID
    uploaded_by_user_id: UUID
    storage_key: str
    original_filename: str
    file_type: str
    file_size: int
    content_hash: str
    rag_status: RagStatus = "pending"
    rag_error: str | None = None
    indexed_at: datetime | None = None
    is_active: bool = True
    created_at: datetime | None = None
    updated_at: datetime | None = None


class RagDoc(BaseModel):
    """Data model for table `rag_docs`."""

    model_config = ConfigDict(extra="ignore")

    doc_id: UUID
    file_id: UUID
    org_id: UUID
    user_id: UUID | None = None
    is_table: bool = False
    doc_title: str = ""
    summary: str = ""
    summary_embedding: list[float] | None = None
    uploaded_at: datetime | None = None
    created_at: date | None = None
    tsv: str | None = None


class Chunk(BaseModel):
    """Data model for table `chunks`."""

    model_config = ConfigDict(extra="ignore")

    id: UUID
    doc_id: UUID
    chunk_text: str
    embedding: list[float]
    metadata: dict[str, Any] = Field(default_factory=dict)
    chunk_index: int | None = None
    tsv: str | None = None


class TableRowChunk(BaseModel):
    """Data model for table `tables_rows_chunks`."""

    model_config = ConfigDict(extra="ignore")

    id: UUID
    doc_id: UUID
    table_chunk_id: UUID | None = None
    row_index: int
    row_text: str
    embedding: list[float]
    tsv: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime | None = None

