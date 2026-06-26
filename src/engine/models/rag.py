from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Optional
from uuid import UUID


def _parse_uuid(value: Any) -> UUID:
    """Accept UUID instances or their string representation."""
    return value if isinstance(value, UUID) else UUID(str(value))


def _parse_optional_uuid(value: Any) -> Optional[UUID]:
    """Parse an optional UUID value from dict/database payloads."""
    if value is None:
        return None
    return _parse_uuid(value)


def _parse_datetime(value: Any) -> Optional[datetime]:
    """Parse optional datetime values from dict payloads."""
    if value is None or isinstance(value, datetime):
        return value
    return datetime.fromisoformat(value)


def _parse_date(value: Any) -> Optional[date]:
    """Parse optional date values from dict payloads."""
    if value is None:
        return value
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return date.fromisoformat(value)


@dataclass
class RelatedDoc:
    """Document-level search result from search_related_docs()."""
    file_id: UUID
    org_id: UUID
    user_id: Optional[UUID]
    doc_title: str
    summary: str
    comment: Optional[str]
    content_type_id: Optional[UUID]
    content_type_name: Optional[str]
    content_type_description: Optional[str]
    uploaded_at: Optional[datetime]
    created_at: Optional[date]
    vec_dist: Optional[float]
    tsv_score: Optional[float]
    mode_used: Optional[str]

    def to_dict(self) -> dict:
        """Convert to dictionary for API response."""
        return {
            "file_id": str(self.file_id),
            "org_id": str(self.org_id),
            "user_id": str(self.user_id) if self.user_id else None,
            "doc_title": self.doc_title,
            "summary": self.summary,
            "comment": self.comment,
            "content_type_id": str(self.content_type_id) if self.content_type_id else None,
            "content_type_name": self.content_type_name,
            "content_type_description": self.content_type_description,
            "uploaded_at": self.uploaded_at.isoformat() if self.uploaded_at else None,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "vec_dist": self.vec_dist,
            "tsv_score": self.tsv_score,
            "mode_used": self.mode_used,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "RelatedDoc":
        """Create from dictionary."""
        return cls(
            file_id=_parse_uuid(data["file_id"]),
            org_id=_parse_uuid(data["org_id"]),
            user_id=_parse_optional_uuid(data.get("user_id")),
            doc_title=data.get("doc_title", ""),
            summary=data.get("summary") or "",
            comment=data.get("comment"),
            content_type_id=_parse_optional_uuid(data.get("content_type_id")),
            content_type_name=data.get("content_type_name"),
            content_type_description=data.get("content_type_description"),
            uploaded_at=_parse_datetime(data.get("uploaded_at")),
            created_at=_parse_date(data.get("created_at")),
            vec_dist=data.get("vec_dist"),
            tsv_score=data.get("tsv_score"),
            mode_used=data.get("mode_used"),
        )


@dataclass
class ChunkSearchResult:
    """Chunk- or table-row-level search result from search_rag()."""
    chunk_id: UUID
    file_id: UUID
    chunk_text: str
    chunk_index: Optional[int]
    vec_dist: Optional[float]
    tsv_score: Optional[float]
    r_vec: Optional[int]
    r_tsv: Optional[int]
    final_rank: Optional[float]
    source_type: str  # 'chunk' | 'table_row'

    def to_dict(self) -> dict:
        """Convert to dictionary for API response."""
        return {
            "chunk_id": str(self.chunk_id),
            "file_id": str(self.file_id),
            "chunk_text": self.chunk_text,
            "chunk_index": self.chunk_index,
            "vec_dist": self.vec_dist,
            "tsv_score": self.tsv_score,
            "r_vec": self.r_vec,
            "r_tsv": self.r_tsv,
            "final_rank": self.final_rank,
            "source_type": self.source_type,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "ChunkSearchResult":
        """Create from dictionary."""
        return cls(
            chunk_id=_parse_uuid(data["chunk_id"]),
            file_id=_parse_uuid(data["file_id"]),
            chunk_text=data.get("chunk_text", ""),
            chunk_index=data.get("chunk_index"),
            vec_dist=data.get("vec_dist"),
            tsv_score=data.get("tsv_score"),
            r_vec=data.get("r_vec"),
            r_tsv=data.get("r_tsv"),
            final_rank=data.get("final_rank"),
            source_type=data.get("source_type", ""),
        )


@dataclass
class ChunkRow:
    """Raw row from the chunks table."""
    id: UUID
    file_id: UUID
    chunk_text: str
    metadata: dict[str, Any]
    chunk_index: Optional[int]

    def to_dict(self) -> dict:
        """Convert to dictionary for API response."""
        return {
            "id": str(self.id),
            "file_id": str(self.file_id),
            "chunk_text": self.chunk_text,
            "metadata": self.metadata,
            "chunk_index": self.chunk_index,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "ChunkRow":
        """Create from dictionary."""
        return cls(
            id=_parse_uuid(data["id"]),
            file_id=_parse_uuid(data["file_id"]),
            chunk_text=data.get("chunk_text", ""),
            metadata=data.get("metadata") or {},
            chunk_index=data.get("chunk_index"),
        )
