"""TaskParticipant Storage — CRUD for task_participants table."""
import logging
from typing import Dict, List, Optional
from uuid import UUID

from .base import BaseStorage
from ..models.task_participant import TaskParticipant

logger = logging.getLogger("rugpt.storage.task_participant")


class TaskParticipantStorage(BaseStorage):

    async def add(
        self,
        task_id: UUID,
        user_id: UUID,
        added_by_user_id: Optional[UUID] = None,
    ) -> TaskParticipant:
        """Insert a participant. Raises asyncpg.UniqueViolationError on duplicate."""
        row = await self.fetchrow(
            """
            INSERT INTO task_participants (task_id, user_id, added_by_user_id, added_at)
            VALUES ($1, $2, $3, NOW())
            RETURNING task_id, user_id, added_by_user_id, added_at
            """,
            task_id, user_id, added_by_user_id,
        )
        return TaskParticipant(
            task_id=row["task_id"],
            user_id=row["user_id"],
            added_by_user_id=row["added_by_user_id"],
            added_at=row["added_at"],
        )

    async def remove(self, task_id: UUID, user_id: UUID) -> bool:
        """Delete participant. Returns True if a row was removed, False if not present."""
        result = await self.execute(
            "DELETE FROM task_participants WHERE task_id = $1 AND user_id = $2",
            task_id, user_id,
        )
        return "DELETE 1" in result

    async def list_user_ids(self, task_id: UUID) -> List[UUID]:
        """Return all participant user_ids for a task (regardless of users.is_active).

        Used for chat sync where we need to know if user belonged to task at all.
        """
        rows = await self.fetch(
            "SELECT user_id FROM task_participants WHERE task_id = $1",
            task_id,
        )
        return [r["user_id"] for r in rows]

    async def list_active_user_dicts(self, task_id: UUID) -> List[dict]:
        """
        Return active participants of a task as User-shaped dicts {id, name}.
        Filters out inactive users. Used by API list endpoints.
        """
        rows = await self.fetch(
            """
            SELECT u.id, u.name
            FROM task_participants tp
            JOIN users u ON u.id = tp.user_id
            WHERE tp.task_id = $1 AND u.is_active = true
            ORDER BY u.name
            """,
            task_id,
        )
        return [{"id": r["id"], "name": r["name"]} for r in rows]

    async def get_for_tasks(
        self, task_ids: List[UUID],
    ) -> Dict[UUID, List[dict]]:
        """
        Bulk fetch active participants for multiple tasks at once.
        Returns {task_id -> [{id, name}, ...]}. Tasks with no participants
        are absent from the dict.
        """
        if not task_ids:
            return {}
        rows = await self.fetch(
            """
            SELECT tp.task_id, u.id, u.name
            FROM task_participants tp
            JOIN users u ON u.id = tp.user_id
            WHERE tp.task_id = ANY($1::uuid[]) AND u.is_active = true
            ORDER BY u.name
            """,
            list(task_ids),
        )
        result: Dict[UUID, List[dict]] = {}
        for r in rows:
            result.setdefault(r["task_id"], []).append(
                {"id": r["id"], "name": r["name"]}
            )
        return result

    async def list_tasks_for_user(
        self, user_id: UUID, include_done: bool = False,
    ) -> List[UUID]:
        """Return task_ids where user is participant. Used by `/tasks/participating`."""
        done_filter = "" if include_done else "AND t.status != 'done'"
        rows = await self.fetch(
            f"""
            SELECT tp.task_id
            FROM task_participants tp
            JOIN tasks t ON t.id = tp.task_id
            WHERE tp.user_id = $1 AND t.is_active = true {done_filter}
            ORDER BY t.created_at DESC
            """,
            user_id,
        )
        return [r["task_id"] for r in rows]
