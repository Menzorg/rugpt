"""
Task Storage

PostgreSQL CRUD for tasks table.
"""
import logging
from datetime import datetime
from typing import Optional, List
from uuid import UUID

from .base import BaseStorage
from ..models.task import Task

logger = logging.getLogger("rugpt.storage.task")


class TaskStorage(BaseStorage):

    async def create(self, task: Task) -> Task:
        """Create a new task"""
        query = """
            INSERT INTO tasks
                (id, org_id, title, description, status,
                 assignee_user_id, deadline,
                 is_active, created_at, updated_at)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)
            RETURNING *
        """
        row = await self.fetchrow(
            query,
            task.id, task.org_id, task.title, task.description, task.status,
            task.assignee_user_id, task.deadline,
            task.is_active, task.created_at, task.updated_at,
        )
        return self._row_to_task(row)

    async def get_by_id(self, task_id: UUID) -> Optional[Task]:
        """Get task by ID"""
        row = await self.fetchrow(
            "SELECT * FROM tasks WHERE id = $1 AND is_active = true",
            task_id,
        )
        return self._row_to_task(row) if row else None

    async def list_by_assignee(
        self,
        assignee_user_id: UUID,
        status: Optional[str] = None,
    ) -> List[Task]:
        """List tasks assigned to a user, optionally filtered by status"""
        if status:
            query = """
                SELECT * FROM tasks
                WHERE assignee_user_id = $1 AND status = $2 AND is_active = true
                ORDER BY created_at DESC
            """
            rows = await self.fetch(query, assignee_user_id, status)
        else:
            query = """
                SELECT * FROM tasks
                WHERE assignee_user_id = $1 AND is_active = true
                ORDER BY created_at DESC
            """
            rows = await self.fetch(query, assignee_user_id)
        return [self._row_to_task(r) for r in rows]

    async def list_by_org(
        self,
        org_id: UUID,
        status: Optional[str] = None,
    ) -> List[Task]:
        """List all tasks in an organization"""
        if status:
            query = """
                SELECT * FROM tasks
                WHERE org_id = $1 AND status = $2 AND is_active = true
                ORDER BY created_at DESC
            """
            rows = await self.fetch(query, org_id, status)
        else:
            query = """
                SELECT * FROM tasks
                WHERE org_id = $1 AND is_active = true
                ORDER BY created_at DESC
            """
            rows = await self.fetch(query, org_id)
        return [self._row_to_task(r) for r in rows]

    async def list_active_with_deadline(self) -> List[Task]:
        """List active tasks with deadlines for overdue checking"""
        query = """
            SELECT * FROM tasks
            WHERE is_active = true
              AND deadline IS NOT NULL
              AND status NOT IN ('done', 'overdue')
            ORDER BY deadline ASC
        """
        rows = await self.fetch(query)
        return [self._row_to_task(r) for r in rows]

    async def list_active_for_polls(self, assignee_user_id: UUID) -> List[Task]:
        """List active non-done tasks for morning poll"""
        query = """
            SELECT * FROM tasks
            WHERE assignee_user_id = $1
              AND is_active = true
              AND status NOT IN ('done')
            ORDER BY deadline ASC NULLS LAST, created_at ASC
        """
        rows = await self.fetch(query, assignee_user_id)
        return [self._row_to_task(r) for r in rows]

    async def list_distinct_assignees(self) -> list:
        """
        Get distinct (assignee_user_id, org_id) pairs with active non-done tasks.
        Used by scheduler to know which users need morning polls.
        """
        query = """
            SELECT DISTINCT assignee_user_id, org_id
            FROM tasks
            WHERE is_active = true
              AND status NOT IN ('done')
        """
        rows = await self.fetch(query)
        return [(row["assignee_user_id"], row["org_id"]) for row in rows]

    async def update(self, task: Task) -> Task:
        """Update a task"""
        task.updated_at = datetime.utcnow()
        query = """
            UPDATE tasks SET
                title = $2,
                description = $3,
                status = $4,
                assignee_user_id = $5,
                deadline = $6,
                updated_at = $7
            WHERE id = $1 AND is_active = true
            RETURNING *
        """
        row = await self.fetchrow(
            query,
            task.id, task.title, task.description, task.status,
            task.assignee_user_id, task.deadline,
            task.updated_at,
        )
        return self._row_to_task(row)

    async def deactivate(self, task_id: UUID) -> bool:
        """Soft-delete a task"""
        result = await self.execute(
            "UPDATE tasks SET is_active = false, updated_at = $2 WHERE id = $1",
            task_id, datetime.utcnow(),
        )
        return "UPDATE 1" in result

    def _row_to_task(self, row) -> Task:
        """Map asyncpg Record to Task"""
        return Task(
            id=row["id"],
            org_id=row["org_id"],
            title=row["title"],
            description=row["description"],
            status=row["status"],
            assignee_user_id=row["assignee_user_id"],
            deadline=row["deadline"],
            is_active=row["is_active"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )
