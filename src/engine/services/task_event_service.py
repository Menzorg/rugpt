"""
TaskEvent Service

Thin wrapper over TaskEventStorage: records and lists audit events for tasks.
Callers are responsible for stringifying UUIDs/datetimes in payload.
"""

from src.engine.unified_logger import get_logger
from typing import List, Optional
from uuid import UUID

from ..models.task_event import TaskEvent
from ..storage.task_event_storage import TaskEventStorage

logger = get_logger("services")

class TaskEventService:

    def __init__(self, storage: TaskEventStorage):
        self.storage = storage

    async def record(
        self,
        task_id: UUID,
        actor_user_id: Optional[UUID],
        event_type: str,
        payload: Optional[dict] = None,
    ) -> TaskEvent:
        event = TaskEvent(
            task_id=task_id,
            actor_user_id=actor_user_id,
            event_type=event_type,
            payload=payload or {},
        )
        return await self.storage.create(event)

    async def list_for_task(self, task_id: UUID, limit: int = 100) -> List[TaskEvent]:
        return await self.storage.list_by_task(task_id, limit)
