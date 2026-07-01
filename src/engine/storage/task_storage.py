"""
Task Storage

PostgreSQL CRUD for tasks table.
"""

from src.engine.unified_logger import get_logger
from datetime import date, datetime
from typing import Dict, Optional, List
from uuid import UUID

from .base import BaseStorage
from ..models.task import Task

logger = get_logger("storage")

class TaskStorage(BaseStorage):

    async def create(self, task: Task) -> Task:
        """Create a new task"""
        query = """
            INSERT INTO tasks
                (id, org_id, title, description, status,
                 assignee_user_id, created_by_user_id, deadline,
                 awaiting_review_at, proposed_deadline, proposed_deadline_by,
                 project_id, priority,
                 is_active, is_overdue, created_at, updated_at)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, $15, $16, $17)
            RETURNING *
        """
        row = await self.fetchrow(
            query,
            task.id, task.org_id, task.title, task.description, task.status,
            task.assignee_user_id, task.created_by_user_id, task.deadline,
            task.awaiting_review_at, task.proposed_deadline, task.proposed_deadline_by,
            task.project_id, task.priority,
            task.is_active, task.is_overdue, task.created_at, task.updated_at,
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
                u.department_id AS creator_department_id,
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
        Tasks where user is assignee, sorted by stored priority then deadline.
        Returns list of dicts {task, creator, assignee}. Assignee is included
        for completeness — UI on the "my" tab still shows the assignee column
        (the user themselves), so the field must be populated.
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
                u.department_id AS creator_department_id,
                u.id AS creator_id,
                u.name AS creator_name
            FROM tasks t
            LEFT JOIN users a ON a.id = t.assignee_user_id
            LEFT JOIN users u ON u.id = t.created_by_user_id
            WHERE t.assignee_user_id = $1 AND t.is_active = true {done_filter}
            ORDER BY
                t.priority DESC,
                t.deadline ASC NULLS LAST,
                t.created_at DESC
        """
        rows = await self.fetch(query, assignee_user_id)
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
                u.department_id AS creator_department_id,
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

    async def list_by_deadline_range(
        self,
        org_id: UUID,
        deadline_from: Optional[date] = None,
        deadline_to: Optional[date] = None,
    ) -> List[Task]:
        """List active tasks whose deadline falls within an inclusive date interval."""
        conditions = ["org_id = $1", "is_active = true", "deadline IS NOT NULL"]
        params: list = [org_id]

        if deadline_from:
            params.append(deadline_from)
            conditions.append(f"deadline >= ${len(params)}::date")
        if deadline_to:
            params.append(deadline_to)
            # Adding 1 day gives an exclusive upper bound for the timestamp column.
            conditions.append(f"deadline < (${len(params)}::date + INTERVAL '1 day')")

        where = " AND ".join(conditions)
        rows = await self.fetch(
            f"SELECT * FROM tasks WHERE {where} ORDER BY deadline ASC",
            *params,
        )
        return [self._row_to_task(r) for r in rows]

    async def list_by_created_range(
        self,
        org_id: UUID,
        created_from: Optional[date] = None,
        created_to: Optional[date] = None,
    ) -> List[Task]:
        """List active tasks whose creation timestamp falls within an inclusive date interval."""
        conditions = ["org_id = $1", "is_active = true"]
        params: list = [org_id]

        if created_from:
            params.append(created_from)
            conditions.append(f"created_at >= ${len(params)}::date")
        if created_to:
            params.append(created_to)
            conditions.append(f"created_at < (${len(params)}::date + INTERVAL '1 day')")

        where = " AND ".join(conditions)
        rows = await self.fetch(
            f"SELECT * FROM tasks WHERE {where} ORDER BY created_at DESC",
            *params,
        )
        return [self._row_to_task(r) for r in rows]

    async def list_active_with_deadline(self) -> List[Task]:
        """List active, not-yet-overdue, non-done tasks with deadlines for the
        overdue check. Overdue is an overlay flag now, so already-flagged tasks
        are skipped here rather than by status."""
        query = """
            SELECT * FROM tasks
            WHERE is_active = true
              AND deadline IS NOT NULL
              AND status != 'done'
              AND is_overdue = false
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
                priority = $11,
                is_overdue = $12,
                updated_at = $13
            WHERE id = $1 AND is_active = true
            RETURNING *
        """
        row = await self.fetchrow(
            query,
            task.id, task.title, task.description, task.status,
            task.assignee_user_id, task.deadline,
            task.awaiting_review_at, task.proposed_deadline, task.proposed_deadline_by,
            task.project_id, task.priority,
            task.is_overdue,
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

    async def set_merged_into(self, task_id: UUID, into_task_id: UUID) -> bool:
        """Mark a still-active task as merged into another (audit/redirect link).
        Guarded by is_active so a retry/race can't relink an already-closed task."""
        result = await self.execute(
            "UPDATE tasks SET merged_into_task_id = $2, updated_at = $3 "
            "WHERE id = $1 AND is_active = true",
            task_id, into_task_id, datetime.utcnow(),
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
                u.department_id AS creator_department_id,
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

    async def user_has_any_active_task_in_project(
        self, project_id: UUID, user_id: UUID,
    ) -> bool:
        """
        Returns True if user is creator, assignee, or participant of at least
        one active task in the given project. Used to decide if user should
        be removed from project chat after losing one role on one task.
        """
        value = await self.fetchval(
            """
            SELECT EXISTS (
                SELECT 1 FROM tasks t
                WHERE t.project_id = $1
                  AND t.is_active = true
                  AND (
                    t.assignee_user_id = $2
                    OR t.created_by_user_id = $2
                    OR EXISTS (
                        SELECT 1 FROM task_participants tp
                        WHERE tp.task_id = t.id AND tp.user_id = $2
                    )
                  )
            )
            """,
            project_id, user_id,
        )
        return bool(value)

    async def list_by_participant_with_priority(
        self,
        user_id: UUID,
        include_done: bool = False,
    ) -> List[dict]:
        """
        Tasks where user is in task_participants. Sorted like /my (priority + deadline).
        Returns list of dicts {task, creator, assignee} — same shape as list_by_assignee_with_priority.
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
                u.department_id AS creator_department_id,
                u.id AS creator_id,
                u.name AS creator_name
            FROM tasks t
            JOIN task_participants tp ON tp.task_id = t.id
            LEFT JOIN users a ON a.id = t.assignee_user_id
            LEFT JOIN users u ON u.id = t.created_by_user_id
            WHERE tp.user_id = $1 AND t.is_active = true {done_filter}
            ORDER BY
                t.priority DESC,
                t.deadline ASC NULLS LAST,
                t.created_at DESC
        """
        rows = await self.fetch(query, user_id)
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

    async def list_done_for_user(self, user_id: UUID) -> List[dict]:
        """
        Done tasks (status='done', is_active=true) where user is
        creator OR assignee OR participant. Used by `/tasks/done`.
        Returns dicts {task, creator, assignee} ordered by updated_at DESC.

        Uses EXISTS (not LEFT JOIN) to avoid row duplication when a user has
        multiple roles in the same task (e.g. creator + participant).
        """
        query = """
            SELECT
                t.*,
                a.id AS assignee_id,
                a.name AS assignee_name,
                a.is_head AS assignee_is_head,
                u.is_admin AS creator_is_admin,
                u.is_head AS creator_is_head,
                u.department_id AS creator_department_id,
                u.id AS creator_id,
                u.name AS creator_name
            FROM tasks t
            LEFT JOIN users a ON a.id = t.assignee_user_id
            LEFT JOIN users u ON u.id = t.created_by_user_id
            WHERE t.is_active = true AND t.status = 'done'
              AND (
                t.assignee_user_id = $1
                OR t.created_by_user_id = $1
                OR EXISTS (
                    SELECT 1 FROM task_participants tp
                    WHERE tp.task_id = t.id AND tp.user_id = $1
                )
              )
            ORDER BY t.updated_at DESC
        """
        rows = await self.fetch(query, user_id)
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
        priority = row["priority"] if "priority" in keys else 1
        is_overdue = row["is_overdue"] if "is_overdue" in keys else False
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
            priority=priority,
            is_active=row["is_active"],
            is_overdue=is_overdue,
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
                "department_id": row["creator_department_id"],
            }
        return {"task": task, "creator": creator}
