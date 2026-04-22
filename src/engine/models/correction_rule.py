"""
Correction Rule Model

Represents a correction rule linking an AI role's mistake to a memory snapshot,
source messages, and the extracted lesson.
"""
from dataclasses import dataclass, field
from typing import List, Optional
from uuid import UUID, uuid4


@dataclass
class CorrectionRule:
    """
    Correction rule created when a user provides feedback on an AI response.

    Links together:
    - The role that made the mistake (role_id)
    - The memory snapshot in use at the time (mem_id + mem_embedding)
    - The original user message that triggered the response (src_user_message_id + user_message_embedding)
    - The AI response that was incorrect (src_ai_response_id)
    - The user's correction and the lesson extracted from it
    """
    id: UUID = field(default_factory=uuid4)
    role_id: UUID = field(default_factory=uuid4)
    mem_id: Optional[UUID] = None                         # FK memory_snapshots
    mem_embedding: Optional[List[float]] = None           # vector(1024)
    src_user_message_id: Optional[UUID] = None            # FK messages
    user_message_embedding: Optional[List[float]] = None  # vector(1024)
    src_ai_response_id: Optional[UUID] = None             # FK messages
    user_correction_text: Optional[str] = None
    extracted_lesson: Optional[str] = None

    def to_dict(self) -> dict:
        return {
            "id": str(self.id),
            "role_id": str(self.role_id),
            "mem_id": str(self.mem_id) if self.mem_id else None,
            "mem_embedding": self.mem_embedding,
            "src_user_message_id": str(self.src_user_message_id) if self.src_user_message_id else None,
            "user_message_embedding": self.user_message_embedding,
            "src_ai_response_id": str(self.src_ai_response_id) if self.src_ai_response_id else None,
            "user_correction_text": self.user_correction_text,
            "extracted_lesson": self.extracted_lesson,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "CorrectionRule":
        def _uuid(key):
            v = data.get(key)
            if v is None:
                return None
            return UUID(v) if isinstance(v, str) else v

        return cls(
            id=_uuid("id") or uuid4(),
            role_id=_uuid("role_id") or uuid4(),
            mem_id=_uuid("mem_id"),
            mem_embedding=data.get("mem_embedding"),
            src_user_message_id=_uuid("src_user_message_id"),
            user_message_embedding=data.get("user_message_embedding"),
            src_ai_response_id=_uuid("src_ai_response_id"),
            user_correction_text=data.get("user_correction_text"),
            extracted_lesson=data.get("extracted_lesson"),
        )
