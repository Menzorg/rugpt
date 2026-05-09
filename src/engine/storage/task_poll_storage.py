"""
Task Poll Storage

PostgreSQL CRUD for task_polls table.
"""
import json

from src.engine.unified_logger import get_logger
from datetime import datetime, date
from typing import Optional, List
from uuid import UUID

from .base import BaseStorage
from ..models.task_poll import TaskPoll

logger = get_logger("storage")

class TaskPollStorage(BaseStorage):

    async def create(self, poll: TaskPoll) -> TaskPoll:
        """Create a new task poll"""
        query = """
            INSERT INTO task_polls
                (id, org_id, assignee_user_id, poll_date, status,
                 responses, created_at, completed_at, expires_at,
                 summary, task_ids)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11)
            RETURNING *
        """
        row = await self.fetchrow(
            query,
            poll.id, poll.org_id, poll.assignee_user_id,
            poll.poll_date, poll.status,
            json.dumps(poll.responses),
            poll.created_at, poll.completed_at, poll.expires_at,
            poll.summary,
            json.dumps([str(x) for x in poll.task_ids]),
        )
        return self._row_to_poll(row)

    async def get_by_id(self, poll_id: UUID) -> Optional[TaskPoll]:
        """Get poll by ID"""
        row = await self.fetchrow(
            "SELECT * FROM task_polls WHERE id = $1",
            poll_id,
        )
        return self._row_to_poll(row) if row else None

    async def get_by_user_and_date(
        self,
        assignee_user_id: UUID,
        poll_date: date,
    ) -> Optional[TaskPoll]:
        """Get poll for a specific user and date"""
        row = await self.fetchrow(
            "SELECT * FROM task_polls WHERE assignee_user_id = $1 AND poll_date = $2",
            assignee_user_id, poll_date,
        )
        return self._row_to_poll(row) if row else None

    async def list_by_user(
        self,
        assignee_user_id: UUID,
        limit: int = 30,
    ) -> List[TaskPoll]:
        """List polls for a user, newest first"""
        query = """
            SELECT * FROM task_polls
            WHERE assignee_user_id = $1
            ORDER BY poll_date DESC
            LIMIT $2
        """
        rows = await self.fetch(query, assignee_user_id, limit)
        return [self._row_to_poll(r) for r in rows]

    async def list_by_org_and_date(
        self,
        org_id: UUID,
        poll_date: date,
    ) -> List[TaskPoll]:
        """List all polls for an org on a specific date (for evening report)"""
        query = """
            SELECT * FROM task_polls
            WHERE org_id = $1 AND poll_date = $2
            ORDER BY assignee_user_id
        """
        rows = await self.fetch(query, org_id, poll_date)
        return [self._row_to_poll(r) for r in rows]

    async def list_pending_expired(self, now: datetime) -> List[TaskPoll]:
        """List pending polls that have expired"""
        query = """
            SELECT * FROM task_polls
            WHERE status = 'pending' AND expires_at IS NOT NULL AND expires_at <= $1
        """
        rows = await self.fetch(query, now)
        return [self._row_to_poll(r) for r in rows]

    async def list_pending_today(self) -> List[TaskPoll]:
        """All polls with status='pending' AND poll_date = today (any org).

        Used by scheduler retry job (_retry_stuck_poll_initials) to find polls
        whose initial AI greeting failed to land in the chat.
        """
        query = """
            SELECT * FROM task_polls
            WHERE status = 'pending' AND poll_date = $1
        """
        rows = await self.fetch(query, date.today())
        return [self._row_to_poll(row) for row in rows]

    async def update(self, poll: TaskPoll) -> TaskPoll:
        """Update a poll (status, responses, completed_at)"""
        query = """
            UPDATE task_polls SET
                status = $2,
                responses = $3,
                completed_at = $4
            WHERE id = $1
            RETURNING *
        """
        row = await self.fetchrow(
            query,
            poll.id, poll.status,
            json.dumps(poll.responses),
            poll.completed_at,
        )
        return self._row_to_poll(row)

    async def update_summary(self, poll_id: UUID, summary: str) -> None:
        """Set the AI-generated markdown summary for a completed poll.

        Status is intentionally NOT touched here — it is managed by a
        separate operation (e.g. mark_completed).
        """
        await self.execute(
            "UPDATE task_polls SET summary = $1 WHERE id = $2",
            summary, poll_id,
        )

    async def update_status(self, poll_id: UUID, status: str) -> None:
        """Update status; stamp completed_at when transitioning to 'completed'.

        Used by the AI-driven poll-summary flow (AIService.generate_poll_summary).
        For full-poll updates (responses + status together) use update().
        """
        if status == "completed":
            await self.execute(
                "UPDATE task_polls SET status = $1, completed_at = $2 WHERE id = $3",
                status, datetime.utcnow(), poll_id,
            )
        else:
            await self.execute(
                "UPDATE task_polls SET status = $1 WHERE id = $2",
                status, poll_id,
            )

    def _row_to_poll(self, row) -> TaskPoll:
        """Map asyncpg Record to TaskPoll"""
        responses = row["responses"]
        if isinstance(responses, str):
            responses = json.loads(responses)

        keys = set(row.keys())
        summary = row["summary"] if "summary" in keys else None

        raw_task_ids = row["task_ids"] if "task_ids" in keys else None
        if isinstance(raw_task_ids, str):
            raw_task_ids = json.loads(raw_task_ids)
        if not raw_task_ids:
            raw_task_ids = []
        task_ids = [x if isinstance(x, UUID) else UUID(str(x)) for x in raw_task_ids]

        return TaskPoll(
            id=row["id"],
            org_id=row["org_id"],
            assignee_user_id=row["assignee_user_id"],
            poll_date=row["poll_date"],
            status=row["status"],
            responses=responses if responses else [],
            created_at=row["created_at"],
            completed_at=row["completed_at"],
            expires_at=row["expires_at"],
            summary=summary,
            task_ids=task_ids,
        )
