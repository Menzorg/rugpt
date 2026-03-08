"""
Task Poll Storage

PostgreSQL CRUD for task_polls table.
"""
import json
import logging
from datetime import datetime, date
from typing import Optional, List
from uuid import UUID

from .base import BaseStorage
from ..models.task_poll import TaskPoll

logger = logging.getLogger("rugpt.storage.task_poll")


class TaskPollStorage(BaseStorage):

    async def create(self, poll: TaskPoll) -> TaskPoll:
        """Create a new task poll"""
        query = """
            INSERT INTO task_polls
                (id, org_id, assignee_user_id, poll_date, status,
                 responses, created_at, completed_at, expires_at)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
            RETURNING *
        """
        row = await self.fetchrow(
            query,
            poll.id, poll.org_id, poll.assignee_user_id,
            poll.poll_date, poll.status,
            json.dumps(poll.responses),
            poll.created_at, poll.completed_at, poll.expires_at,
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

    def _row_to_poll(self, row) -> TaskPoll:
        """Map asyncpg Record to TaskPoll"""
        responses = row["responses"]
        if isinstance(responses, str):
            responses = json.loads(responses)

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
        )
