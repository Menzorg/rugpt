"""
TaskMergeRequest Storage

PostgreSQL CRUD for task_merge_requests (task merge preview + snapshot).
"""
import json
from datetime import datetime
from typing import Optional
from uuid import UUID

from src.engine.unified_logger import get_logger
from .base import BaseStorage
from ..models.task_merge_request import TaskMergeRequest

logger = get_logger("storage")


class TaskMergeRequestStorage(BaseStorage):

    async def create(self, req: TaskMergeRequest) -> TaskMergeRequest:
        query = """
            INSERT INTO task_merge_requests (
                id, org_id, actor_user_id, source_task_ids, source_snapshot,
                proposed_title, proposed_description, proposed_summary,
                status, error, created_at
            )
            VALUES ($1, $2, $3, $4::uuid[], $5::jsonb, $6, $7, $8, $9, $10, $11)
            RETURNING *
        """
        row = await self.fetchrow(
            query,
            req.id, req.org_id, req.actor_user_id,
            list(req.source_task_ids),
            json.dumps(req.source_snapshot or {}),
            req.proposed_title, req.proposed_description, req.proposed_summary,
            req.status, req.error, req.created_at,
        )
        return self._row_to_req(row)

    async def get(self, request_id: UUID) -> Optional[TaskMergeRequest]:
        row = await self.fetchrow(
            "SELECT * FROM task_merge_requests WHERE id = $1", request_id
        )
        return self._row_to_req(row) if row else None

    async def mark_applied(self, request_id: UUID, new_task_id: UUID) -> None:
        await self.execute(
            """
            UPDATE task_merge_requests
               SET status = 'applied', new_task_id = $2, applied_at = NOW()
             WHERE id = $1
            """,
            request_id, new_task_id,
        )

    async def mark_failed(self, request_id: UUID, error: str) -> None:
        await self.execute(
            "UPDATE task_merge_requests SET status = 'failed', error = $2 WHERE id = $1",
            request_id, error,
        )

    def _row_to_req(self, row) -> TaskMergeRequest:
        snapshot = row["source_snapshot"]
        if isinstance(snapshot, str):
            snapshot = json.loads(snapshot)
        return TaskMergeRequest(
            id=row["id"],
            org_id=row["org_id"],
            actor_user_id=row["actor_user_id"],
            source_task_ids=list(row["source_task_ids"] or []),
            source_snapshot=snapshot or {},
            proposed_title=row["proposed_title"],
            proposed_description=row["proposed_description"],
            proposed_summary=row["proposed_summary"],
            status=row["status"],
            new_task_id=row["new_task_id"],
            error=row["error"],
            created_at=row["created_at"],
            applied_at=row["applied_at"],
        )
