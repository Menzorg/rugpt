"""Invoice dataclass and status enum."""
from dataclasses import dataclass, field
from datetime import date, datetime
from enum import Enum
from typing import Optional
from uuid import UUID


class InvoiceStatus(str, Enum):
    CREATED = "created"
    APPROVED = "approved"
    REJECTED = "rejected"
    PROCESSED = "processed"


@dataclass
class Invoice:
    id: UUID
    org_id: UUID
    file_id: UUID
    uploaded_by_user_id: UUID
    due_date: Optional[date]
    status: InvoiceStatus
    approved_by_user_id: Optional[UUID]
    approved_at: Optional[datetime]
    rejected_at: Optional[datetime]
    rejection_reason: Optional[str]
    processed_at: Optional[datetime]
    processed_by_user_id: Optional[UUID]
    is_active: bool
    created_at: datetime
    updated_at: datetime

    def to_dict(self) -> dict:
        return {
            "id": str(self.id),
            "org_id": str(self.org_id),
            "file_id": str(self.file_id),
            "uploaded_by_user_id": str(self.uploaded_by_user_id),
            "due_date": self.due_date.isoformat() if self.due_date else None,
            "status": self.status.value,
            "approved_by_user_id": str(self.approved_by_user_id) if self.approved_by_user_id else None,
            "approved_at": self.approved_at.isoformat() if self.approved_at else None,
            "rejected_at": self.rejected_at.isoformat() if self.rejected_at else None,
            "rejection_reason": self.rejection_reason,
            "processed_at": self.processed_at.isoformat() if self.processed_at else None,
            "processed_by_user_id": str(self.processed_by_user_id) if self.processed_by_user_id else None,
            "is_active": self.is_active,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }
