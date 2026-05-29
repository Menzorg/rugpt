"""
User File Folder Model

Personal folder for organizing user files.
Adjacency list (parent_folder_id), soft-delete via is_active.
"""
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional
from uuid import UUID, uuid4


@dataclass
class UserFileFolder:
    """
    Folder metadata record. Personal (owned by user_id).
    parent_folder_id IS NULL → root-level folder.
    """
    id: UUID = field(default_factory=uuid4)
    user_id: UUID = field(default_factory=uuid4)
    org_id: UUID = field(default_factory=uuid4)
    parent_folder_id: Optional[UUID] = None
    name: str = ""
    is_active: bool = True
    created_at: datetime = field(default_factory=datetime.utcnow)
    updated_at: datetime = field(default_factory=datetime.utcnow)

    def to_dict(self) -> dict:
        return {
            "id": str(self.id),
            "user_id": str(self.user_id),
            "org_id": str(self.org_id),
            "parent_folder_id": str(self.parent_folder_id) if self.parent_folder_id else None,
            "name": self.name,
            "is_active": self.is_active,
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
        }
