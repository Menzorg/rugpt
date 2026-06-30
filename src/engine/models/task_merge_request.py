"""
TaskMergeRequest Model

One row per task-merge preview. The `task_merger` system role formulates a
proposed title/description/summary for merging several source tasks into one;
this row persists that proposal together with an as-is snapshot of the source
tasks and their chat transcripts.

The snapshot exists for recovery: if a merge turns out to be a mistake, the
original task contents and conversations can be reconstructed by hand from it.

Status flow: previewed -> applied (or failed).
"""
from dataclasses import dataclass, field
from datetime import datetime
from typing import List, Optional
from uuid import UUID, uuid4


VALID_MERGE_STATUSES = {"previewed", "applied", "failed"}


@dataclass
class TaskMergeRequest:
    id: UUID = field(default_factory=uuid4)
    org_id: UUID = field(default_factory=uuid4)
    actor_user_id: UUID = field(default_factory=uuid4)
    source_task_ids: List[UUID] = field(default_factory=list)
    source_snapshot: dict = field(default_factory=dict)
    proposed_title: Optional[str] = None
    proposed_description: Optional[str] = None
    proposed_summary: Optional[str] = None
    status: str = "previewed"
    new_task_id: Optional[UUID] = None
    error: Optional[str] = None
    created_at: datetime = field(default_factory=datetime.utcnow)
    applied_at: Optional[datetime] = None

    def to_dict(self) -> dict:
        return {
            "id": str(self.id),
            "org_id": str(self.org_id),
            "actor_user_id": str(self.actor_user_id),
            "source_task_ids": [str(t) for t in self.source_task_ids],
            "proposed_title": self.proposed_title,
            "proposed_description": self.proposed_description,
            "proposed_summary": self.proposed_summary,
            "status": self.status,
            "new_task_id": str(self.new_task_id) if self.new_task_id else None,
            "error": self.error,
            "created_at": self.created_at.isoformat(),
            "applied_at": self.applied_at.isoformat() if self.applied_at else None,
        }
