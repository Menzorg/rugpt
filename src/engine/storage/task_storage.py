"""
Task Storage

PostgreSQL CRUD for tasks table.
"""
import logging
from datetime import date, datetime
from typing import Dict, Optional, List
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
                 assignee_user_id, created_by_user_id, deadline,
                 awaiting_review_at, proposed_deadline, proposed_deadline_by,
                 project_id,
                 is_active, created_at, updated_at)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, $15)
            RETURNING *
        """
        row = await self.fetchrow(
            query,
            task.id, task.org_id, task.title, task.description, task.status,
            task.assignee_user_id, task.created_by_user_id, task.deadline,
            task.awaiting_review_at, task.proposed_deadline, task.proposed_deadline_by,
            task.project_id,
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

    async def get_with_creator(self, task_id: UUID) -> Optional[dict]:
        """
        Get task by id with creator's role info (is_admin, is_head).
        Returns dict {task, creator} or None. creator is None if created_by_user_id is NULL.
        """
        query = """
            SELECT
                t.*,
                u.is_admin AS creator_is_admin,
                u.is_head AS creator_is_head,
                u.id AS creator_id,
                u.name AS creator_name
            FROM tasks t
            LEFT JOIN users u ON u.id = t.created_by_user_id
            WHERE t.id = $1 AND t.is_active = true
        """
        row = await self.fetchrow(query, task_id)
        if not row:
            return None
        return self._row_with_creator(row)

    async def list_by_assignee(
        self,
        assignee_user_id: UUID,
        status: Optional[str] = None,
    ) -> List[Task]:
        """List tasks assigned to a user, optionally filtered by status (legacy)"""
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

    async def list_by_assignee_with_priority(
        self,
        assignee_user_id: UUID,
        include_done: bool = False,
    ) -> List[dict]:
        """
        Tasks where user is assignee, sorted by priority (creator role) then deadline.
        Returns list of dicts {task, creator}.
        """
        done_filter = "" if include_done else "AND t.status != 'done'"
        query = f"""
            SELECT
                t.*,
                u.is_admin AS creator_is_admin,
                u.is_head AS creator_is_head,
                u.id AS creator_id,
                u.name AS creator_name
            FROM tasks t
            LEFT JOIN users u ON u.id = t.created_by_user_id
            WHERE t.assignee_user_id = $1 AND t.is_active = true {done_filter}
            ORDER BY
                CASE
                    WHEN u.is_admin THEN 3
                    WHEN u.is_head THEN 2
                    ELSE 1
                END DESC,
                t.deadline ASC NULLS LAST,
                t.created_at DESC
        """
        rows = await self.fetch(query, assignee_user_id)
        return [self._row_with_creator(r) for r in rows]

    async def list_by_creator_with_assignee(
        self,
        creator_user_id: UUID,
        include_done: bool = False,
    ) -> List[dict]:
        """
        Tasks where user is creator, with assignee info.
        Returns list of dicts {task, creator, assignee}.
        """
        done_filter = "" if include_done else "AND t.status != 'done'"
        query = f"""
            SELECT
                t.*,
                a.id AS assignee_id,
                a.name AS assignee_name,
                a.is_head AS assignee_is_head,
                u.is_admin AS creator_is_admin,
                u.is_head AS creator_is_head,
                u.id AS creator_id,
                u.name AS creator_name
            FROM tasks t
            LEFT JOIN users a ON a.id = t.assignee_user_id
            LEFT JOIN users u ON u.id = t.created_by_user_id
            WHERE t.created_by_user_id = $1 AND t.is_active = true {done_filter}
            ORDER BY
                t.deadline ASC NULLS LAST,
                t.created_at DESC
        """
        rows = await self.fetch(query, creator_user_id)
        result = []
        for r in rows:
            entry = self._row_with_creator(r)
            entry["assignee"] = {
                "id": r["assignee_id"],
                "name": r["assignee_name"],
                "is_head": r["assignee_is_head"],
            } if r["assignee_id"] else None
            result.append(entry)
        return result

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

    async def list_by_date_range(
        self,
        org_id: UUID,
        date_from: Optional[date] = None,
        date_to: Optional[date] = None,
    ) -> List[Task]:
        """List active tasks created within an inclusive date interval."""
        conditions = ["org_id = $1", "is_active = true"]
        params: list = [org_id]

        if date_from:
            params.append(date_from)
            conditions.append(f"created_at >= ${len(params)}::date")
        if date_to:
            params.append(date_to)
            # date_to is a date; adding 1 day gives an exclusive upper bound for the timestamp column.
            conditions.append(f"created_at < (${len(params)}::date + INTERVAL '1 day')")

        where = " AND ".join(conditions)
        rows = await self.fetch(
            f"SELECT * FROM tasks WHERE {where} ORDER BY created_at DESC",
            *params,
        )
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
                awaiting_review_at = $7,
                proposed_deadline = $8,
                proposed_deadline_by = $9,
                project_id = $10,
                updated_at = $11
            WHERE id = $1 AND is_active = true
            RETURNING *
        """
        row = await self.fetchrow(
            query,
            task.id, task.title, task.description, task.status,
            task.assignee_user_id, task.deadline,
            task.awaiting_review_at, task.proposed_deadline, task.proposed_deadline_by,
            task.project_id,
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

    async def list_archived_for_user(
        self, user_id: UUID, org_id: UUID, limit: int = 200,
    ) -> List[dict]:
        """
        Archived tasks for a user: (is_active=false OR status='done'),
        where the user is creator OR current assignee, within the same org.
        Returns dicts {task, creator, assignee} for UI display.
        Ordered by updated_at DESC.
        """
        query = """
            SELECT
                t.*,
                a.id AS assignee_id,
                a.name AS assignee_name,
                a.is_head AS assignee_is_head,
                u.is_admin AS creator_is_admin,
                u.is_head AS creator_is_head,
                u.id AS creator_id,
                u.name AS creator_name
            FROM tasks t
            LEFT JOIN users a ON a.id = t.assignee_user_id
            LEFT JOIN users u ON u.id = t.created_by_user_id
            WHERE t.org_id = $2
              AND (t.assignee_user_id = $1 OR t.created_by_user_id = $1)
              AND (t.is_active = false OR t.status = 'done')
            ORDER BY t.updated_at DESC
            LIMIT $3
        """
        rows = await self.fetch(query, user_id, org_id, limit)
        result = []
        for r in rows:
            entry = self._row_with_creator(r)
            entry["assignee"] = {
                "id": r["assignee_id"],
                "name": r["assignee_name"],
                "is_head": r["assignee_is_head"],
            } if r["assignee_id"] else None
            result.append(entry)
        return result

    async def count_active_in_project(self, project_id: UUID) -> int:
        """Count active tasks linked to a project."""
        value = await self.fetchval(
            "SELECT COUNT(*) FROM tasks WHERE project_id = $1 AND is_active = true",
            project_id,
        )
        return int(value or 0)

    async def text_search(
        self,
        org_id: UUID,
        query: str,
        limit: int = 20,
    ) -> List[Task]:
        """Full-text search over tasks in an org using the pre-built tsv column.

        Ranks results with ts_rank_cd(normalization=32): rank is divided by the
        mean harmonic distance between extents, which rewards compact matches and
        penalises documents where query terms are far apart.
        """
        rows = await self.fetch(
            """
            SELECT *, ts_rank_cd(tsv, query, 32) AS rank
            FROM tasks, plainto_tsquery('russian', $2) query
            WHERE org_id = $1
              AND is_active = true
              AND tsv @@ query
            ORDER BY rank DESC
            LIMIT $3
            """,
            org_id, query, limit,
        )
        return [self._row_to_task(row) for row in rows]

    async def get_many_by_ids(self, ids: List[UUID]) -> Dict[UUID, Task]:
        """Batch-fetch tasks by id (includes inactive for audit/reference resolution)."""
        if not ids:
            return {}
        rows = await self.fetch(
            "SELECT * FROM tasks WHERE id = ANY($1::uuid[])", list(ids),
        )
        return {row["id"]: self._row_to_task(row) for row in rows}

    def _row_to_task(self, row) -> Task:
        """Map asyncpg Record to Task"""
        keys = set(row.keys())
        project_id = row["project_id"] if "project_id" in keys else None
        return Task(
            id=row["id"],
            org_id=row["org_id"],
            title=row["title"],
            description=row["description"],
            status=row["status"],
            assignee_user_id=row["assignee_user_id"],
            created_by_user_id=row["created_by_user_id"],
            deadline=row["deadline"],
            awaiting_review_at=row["awaiting_review_at"],
            proposed_deadline=row["proposed_deadline"],
            proposed_deadline_by=row["proposed_deadline_by"],
            project_id=project_id,
            is_active=row["is_active"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    def _row_with_creator(self, row) -> dict:
        """Convert row from JOIN-with-creator query to dict {task, creator}"""
        task = self._row_to_task(row)
        creator = None
        if row["creator_id"]:
            creator = {
                "id": row["creator_id"],
                "name": row["creator_name"],
                "is_admin": row["creator_is_admin"],
                "is_head": row["creator_is_head"],
            }
        return {"task": task, "creator": creator}
