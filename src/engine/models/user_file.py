"""
User File Model

File metadata for RAG system.
Binary data stored via StorageAdapter (local FS or S3).
"""
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional
from uuid import UUID, uuid4


@dataclass
class UserFile:
    """
    File metadata record.

    Files belong to employees (user_id).
    Uploaded by managers (uploaded_by_user_id).
    Binary data stored externally via StorageAdapter.
    """
    id: UUID = field(default_factory=uuid4)
    user_id: UUID = field(default_factory=uuid4)            # employee owner
    org_id: UUID = field(default_factory=uuid4)
    uploaded_by_user_id: UUID = field(default_factory=uuid4) # manager
    storage_key: str = ""                                    # {org_id}/{user_id}/{file_id}.{ext}
    original_filename: str = ""
    file_type: str = ""                                      # pdf | docx
    file_size: int = 0
    content_hash: Optional[str] = None                      # SHA-256 hex-дайджест содержимого файла
    summary: str = ""                                        # LLM-generated summary (written during RAG ingest)
    is_table: bool = False                                   # True when file was parsed as a structured table
    rag_status: str = "pending"                              # pending | indexing | indexed | failed
    rag_error: Optional[str] = None
    indexed_at: Optional[datetime] = None
    is_active: bool = True
    created_at: datetime = field(default_factory=datetime.utcnow)
    updated_at: datetime = field(default_factory=datetime.utcnow)

    def to_dict(self) -> dict:
        """Convert to dictionary for API response"""
        return {
            "id": str(self.id),
            "user_id": str(self.user_id),
            "org_id": str(self.org_id),
            "uploaded_by_user_id": str(self.uploaded_by_user_id),
            "storage_key": self.storage_key,
            "original_filename": self.original_filename,
            "file_type": self.file_type,
            "file_size": self.file_size,
            "content_hash": self.content_hash,
            "summary": self.summary,
            "is_table": self.is_table,
            "rag_status": self.rag_status,
            "rag_error": self.rag_error,
            "indexed_at": self.indexed_at.isoformat() if self.indexed_at else None,
            "is_active": self.is_active,
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
        }
