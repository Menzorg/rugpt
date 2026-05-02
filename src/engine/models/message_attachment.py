"""
MessageAttachment — Join row between messages and user_files.

Carries position for stable display order. Optionally hydrated with the joined
UserFile so that one query can return everything UI needs.
"""
from dataclasses import dataclass
from typing import Optional, TYPE_CHECKING
from uuid import UUID

if TYPE_CHECKING:
    from .user_file import UserFile


@dataclass
class MessageAttachment:
    message_id: UUID
    file_id: UUID
    position: int = 0
    file: Optional["UserFile"] = None  # hydrated on read

    def to_dict(self) -> dict:
        """UI-shape — minimal info needed by frontend to render the attachment chip."""
        f = self.file
        return {
            "id": str(self.file_id),
            "position": self.position,
            "original_filename": f.original_filename if f else None,
            "file_size": f.file_size if f else None,
            "file_type": f.file_type if f else None,
            "is_deleted": (not f.is_active) if f else True,
        }
