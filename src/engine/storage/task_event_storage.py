"""
TaskEvent Storage

Audit trail for task changes.
"""
import json

from src.engine.unified_logger import get_logger
from typing import List
from uuid import UUID

from .base import BaseStorage
from ..models.task_event import TaskEvent

logger = get_logger("storage")

class TaskEventStorage(BaseStorage):

    async def create(self, event: TaskEvent) -> TaskEvent:
        query = """
            INSERT INTO task_events
                (id, task_id, actor_user_id, event_type, payload, created_at)
            VALUES ($1, $2, $3, $4, $5::jsonb, $6)
            RETURNING *
        """
        row = await self.fetchrow(
            query,
            event.id, event.task_id, event.actor_user_id,
            event.event_type, json.dumps(event.payload or {}),
            event.created_at,
        )
        return self._row_to_event(row)

    async def list_by_task(self, task_id: UUID, limit: int = 100) -> List[TaskEvent]:
        query = """
            SELECT * FROM task_events
            WHERE task_id = $1
            ORDER BY created_at DESC
            LIMIT $2
        """
        rows = await self.fetch(query, task_id, limit)
        return [self._row_to_event(r) for r in rows]

    def _row_to_event(self, row) -> TaskEvent:
        payload = row["payload"]
        if isinstance(payload, str):
            payload = json.loads(payload)
        return TaskEvent(
            id=row["id"],
            task_id=row["task_id"],
            actor_user_id=row["actor_user_id"],
            event_type=row["event_type"],
            payload=payload or {},
            created_at=row["created_at"],
        )
