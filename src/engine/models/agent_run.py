"""
AgentRun Model

Represents an async agent execution. Used for Kafka-retry idempotency:
each `agent.requests` message carries a `request_id` which maps to a row
here. Status transitions: pending -> running -> done|failed.

The CAS `pending -> running` is atomic in storage layer — if two consumers
pick up the same Kafka message, only one succeeds and actually runs the agent.
"""
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional
from uuid import UUID, uuid4


VALID_AGENT_RUN_STATUSES = {"pending", "running", "done", "failed"}


@dataclass
class AgentRun:
    request_id: UUID = field(default_factory=uuid4)
    chat_id: UUID = field(default_factory=uuid4)
    user_message_id: Optional[UUID] = None
    triggering_user_id: Optional[UUID] = None
    role_code: str = ""
    status: str = "pending"
    result_message_id: Optional[UUID] = None
    error_message: Optional[str] = None
    created_at: datetime = field(default_factory=datetime.utcnow)
    started_at: Optional[datetime] = None
    finished_at: Optional[datetime] = None

    def to_dict(self) -> dict:
        return {
            "request_id": str(self.request_id),
            "chat_id": str(self.chat_id),
            "user_message_id": str(self.user_message_id) if self.user_message_id else None,
            "triggering_user_id": str(self.triggering_user_id) if self.triggering_user_id else None,
            "role_code": self.role_code,
            "status": self.status,
            "result_message_id": str(self.result_message_id) if self.result_message_id else None,
            "error_message": self.error_message,
            "created_at": self.created_at.isoformat(),
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "finished_at": self.finished_at.isoformat() if self.finished_at else None,
        }
