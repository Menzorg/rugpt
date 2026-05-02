# Task Participants Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Добавить multi-participant к задачам (рядом с одним assignee), с авто-синхронизацией task chat и project chat, отдельной вкладкой `Участвую`, единой вкладкой `Выполненные` и полным receiver list для уведомлений.

**Architecture:** Отдельная M:N таблица `task_participants` на engine. Auto-swap старого/нового assignee между ролями assignee и participant при reassign. Receiver list уведомлений вынесен в общий helper `_resolve_recipients` в `task_notification_service`. UI обновляет вкладки `/tasks` и блок участников в модалках create/edit.

**Tech Stack:** PostgreSQL (asyncpg), FastAPI engine, NestJS proxy, Next.js + React + Zustand frontend.

**Reference spec:** `/root/rugpt/docs/superpowers/specs/2026-05-02-task-participants-design.md`

---

## File map

### Engine (new files)

- `src/engine/migrations/032_task_participants.sql` — миграция M:N таблицы.
- `src/engine/models/task_participant.py` — `TaskParticipant` dataclass.
- `src/engine/storage/task_participant_storage.py` — CRUD таблицы.
- `tests/test_task_participant_storage.py` — unit storage.
- `tests/test_task_service_participants.py` — unit service.
- `tests/test_resolve_recipients.py` — unit helper.
- `tests/integration/test_task_participants_integration.py` — chat sync, project chat sync.

### Engine (modified)

- `src/engine/services/engine_service.py` — wire `TaskParticipantStorage`.
- `src/engine/services/task_service.py` — add/remove participants, create extension, assignee swap, list_participating, list_done, расширенные хуки.
- `src/engine/services/task_notification_service.py` — `_resolve_recipients`, обновлённые тексты, новые `notify_added_as_participant` / `notify_removed_as_participant`.
- `src/engine/services/chat_service.py` — `remove_task_chat_participant`, `remove_user_from_project_chat_if_unused`.
- `src/engine/storage/task_storage.py` — `user_has_any_active_task_in_project`, расширение всех `_with_*` методов чтобы дотягивать participants через bulk fetch.
- `src/engine/storage/chat_storage.py` — без изменений (generic add/remove participant уже есть).
- `src/engine/routes/tasks.py` — новые endpoints, `_serialize_entry` дотягивает participants.

### WebClient backend

- `packages/backend/src/engine/adapters/rugpt.adapter.ts` — 4 новых command'а.
- `packages/backend/src/task/task.service.ts` — proxy methods.
- `packages/backend/src/task/task.controller.ts` — 4 новых route.
- `packages/common/src/types/task.ts` — `TaskParticipant`, расширение `TaskRow`.

### Frontend

- `packages/frontend/src/app/tasks/page.tsx` — 5 вкладок, удаление чекбокса include_done, рендер participants chips, multiselect в create modal, блок управления в edit modal, бейджи-счётчики.

---

## Task 1: Migration 032 — `task_participants` table

**Files:**
- Create: `/root/rugpt/src/engine/migrations/032_task_participants.sql`
- Test (manual via apply): the migration script.

- [ ] **Step 1: Создать файл миграции**

```sql
-- Migration 032: Task participants (M:N)
-- Дополнительные участники задачи (chat collaborators).
-- Отдельная сущность от assignee (один) и creator (один).

CREATE TABLE IF NOT EXISTS task_participants (
    task_id UUID NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    user_id UUID NOT NULL REFERENCES users(id),
    added_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),
    added_by_user_id UUID REFERENCES users(id),
    PRIMARY KEY (task_id, user_id)
);

CREATE INDEX IF NOT EXISTS idx_task_participants_user
    ON task_participants(user_id);

COMMENT ON TABLE task_participants IS
    'Additional task participants (chat collaborators). Separate from assignee (one) and creator. Idempotent inserts via PK; ON DELETE CASCADE on task removal.';
```

- [ ] **Step 2: Применить миграцию на dev DB**

Run: `cd /root/rugpt && ./migrate.sh`
Expected: `Migration 032 applied`. Если миграция уже была — `already up to date`.

- [ ] **Step 3: Smoke check schema**

Run: `psql -U postgres -h localhost -d rugpt -c "\\d task_participants"`
Expected: видим колонки `task_id`, `user_id`, `added_at`, `added_by_user_id`, PK `(task_id, user_id)` и индекс `idx_task_participants_user`.


---

## Task 2: Engine model `TaskParticipant`

**Files:**
- Create: `/root/rugpt/src/engine/models/task_participant.py`
- Test: `/root/rugpt/tests/test_task_participant_model.py`

- [ ] **Step 1: Написать падающий тест модели**

`/root/rugpt/tests/test_task_participant_model.py`:

```python
from datetime import datetime
from uuid import uuid4

from src.engine.models.task_participant import TaskParticipant


def test_to_dict_returns_strings_for_uuids_and_iso_for_datetime():
    task_id = uuid4()
    user_id = uuid4()
    added_by = uuid4()
    now = datetime(2026, 5, 2, 12, 0, 0)
    p = TaskParticipant(
        task_id=task_id, user_id=user_id,
        added_at=now, added_by_user_id=added_by,
    )
    assert p.to_dict() == {
        "task_id": str(task_id),
        "user_id": str(user_id),
        "added_at": "2026-05-02T12:00:00",
        "added_by_user_id": str(added_by),
    }


def test_to_dict_handles_null_added_by():
    p = TaskParticipant(
        task_id=uuid4(), user_id=uuid4(),
        added_at=datetime(2026, 5, 2), added_by_user_id=None,
    )
    assert p.to_dict()["added_by_user_id"] is None
```

- [ ] **Step 2: Запустить тест — должен FAIL**

Run: `cd /root/rugpt && venv/bin/pytest tests/test_task_participant_model.py -v`
Expected: `ImportError` или `ModuleNotFoundError`.

- [ ] **Step 3: Реализовать модель**

`/root/rugpt/src/engine/models/task_participant.py`:

```python
"""TaskParticipant model — additional task collaborator (chat-only role)."""
from dataclasses import dataclass
from datetime import datetime
from typing import Optional
from uuid import UUID


@dataclass
class TaskParticipant:
    task_id: UUID
    user_id: UUID
    added_at: datetime
    added_by_user_id: Optional[UUID] = None

    def to_dict(self) -> dict:
        return {
            "task_id": str(self.task_id),
            "user_id": str(self.user_id),
            "added_at": self.added_at.isoformat(),
            "added_by_user_id": str(self.added_by_user_id) if self.added_by_user_id else None,
        }
```

- [ ] **Step 4: Запустить тест — должен PASS**

Run: `cd /root/rugpt && venv/bin/pytest tests/test_task_participant_model.py -v`
Expected: 2 passed.


---

## Task 3: Storage `TaskParticipantStorage`

**Files:**
- Create: `/root/rugpt/src/engine/storage/task_participant_storage.py`
- Test: `/root/rugpt/tests/test_task_participant_storage.py`

- [ ] **Step 1: Написать падающие тесты storage**

`/root/rugpt/tests/test_task_participant_storage.py`:

```python
"""Storage tests for task_participants. Uses real Postgres via env var."""
import os
import pytest
import pytest_asyncio
from uuid import UUID, uuid4
from datetime import datetime

import asyncpg

from src.engine.storage.task_participant_storage import TaskParticipantStorage
from src.engine.models.task_participant import TaskParticipant


DSN = os.environ.get("DATABASE_URL", "postgresql://postgres@localhost/rugpt")


@pytest_asyncio.fixture
async def fixtures():
    """Create org/users/task for tests; return ids and clean up on teardown."""
    pool = await asyncpg.create_pool(DSN)
    async with pool.acquire() as conn:
        org_id = await conn.fetchval(
            "INSERT INTO organizations (id, name) VALUES (gen_random_uuid(), 'p_org') RETURNING id"
        )
        creator_id = await conn.fetchval(
            "INSERT INTO users (id, org_id, username, name, password_hash) "
            "VALUES (gen_random_uuid(), $1, 'p_creator', 'Creator', 'x') RETURNING id",
            org_id,
        )
        u1 = await conn.fetchval(
            "INSERT INTO users (id, org_id, username, name, password_hash) "
            "VALUES (gen_random_uuid(), $1, 'p_u1', 'U1', 'x') RETURNING id", org_id,
        )
        u2 = await conn.fetchval(
            "INSERT INTO users (id, org_id, username, name, password_hash) "
            "VALUES (gen_random_uuid(), $1, 'p_u2', 'U2', 'x') RETURNING id", org_id,
        )
        u3_inactive = await conn.fetchval(
            "INSERT INTO users (id, org_id, username, name, password_hash, is_active) "
            "VALUES (gen_random_uuid(), $1, 'p_u3', 'U3', 'x', false) RETURNING id", org_id,
        )
        task_id = await conn.fetchval(
            "INSERT INTO tasks (id, org_id, title, assignee_user_id, created_by_user_id) "
            "VALUES (gen_random_uuid(), $1, 'T', $2, $3) RETURNING id",
            org_id, u1, creator_id,
        )
    yield {
        "org_id": org_id, "task_id": task_id,
        "creator_id": creator_id, "u1": u1, "u2": u2, "u3_inactive": u3_inactive,
    }
    async with pool.acquire() as conn:
        await conn.execute("DELETE FROM tasks WHERE org_id = $1", org_id)
        await conn.execute("DELETE FROM users WHERE org_id = $1", org_id)
        await conn.execute("DELETE FROM organizations WHERE id = $1", org_id)
    await pool.close()


@pytest.mark.asyncio
async def test_add_and_list(fixtures):
    storage = TaskParticipantStorage(DSN)
    await storage.init()
    try:
        added = await storage.add(
            task_id=fixtures["task_id"], user_id=fixtures["u2"],
            added_by_user_id=fixtures["creator_id"],
        )
        assert added.user_id == fixtures["u2"]

        rows = await storage.list_active_user_dicts(fixtures["task_id"])
        assert len(rows) == 1
        assert rows[0]["id"] == fixtures["u2"]
        assert rows[0]["name"] == "U2"
    finally:
        await storage.close()


@pytest.mark.asyncio
async def test_duplicate_raises_unique_violation(fixtures):
    storage = TaskParticipantStorage(DSN)
    await storage.init()
    try:
        await storage.add(fixtures["task_id"], fixtures["u2"], fixtures["creator_id"])
        with pytest.raises(asyncpg.UniqueViolationError):
            await storage.add(fixtures["task_id"], fixtures["u2"], fixtures["creator_id"])
    finally:
        await storage.close()


@pytest.mark.asyncio
async def test_remove_returns_true_on_match_false_on_missing(fixtures):
    storage = TaskParticipantStorage(DSN)
    await storage.init()
    try:
        await storage.add(fixtures["task_id"], fixtures["u2"], fixtures["creator_id"])
        assert await storage.remove(fixtures["task_id"], fixtures["u2"]) is True
        assert await storage.remove(fixtures["task_id"], fixtures["u2"]) is False
    finally:
        await storage.close()


@pytest.mark.asyncio
async def test_list_active_user_dicts_filters_inactive_users(fixtures):
    storage = TaskParticipantStorage(DSN)
    await storage.init()
    try:
        await storage.add(fixtures["task_id"], fixtures["u2"], fixtures["creator_id"])
        await storage.add(fixtures["task_id"], fixtures["u3_inactive"], fixtures["creator_id"])
        rows = await storage.list_active_user_dicts(fixtures["task_id"])
        ids = [r["id"] for r in rows]
        assert fixtures["u2"] in ids
        assert fixtures["u3_inactive"] not in ids
    finally:
        await storage.close()


@pytest.mark.asyncio
async def test_list_user_ids_returns_all_including_inactive(fixtures):
    """list_user_ids is for project-chat sync — needs ALL participants regardless of is_active."""
    storage = TaskParticipantStorage(DSN)
    await storage.init()
    try:
        await storage.add(fixtures["task_id"], fixtures["u2"], fixtures["creator_id"])
        await storage.add(fixtures["task_id"], fixtures["u3_inactive"], fixtures["creator_id"])
        ids = await storage.list_user_ids(fixtures["task_id"])
        assert set(ids) == {fixtures["u2"], fixtures["u3_inactive"]}
    finally:
        await storage.close()


@pytest.mark.asyncio
async def test_get_for_tasks_bulk(fixtures):
    """Bulk fetch participants for multiple tasks at once (used by list endpoints)."""
    storage = TaskParticipantStorage(DSN)
    await storage.init()
    try:
        await storage.add(fixtures["task_id"], fixtures["u2"], fixtures["creator_id"])
        result = await storage.get_for_tasks([fixtures["task_id"]])
        assert fixtures["task_id"] in result
        assert result[fixtures["task_id"]][0]["id"] == fixtures["u2"]
        assert result[fixtures["task_id"]][0]["name"] == "U2"
    finally:
        await storage.close()
```

- [ ] **Step 2: Запустить — должен FAIL (ModuleNotFoundError)**

Run: `cd /root/rugpt && venv/bin/pytest tests/test_task_participant_storage.py -v`
Expected: ImportError on storage module.

- [ ] **Step 3: Реализовать TaskParticipantStorage**

`/root/rugpt/src/engine/storage/task_participant_storage.py`:

```python
"""TaskParticipant Storage — CRUD for task_participants table."""
import logging
from datetime import datetime
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
            """,
            user_id,
        )
        return [r["task_id"] for r in rows]
```

- [ ] **Step 4: Запустить тесты — должны PASS**

Run: `cd /root/rugpt && venv/bin/pytest tests/test_task_participant_storage.py -v`
Expected: 6 passed.


---

## Task 4: TaskStorage extension — `user_has_any_active_task_in_project` + list-with-participants

**Files:**
- Modify: `/root/rugpt/src/engine/storage/task_storage.py`
- Test: `/root/rugpt/tests/test_task_storage_participants.py`

This task adds storage methods needed by task_service hooks and list endpoints:
- `user_has_any_active_task_in_project(project_id, user_id)` — для project chat sync
- `list_by_participant_with_priority(user_id, include_done)` — для `/tasks/participating`
- `list_done_for_user(user_id)` — для `/tasks/done`

- [ ] **Step 1: Написать падающие тесты**

`/root/rugpt/tests/test_task_storage_participants.py`:

```python
import os, pytest, pytest_asyncio, asyncpg
from uuid import uuid4
from src.engine.storage.task_storage import TaskStorage
from src.engine.storage.task_participant_storage import TaskParticipantStorage

DSN = os.environ.get("DATABASE_URL", "postgresql://postgres@localhost/rugpt")


@pytest_asyncio.fixture
async def fixtures():
    pool = await asyncpg.create_pool(DSN)
    async with pool.acquire() as conn:
        org = await conn.fetchval(
            "INSERT INTO organizations (id, name) VALUES (gen_random_uuid(), 'tsp') RETURNING id"
        )
        creator = await conn.fetchval(
            "INSERT INTO users (id, org_id, username, name, password_hash) "
            "VALUES (gen_random_uuid(), $1, 'tsp_c', 'C', 'x') RETURNING id", org,
        )
        u_assignee = await conn.fetchval(
            "INSERT INTO users (id, org_id, username, name, password_hash) "
            "VALUES (gen_random_uuid(), $1, 'tsp_a', 'A', 'x') RETURNING id", org,
        )
        u_part = await conn.fetchval(
            "INSERT INTO users (id, org_id, username, name, password_hash) "
            "VALUES (gen_random_uuid(), $1, 'tsp_p', 'P', 'x') RETURNING id", org,
        )
        u_other = await conn.fetchval(
            "INSERT INTO users (id, org_id, username, name, password_hash) "
            "VALUES (gen_random_uuid(), $1, 'tsp_o', 'O', 'x') RETURNING id", org,
        )
        project = await conn.fetchval(
            "INSERT INTO projects (id, org_id, name, created_by_user_id) "
            "VALUES (gen_random_uuid(), $1, 'P', $2) RETURNING id",
            org, creator,
        )
        t1 = await conn.fetchval(
            "INSERT INTO tasks (id, org_id, title, assignee_user_id, created_by_user_id, project_id) "
            "VALUES (gen_random_uuid(), $1, 'T1', $2, $3, $4) RETURNING id",
            org, u_assignee, creator, project,
        )
        t2 = await conn.fetchval(
            "INSERT INTO tasks (id, org_id, title, assignee_user_id, created_by_user_id, project_id, status) "
            "VALUES (gen_random_uuid(), $1, 'T2', $2, $3, $4, 'done') RETURNING id",
            org, u_assignee, creator, project,
        )
    yield {
        "org": org, "creator": creator, "u_assignee": u_assignee,
        "u_part": u_part, "u_other": u_other, "project": project,
        "t1": t1, "t2": t2,
    }
    async with pool.acquire() as conn:
        await conn.execute("DELETE FROM tasks WHERE org_id = $1", org)
        await conn.execute("DELETE FROM projects WHERE org_id = $1", org)
        await conn.execute("DELETE FROM users WHERE org_id = $1", org)
        await conn.execute("DELETE FROM organizations WHERE id = $1", org)
    await pool.close()


@pytest.mark.asyncio
async def test_user_has_any_active_task_in_project_creator(fixtures):
    storage = TaskStorage(DSN); await storage.init()
    try:
        assert await storage.user_has_any_active_task_in_project(fixtures["project"], fixtures["creator"]) is True
    finally:
        await storage.close()


@pytest.mark.asyncio
async def test_user_has_any_active_task_in_project_assignee(fixtures):
    storage = TaskStorage(DSN); await storage.init()
    try:
        assert await storage.user_has_any_active_task_in_project(fixtures["project"], fixtures["u_assignee"]) is True
    finally:
        await storage.close()


@pytest.mark.asyncio
async def test_user_has_any_active_task_in_project_via_participant(fixtures):
    """User who is participant in any active task of the project should count."""
    ps = TaskParticipantStorage(DSN); await ps.init()
    storage = TaskStorage(DSN); await storage.init()
    try:
        await ps.add(fixtures["t1"], fixtures["u_part"], fixtures["creator"])
        assert await storage.user_has_any_active_task_in_project(fixtures["project"], fixtures["u_part"]) is True
    finally:
        await ps.close(); await storage.close()


@pytest.mark.asyncio
async def test_user_has_any_active_task_in_project_negative(fixtures):
    storage = TaskStorage(DSN); await storage.init()
    try:
        assert await storage.user_has_any_active_task_in_project(fixtures["project"], fixtures["u_other"]) is False
    finally:
        await storage.close()


@pytest.mark.asyncio
async def test_list_by_participant_with_priority_excludes_done(fixtures):
    """Default include_done=False — only active, non-done tasks."""
    ps = TaskParticipantStorage(DSN); await ps.init()
    storage = TaskStorage(DSN); await storage.init()
    try:
        await ps.add(fixtures["t1"], fixtures["u_part"], fixtures["creator"])  # status='created'
        await ps.add(fixtures["t2"], fixtures["u_part"], fixtures["creator"])  # status='done'
        rows = await storage.list_by_participant_with_priority(fixtures["u_part"])
        ids = [e["task"].id for e in rows]
        assert fixtures["t1"] in ids
        assert fixtures["t2"] not in ids
    finally:
        await ps.close(); await storage.close()


@pytest.mark.asyncio
async def test_list_done_for_user_unions_all_roles(fixtures):
    """Done list includes done tasks where user is creator OR assignee OR participant."""
    ps = TaskParticipantStorage(DSN); await ps.init()
    storage = TaskStorage(DSN); await storage.init()
    try:
        await ps.add(fixtures["t1"], fixtures["u_part"], fixtures["creator"])

        # creator
        rows = await storage.list_done_for_user(fixtures["creator"])
        assert any(e["task"].id == fixtures["t2"] for e in rows)

        # assignee
        rows = await storage.list_done_for_user(fixtures["u_assignee"])
        assert any(e["task"].id == fixtures["t2"] for e in rows)

        # other (no relation) - empty
        rows = await storage.list_done_for_user(fixtures["u_other"])
        assert rows == []
    finally:
        await ps.close(); await storage.close()
```

- [ ] **Step 2: Запустить — FAIL**

Run: `cd /root/rugpt && venv/bin/pytest tests/test_task_storage_participants.py -v`
Expected: AttributeError on missing methods.

- [ ] **Step 3: Добавить методы в `task_storage.py`**

Append after the existing `count_active_in_project` method (around line 367):

```python
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
```

- [ ] **Step 4: Запустить — PASS**

Run: `cd /root/rugpt && venv/bin/pytest tests/test_task_storage_participants.py -v`
Expected: 6 passed.

- [ ] **Step 5: Прогнать полный engine test suite — никаких regression**

Run: `cd /root/rugpt && venv/bin/pytest tests/ -x --ignore=tests/integration -q`
Expected: existing tests still green (≥300 passed). New tests included.


---

## Task 5: Wire `TaskParticipantStorage` in `engine_service`

**Files:**
- Modify: `/root/rugpt/src/engine/services/engine_service.py`

- [ ] **Step 1: Добавить импорт и инициализацию**

В импортах (рядом с `TaskStorage`):

```python
from ..storage.task_participant_storage import TaskParticipantStorage
```

В `__init__` после `self.task_storage = TaskStorage(...)` (строка ~99):

```python
        self.task_participant_storage = TaskParticipantStorage(self.postgres_dsn)
```

В `task_service` конструкторе (строка ~170) добавить параметр (см. Task 6 — там подключим). Сейчас просто запоминаем.

В `init` (строка ~414) и `close` (строка ~474) добавить:

```python
        await self.task_participant_storage.init()
```
```python
        await self.task_participant_storage.close()
```

- [ ] **Step 2: Smoke run app to ensure no startup error**

Run: `cd /root/rugpt && venv/bin/python -c "from src.engine.services.engine_service import EngineService; print('ok')"`
Expected: `ok` (no import errors).


---

## Task 6: TaskService — add/remove/list participants + create extension

**Files:**
- Modify: `/root/rugpt/src/engine/services/task_service.py`
- Modify: `/root/rugpt/src/engine/services/engine_service.py` (добавляем param)
- Test: `/root/rugpt/tests/test_task_service_participants.py`

- [ ] **Step 1: Тесты на add/remove/list/create participant logic**

`/root/rugpt/tests/test_task_service_participants.py`:

```python
import os, pytest, pytest_asyncio, asyncpg
from uuid import UUID, uuid4

from src.engine.services.engine_service import EngineService
from src.engine.models.user import User

DSN = os.environ.get("DATABASE_URL", "postgresql://postgres@localhost/rugpt")


@pytest_asyncio.fixture
async def env():
    os.environ["DATABASE_URL"] = DSN
    e = EngineService()
    await e.init()
    pool = await asyncpg.create_pool(DSN)
    async with pool.acquire() as conn:
        org = await conn.fetchval(
            "INSERT INTO organizations (id, name) VALUES (gen_random_uuid(), 'tsv') RETURNING id"
        )
        creator = await conn.fetchval(
            "INSERT INTO users (id, org_id, username, name, password_hash) "
            "VALUES (gen_random_uuid(), $1, 'tsv_c', 'C', 'x') RETURNING id", org,
        )
        assignee = await conn.fetchval(
            "INSERT INTO users (id, org_id, username, name, password_hash) "
            "VALUES (gen_random_uuid(), $1, 'tsv_a', 'A', 'x') RETURNING id", org,
        )
        part = await conn.fetchval(
            "INSERT INTO users (id, org_id, username, name, password_hash) "
            "VALUES (gen_random_uuid(), $1, 'tsv_p', 'P', 'x') RETURNING id", org,
        )
        outsider = await conn.fetchval(
            "INSERT INTO users (id, org_id, username, name, password_hash) "
            "VALUES (gen_random_uuid(), $1, 'tsv_o', 'O', 'x') RETURNING id", org,
        )
    yield {"engine": e, "org": org, "creator": creator,
           "assignee": assignee, "part": part, "outsider": outsider}
    async with pool.acquire() as conn:
        await conn.execute("DELETE FROM tasks WHERE org_id = $1", org)
        await conn.execute("DELETE FROM users WHERE org_id = $1", org)
        await conn.execute("DELETE FROM organizations WHERE id = $1", org)
    await pool.close()
    await e.close()


def _user(uid, org, *, is_admin=False, is_head=False):
    return User(id=uid, org_id=org, username="x", name="x",
                password_hash="x", is_admin=is_admin, is_head=is_head)


@pytest.mark.asyncio
async def test_add_participant_happy_path(env):
    svc = env["engine"].task_service
    task = await svc.create(
        org_id=env["org"], title="T",
        assignee_user_id=env["assignee"], created_by_user_id=env["creator"],
    )
    creator_user = _user(env["creator"], env["org"])
    added = await svc.add_participant(task.id, env["part"], creator_user)
    assert added["id"] == env["part"]


@pytest.mark.asyncio
async def test_add_participant_rejects_assignee_or_creator(env):
    svc = env["engine"].task_service
    task = await svc.create(
        org_id=env["org"], title="T",
        assignee_user_id=env["assignee"], created_by_user_id=env["creator"],
    )
    creator_user = _user(env["creator"], env["org"])
    with pytest.raises(ValueError, match="already assignee"):
        await svc.add_participant(task.id, env["assignee"], creator_user)
    with pytest.raises(ValueError, match="already creator"):
        await svc.add_participant(task.id, env["creator"], creator_user)


@pytest.mark.asyncio
async def test_add_participant_only_creator_or_admin(env):
    svc = env["engine"].task_service
    task = await svc.create(
        org_id=env["org"], title="T",
        assignee_user_id=env["assignee"], created_by_user_id=env["creator"],
    )
    outsider = _user(env["outsider"], env["org"])
    with pytest.raises(PermissionError):
        await svc.add_participant(task.id, env["part"], outsider)
    admin = _user(env["outsider"], env["org"], is_admin=True)
    out = await svc.add_participant(task.id, env["part"], admin)
    assert out["id"] == env["part"]


@pytest.mark.asyncio
async def test_remove_participant_happy_path_and_missing(env):
    svc = env["engine"].task_service
    task = await svc.create(
        org_id=env["org"], title="T",
        assignee_user_id=env["assignee"], created_by_user_id=env["creator"],
    )
    creator_user = _user(env["creator"], env["org"])
    await svc.add_participant(task.id, env["part"], creator_user)
    assert await svc.remove_participant(task.id, env["part"], creator_user) is True
    assert await svc.remove_participant(task.id, env["part"], creator_user) is False


@pytest.mark.asyncio
async def test_create_with_participant_user_ids(env):
    svc = env["engine"].task_service
    task = await svc.create(
        org_id=env["org"], title="T",
        assignee_user_id=env["assignee"],
        created_by_user_id=env["creator"],
        participant_user_ids=[env["part"]],
    )
    rows = await env["engine"].task_participant_storage.list_active_user_dicts(task.id)
    assert any(r["id"] == env["part"] for r in rows)


@pytest.mark.asyncio
async def test_create_filters_assignee_and_creator_from_participants(env):
    """Even if caller mistakenly passes assignee/creator in participant_user_ids, they must be silently filtered."""
    svc = env["engine"].task_service
    task = await svc.create(
        org_id=env["org"], title="T",
        assignee_user_id=env["assignee"],
        created_by_user_id=env["creator"],
        participant_user_ids=[env["assignee"], env["creator"], env["part"]],
    )
    rows = await env["engine"].task_participant_storage.list_active_user_dicts(task.id)
    ids = {r["id"] for r in rows}
    assert env["part"] in ids
    assert env["assignee"] not in ids
    assert env["creator"] not in ids


@pytest.mark.asyncio
async def test_assignee_swap_old_becomes_participant_new_leaves(env):
    """Reassign auto-swap: old assignee -> participant, new assignee -> removed from participants."""
    svc = env["engine"].task_service
    task = await svc.create(
        org_id=env["org"], title="T",
        assignee_user_id=env["assignee"], created_by_user_id=env["creator"],
        participant_user_ids=[env["part"]],
    )
    # Reassign to part (who was a participant)
    await svc.update(
        task_id=task.id,
        assignee_user_id=env["part"],
        actor_user_id=env["creator"],
    )
    rows = await env["engine"].task_participant_storage.list_user_ids(task.id)
    # part should no longer be participant (now assignee)
    assert env["part"] not in rows
    # old assignee should be participant
    assert env["assignee"] in rows


@pytest.mark.asyncio
async def test_assignee_swap_skips_creator(env):
    """If old assignee == creator, do NOT add as participant."""
    svc = env["engine"].task_service
    # creator is also assignee (degenerate but possible)
    task = await svc.create(
        org_id=env["org"], title="T",
        assignee_user_id=env["creator"], created_by_user_id=env["creator"],
    )
    await svc.update(
        task_id=task.id,
        assignee_user_id=env["assignee"],
        actor_user_id=env["creator"],
    )
    rows = await env["engine"].task_participant_storage.list_user_ids(task.id)
    # creator stays as creator only, not duplicated as participant
    assert env["creator"] not in rows


@pytest.mark.asyncio
async def test_list_participating(env):
    svc = env["engine"].task_service
    task = await svc.create(
        org_id=env["org"], title="T",
        assignee_user_id=env["assignee"], created_by_user_id=env["creator"],
        participant_user_ids=[env["part"]],
    )
    entries = await svc.list_participating(env["part"])
    ids = [e["task"].id for e in entries]
    assert task.id in ids


@pytest.mark.asyncio
async def test_list_done_returns_done_for_all_roles(env):
    svc = env["engine"].task_service
    task = await svc.create(
        org_id=env["org"], title="T",
        assignee_user_id=env["assignee"], created_by_user_id=env["creator"],
        participant_user_ids=[env["part"]],
    )
    # Move task to done via take + mark_done + accept
    a = _user(env["assignee"], env["org"])
    c = _user(env["creator"], env["org"])
    await svc.take_task(task.id, a)
    await svc.mark_done(task.id, a)
    await svc.accept_task(task.id, c)
    for u in (env["creator"], env["assignee"], env["part"]):
        entries = await svc.list_done(u)
        assert any(e["task"].id == task.id for e in entries), f"missing for {u}"
```

- [ ] **Step 2: Запустить — FAIL (методов ещё нет)**

Run: `cd /root/rugpt && venv/bin/pytest tests/test_task_service_participants.py -v`
Expected: AttributeError на `add_participant` / `list_participating` / `list_done`.

- [ ] **Step 3: Расширить TaskService**

В `task_service.py`:

(a) Импорт TaskParticipantStorage и user_storage уже есть; добавить парам в конструктор:

```python
    def __init__(
        self,
        storage: TaskStorage,
        in_app_notification_service: InAppNotificationService,
        chat_service: Optional["ChatService"] = None,
        task_event_service: Optional["TaskEventService"] = None,
        project_service: Optional["ProjectService"] = None,
        task_notification_service: Optional["TaskNotificationService"] = None,
        user_storage: Optional[UserStorage] = None,
        task_participant_storage: Optional["TaskParticipantStorage"] = None,
    ):
        ...
        self.task_participant_storage = task_participant_storage
```

И импорт `TaskParticipantStorage` в TYPE_CHECKING:

```python
if TYPE_CHECKING:
    from ..storage.task_participant_storage import TaskParticipantStorage
    ...
```

(b) Permission helper — расширение существующего `_check_creator`:

Сейчас: только creator или admin. Для add/remove participants нужно: creator OR head OR admin.

```python
    def _check_creator_or_head(self, task: Task, user: User):
        """Allows: creator (if task has one) OR head OR admin."""
        if user.is_admin or getattr(user, "is_head", False):
            return
        if task.created_by_user_id is not None and task.created_by_user_id == user.id:
            return
        raise PermissionError("Only creator, head, or admin can perform this action")
```

(c) Метод `create` — добавить параметр и логику:

```python
    async def create(
        self,
        org_id: UUID,
        title: str,
        assignee_user_id: UUID,
        description: Optional[str] = None,
        deadline: Optional[datetime] = None,
        created_by_user_id: Optional[UUID] = None,
        project_id: Optional[UUID] = None,
        priority: Optional[int] = None,
        participant_user_ids: Optional[List[UUID]] = None,
    ) -> Task:
        ...  # существующая логика до строки `created = await self.storage.create(task)`

        # === NEW: persist participants (filter assignee/creator) ===
        filtered_participants: List[UUID] = []
        if participant_user_ids and self.task_participant_storage is not None:
            seen = {created.assignee_user_id, created.created_by_user_id}
            for pid in participant_user_ids:
                if pid is None or pid in seen:
                    continue
                seen.add(pid)
                try:
                    await self.task_participant_storage.add(
                        task_id=created.id,
                        user_id=pid,
                        added_by_user_id=created_by_user_id,
                    )
                    filtered_participants.append(pid)
                except Exception as e:
                    logger.warning(f"Failed to add participant {pid} to task {created.id}: {e}")

        # === END NEW ===

        # 1. Auto-create task chat — extend with participants.
        if self.chat_service is not None:
            try:
                await self.chat_service.create_task_chat(
                    task_id=created.id,
                    org_id=org_id,
                    creator_id=created_by_user_id or assignee_user_id,
                    assignee_id=assignee_user_id,
                )
                # Add participants to the chat
                for pid in filtered_participants:
                    await self.chat_service.add_task_chat_participant(created.id, pid)
            except Exception as e:
                logger.error(f"Failed to create task chat for {created.id}: {e}")

        # 2. Project chat — include participants.
        if project_id and self.chat_service is not None:
            participants = [u for u in [created_by_user_id, assignee_user_id, *filtered_participants] if u]
            try:
                await self.chat_service.ensure_project_chat_membership(
                    project_id=project_id, org_id=org_id, user_ids=participants,
                )
            except Exception as e:
                logger.error(f"Failed to ensure project chat for {project_id}: {e}")

        # 3. Audit event — unchanged (single 'created').
        await self._record_event(
            task_id=created.id,
            actor_user_id=created_by_user_id,
            event_type="created",
            payload={
                "title": title,
                "assignee_user_id": str(assignee_user_id),
                "project_id": str(project_id) if project_id else None,
                "participant_user_ids": [str(p) for p in filtered_participants],
            },
        )

        # 4. Notify assignee (existing).
        await self.notification_service.create(
            user_id=assignee_user_id, org_id=org_id, type="new_task",
            title=f"Новая задача: {title}", content=description,
            reference_type="task", reference_id=created.id,
        )

        # 5. Notify each new participant (one-time `task_added_as_participant`).
        for pid in filtered_participants:
            await self._notify(
                "notify_added_as_participant", created, pid, by_user_id=created_by_user_id,
            )

        return created
```

(d) Новые методы add/remove/list:

```python
    # ============================================
    # Participants
    # ============================================

    async def add_participant(
        self, task_id: UUID, user_id: UUID, actor: User,
    ) -> dict:
        """Add a participant. Returns {id, name}. Raises:
        - PermissionError if actor is not creator/head/admin
        - ValueError if user is already creator or assignee
        - ValueError if duplicate (409 mapping at route layer)
        """
        if self.task_participant_storage is None:
            raise AssertionError("task_participant_storage required")
        task = await self.storage.get_by_id(task_id)
        if task is None:
            raise ValueError(f"Task {task_id} not found")
        self._check_creator_or_head(task, actor)
        if user_id == task.assignee_user_id:
            raise ValueError("User is already assignee of this task")
        if task.created_by_user_id is not None and user_id == task.created_by_user_id:
            raise ValueError("User is already creator of this task")

        import asyncpg
        try:
            await self.task_participant_storage.add(
                task_id=task_id, user_id=user_id, added_by_user_id=actor.id,
            )
        except asyncpg.UniqueViolationError:
            raise ValueError("User is already a participant")

        # Hook: task chat
        if self.chat_service is not None:
            try:
                await self.chat_service.add_task_chat_participant(task_id, user_id)
            except Exception as e:
                logger.error(f"add_task_chat_participant failed: {e}")

        # Hook: project chat
        if task.project_id and self.chat_service is not None:
            try:
                await self.chat_service.ensure_project_chat_membership(
                    project_id=task.project_id, org_id=task.org_id, user_ids=[user_id],
                )
            except Exception as e:
                logger.error(f"ensure_project_chat_membership failed: {e}")

        # Audit event
        await self._record_event(
            task_id=task_id, actor_user_id=actor.id,
            event_type="participant_added",
            payload={"user_id": str(user_id), "added_by": str(actor.id)},
        )

        # Notify
        await self._notify("notify_added_as_participant", task, user_id, by_user_id=actor.id)

        # Return shape for API
        rows = await self.task_participant_storage.list_active_user_dicts(task_id)
        for r in rows:
            if r["id"] == user_id:
                return r
        return {"id": user_id, "name": ""}

    async def remove_participant(
        self, task_id: UUID, user_id: UUID, actor: User,
    ) -> bool:
        """Remove participant. Returns False if not present (404 mapping at route layer)."""
        if self.task_participant_storage is None:
            raise AssertionError("task_participant_storage required")
        task = await self.storage.get_by_id(task_id)
        if task is None:
            raise ValueError(f"Task {task_id} not found")
        self._check_creator_or_head(task, actor)

        removed = await self.task_participant_storage.remove(task_id, user_id)
        if not removed:
            return False

        # Hook: task chat — remove from chat only if user is not creator/assignee.
        if self.chat_service is not None and user_id not in {
            task.created_by_user_id, task.assignee_user_id,
        }:
            try:
                chat = await self.chat_service.get_task_chat(task_id)
                if chat is not None:
                    await self.chat_service.chat_storage.remove_participant(chat.id, user_id)
            except Exception as e:
                logger.error(f"remove from task chat failed: {e}")

        # Hook: project chat — remove only if user has no other active task in project.
        if task.project_id and self.chat_service is not None:
            try:
                still_in = await self.storage.user_has_any_active_task_in_project(
                    task.project_id, user_id,
                )
                if not still_in:
                    chat = await self.chat_service.chat_storage.get_by_project_id(task.project_id)
                    if chat is not None:
                        await self.chat_service.chat_storage.remove_participant(chat.id, user_id)
            except Exception as e:
                logger.error(f"remove from project chat failed: {e}")

        # Audit
        await self._record_event(
            task_id=task_id, actor_user_id=actor.id,
            event_type="participant_removed",
            payload={"user_id": str(user_id), "removed_by": str(actor.id)},
        )

        # Notify
        await self._notify("notify_removed_as_participant", task, user_id, by_user_id=actor.id)

        return True

    async def list_participating(
        self, user_id: UUID, include_done: bool = False,
    ) -> List[dict]:
        """Tasks where user is in task_participants. Same shape as list_my_tasks."""
        return await self.storage.list_by_participant_with_priority(user_id, include_done)

    async def list_done(self, user_id: UUID) -> List[dict]:
        """Done tasks where user is creator OR assignee OR participant."""
        return await self.storage.list_done_for_user(user_id)
```

(e) Метод `update` — assignee swap:

В блоке `if assignee_user_id is not None and assignee_user_id != old_assignee:` после `add_task_chat_participant` вызова и **до** `_record_event` добавить:

```python
            # Auto-swap participants on reassign:
            #   - new assignee: remove from participants if they were one
            #   - old assignee: add to participants (unless they are creator)
            if self.task_participant_storage is not None:
                try:
                    await self.task_participant_storage.remove(updated.id, assignee_user_id)
                except Exception as e:
                    logger.warning(f"swap-remove participant failed: {e}")
                if old_assignee and old_assignee != updated.created_by_user_id and old_assignee != assignee_user_id:
                    import asyncpg
                    try:
                        await self.task_participant_storage.add(
                            task_id=updated.id, user_id=old_assignee,
                            added_by_user_id=actor_user_id,
                        )
                        # Old assignee stays in chat naturally — they're already a chat participant
                        # since they were assignee. ensure_task_chat formula keeps them.
                    except asyncpg.UniqueViolationError:
                        pass  # already a participant somehow — fine
                    except Exception as e:
                        logger.warning(f"swap-add participant failed: {e}")
```

- [ ] **Step 4: Wire `task_participant_storage` в EngineService**

В `engine_service.py` строка ~170 расширяем конструктор `TaskService`:

```python
        self.task_service = TaskService(
            self.task_storage,
            self.in_app_notification_service,
            chat_service=self.chat_service,
            task_event_service=self.task_event_service,
            project_service=self.project_service,
            task_notification_service=self.task_notification_service,
            user_storage=self.user_storage,
            task_participant_storage=self.task_participant_storage,  # NEW
        )
```

- [ ] **Step 5: Запустить тесты — PASS**

Run: `cd /root/rugpt && venv/bin/pytest tests/test_task_service_participants.py -v`
Expected: 9 passed.

- [ ] **Step 6: Полный engine suite**

Run: `cd /root/rugpt && venv/bin/pytest tests/ -x --ignore=tests/integration -q`
Expected: всё зелёное.


---

## Task 7: TaskNotificationService — `_resolve_recipients` helper + recipient lists + 3 text fixes

**Files:**
- Modify: `/root/rugpt/src/engine/services/task_notification_service.py`
- Test: `/root/rugpt/tests/test_resolve_recipients.py`
- Update: `/root/rugpt/tests/test_task_notifications.py` (regression)

- [ ] **Step 1: Тест helper'а**

`/root/rugpt/tests/test_resolve_recipients.py`:

```python
import os, pytest, pytest_asyncio, asyncpg
from src.engine.services.engine_service import EngineService

DSN = os.environ.get("DATABASE_URL", "postgresql://postgres@localhost/rugpt")


@pytest_asyncio.fixture
async def env():
    e = EngineService(); await e.init()
    pool = await asyncpg.create_pool(DSN)
    async with pool.acquire() as conn:
        org = await conn.fetchval(
            "INSERT INTO organizations (id, name) VALUES (gen_random_uuid(), 'rr') RETURNING id"
        )
        users = {}
        for tag in ("creator", "assignee", "p1", "p2", "inactive"):
            is_active = tag != "inactive"
            uid = await conn.fetchval(
                "INSERT INTO users (id, org_id, username, name, password_hash, is_active) "
                "VALUES (gen_random_uuid(), $1, $2, $2, 'x', $3) RETURNING id",
                org, f"rr_{tag}", is_active,
            )
            users[tag] = uid
    yield {"engine": e, "org": org, **users}
    async with pool.acquire() as conn:
        await conn.execute("DELETE FROM tasks WHERE org_id = $1", org)
        await conn.execute("DELETE FROM users WHERE org_id = $1", org)
        await conn.execute("DELETE FROM organizations WHERE id = $1", org)
    await pool.close()
    await e.close()


@pytest.mark.asyncio
async def test_resolve_recipients_unions_creator_assignee_participants(env):
    svc = env["engine"].task_service
    tns = env["engine"].task_notification_service
    task = await svc.create(
        org_id=env["org"], title="T",
        assignee_user_id=env["assignee"],
        created_by_user_id=env["creator"],
        participant_user_ids=[env["p1"], env["p2"]],
    )
    task = await svc.get(task.id)
    rec = await tns._resolve_recipients(task)
    ids = {u.id for u in rec}
    assert ids == {env["creator"], env["assignee"], env["p1"], env["p2"]}


@pytest.mark.asyncio
async def test_resolve_recipients_excludes_actor(env):
    svc = env["engine"].task_service
    tns = env["engine"].task_notification_service
    task = await svc.create(
        org_id=env["org"], title="T",
        assignee_user_id=env["assignee"],
        created_by_user_id=env["creator"],
        participant_user_ids=[env["p1"]],
    )
    task = await svc.get(task.id)
    rec = await tns._resolve_recipients(task, exclude_user_id=env["assignee"])
    ids = {u.id for u in rec}
    assert env["assignee"] not in ids
    assert ids == {env["creator"], env["p1"]}


@pytest.mark.asyncio
async def test_resolve_recipients_filters_inactive(env):
    """Inactive user must be filtered out even if listed in task_participants."""
    svc = env["engine"].task_service
    tns = env["engine"].task_notification_service
    task = await svc.create(
        org_id=env["org"], title="T",
        assignee_user_id=env["assignee"],
        created_by_user_id=env["creator"],
        participant_user_ids=[env["inactive"]],
    )
    task = await svc.get(task.id)
    rec = await tns._resolve_recipients(task)
    ids = {u.id for u in rec}
    assert env["inactive"] not in ids
```

- [ ] **Step 2: FAIL**

Run: `cd /root/rugpt && venv/bin/pytest tests/test_resolve_recipients.py -v`
Expected: AttributeError на `_resolve_recipients` или на `task_participant_storage` в `TaskNotificationService`.

- [ ] **Step 3: Расширить TaskNotificationService**

В `task_notification_service.py`:

(a) Расширить конструктор:

```python
    def __init__(
        self,
        chat_service: "ChatService",
        message_storage: "MessageStorage",
        user_storage: "UserStorage",
        kafka_producer: Optional["KafkaProducerService"] = None,
        task_participant_storage: Optional["TaskParticipantStorage"] = None,
    ):
        self.chat_service = chat_service
        self.message_storage = message_storage
        self.user_storage = user_storage
        self.kafka_producer = kafka_producer
        self.task_participant_storage = task_participant_storage
        self._pm_user_id: Optional[UUID] = None
```

И в TYPE_CHECKING:

```python
if TYPE_CHECKING:
    from ..storage.task_participant_storage import TaskParticipantStorage
```

(b) Helper:

```python
    async def _resolve_recipients(
        self, task: Task, exclude_user_id: Optional[UUID] = None,
    ) -> List["User"]:
        """
        Returns active users involved in the task: creator + assignee + participants.
        Deduplicates and excludes the actor (or any other user_id passed in).
        Filters out users with is_active=false.
        """
        candidate_ids: List[UUID] = []
        if task.created_by_user_id:
            candidate_ids.append(task.created_by_user_id)
        if task.assignee_user_id:
            candidate_ids.append(task.assignee_user_id)
        if self.task_participant_storage is not None:
            participant_ids = await self.task_participant_storage.list_user_ids(task.id)
            candidate_ids.extend(participant_ids)

        # Dedup + exclude
        seen = set()
        unique_ids: List[UUID] = []
        for uid in candidate_ids:
            if uid is None or uid == exclude_user_id or uid in seen:
                continue
            seen.add(uid)
            unique_ids.append(uid)

        # Load + filter inactive
        result: List["User"] = []
        for uid in unique_ids:
            user = await self.user_storage.get_by_id(uid)
            if user is None:
                continue
            if not getattr(user, "is_active", True):
                continue
            result.append(user)
        return result
```

(c) Заменить 9 `notify_*` методов через helper. Существующая логика «addressing rule» (assignee notifies creator, creator notifies assignee) заменяется на «notify everyone except actor».

Заменяем тела методов. Для KISS — все методы вызывают общий internal `_post_to_recipients`:

```python
    async def _post_to_recipients(
        self, task: Task, actor_user_id: Optional[UUID], text: str,
    ) -> None:
        recipients = await self._resolve_recipients(task, exclude_user_id=actor_user_id)
        for u in recipients:
            await self._post(u.id, task.org_id, text, task.id)

    async def notify_take(self, task: Task, actor: "User") -> None:
        text = (
            f"{self._actor_name(actor)} взял задачу «{task.title}» в работу.\n"
            f"→ Открыть: /chat/task/{task.id}"
        )
        await self._post_to_recipients(task, actor.id, text)

    async def notify_mark_done(self, task: Task, actor: "User") -> None:
        text = (
            f"{self._actor_name(actor)} отметил задачу «{task.title}» готовой. "
            f"Ожидает приёмки.\n"
            f"→ Открыть: /chat/task/{task.id}"
        )
        await self._post_to_recipients(task, actor.id, text)

    async def notify_accept(self, task: Task, actor: "User") -> None:
        text = f"Задача «{task.title}» принята."
        await self._post_to_recipients(task, actor.id, text)

    async def notify_reject(
        self, task: Task, actor: "User", comment: Optional[str] = None,
    ) -> None:
        text = f"Задача «{task.title}» возвращена в работу."
        if comment:
            text += f"\nПричина: {comment}"
        text += f"\n→ Открыть: /chat/task/{task.id}"
        await self._post_to_recipients(task, actor.id, text)

    async def notify_set_deadline(self, task: Task, actor: "User") -> None:
        deadline_str = task.deadline.strftime("%d.%m.%Y %H:%M") if task.deadline else "—"
        text = (
            f"Срок задачи «{task.title}» изменён: {deadline_str}\n"
            f"→ Открыть: /chat/task/{task.id}"
        )
        await self._post_to_recipients(task, actor.id, text)

    async def notify_propose_deadline(self, task: Task, actor: "User") -> None:
        proposed_str = (
            task.proposed_deadline.strftime("%d.%m.%Y %H:%M")
            if task.proposed_deadline else "—"
        )
        text = (
            f"{self._actor_name(actor)} предложил перенести срок задачи «{task.title}» "
            f"на {proposed_str}.\n"
            f"→ Принять или отклонить: /chat/task/{task.id}"
        )
        await self._post_to_recipients(task, actor.id, text)

    async def notify_accept_proposed_deadline(self, task: Task, actor: "User") -> None:
        deadline_str = task.deadline.strftime("%d.%m.%Y %H:%M") if task.deadline else "—"
        text = f"Предложение нового срока для «{task.title}» принято: {deadline_str}"
        await self._post_to_recipients(task, actor.id, text)

    async def notify_reject_proposed_deadline(self, task: Task, actor: "User") -> None:
        text = f"Предложение нового срока для «{task.title}» отклонено."
        await self._post_to_recipients(task, actor.id, text)

    async def notify_overdue(self, task: Task) -> None:
        """Scheduler-driven; no actor — notify everyone."""
        text = (
            f"Задача «{task.title}» просрочена.\n"
            f"→ Открыть: /chat/task/{task.id}"
        )
        await self._post_to_recipients(task, actor_user_id=None, text=text)
```

(d) Новые методы для добавления/удаления участника:

```python
    async def notify_added_as_participant(
        self, task: Task, user_id: UUID, by_user_id: Optional[UUID] = None,
    ) -> None:
        """Notify a single user that they were added as a participant of a task."""
        user = await self.user_storage.get_by_id(user_id)
        if user is None or not getattr(user, "is_active", True):
            return
        if by_user_id is not None and user_id == by_user_id:
            return  # don't notify yourself for self-add
        text = (
            f"Вас добавили в задачу «{task.title}» как участника.\n"
            f"→ Открыть: /chat/task/{task.id}"
        )
        await self._post(user_id, task.org_id, text, task.id)

    async def notify_removed_as_participant(
        self, task: Task, user_id: UUID, by_user_id: Optional[UUID] = None,
    ) -> None:
        user = await self.user_storage.get_by_id(user_id)
        if user is None or not getattr(user, "is_active", True):
            return
        if by_user_id is not None and user_id == by_user_id:
            return
        text = f"Вас исключили из задачи «{task.title}»."
        await self._post(user_id, task.org_id, text, task.id)
```

- [ ] **Step 4: Wire participant storage в EngineService**

В `engine_service.py` строка ~162:

```python
        self.task_notification_service = TaskNotificationService(
            chat_service=self.chat_service,
            message_storage=self.message_storage,
            user_storage=self.user_storage,
            kafka_producer=self.kafka_producer,
            task_participant_storage=self.task_participant_storage,  # NEW
        )
```

- [ ] **Step 5: Запустить новый тест — PASS**

Run: `cd /root/rugpt && venv/bin/pytest tests/test_resolve_recipients.py -v`
Expected: 3 passed.

- [ ] **Step 6: Прогнать существующий test_task_notifications.py**

Run: `cd /root/rugpt && venv/bin/pytest tests/test_task_notifications.py -v`

Expected: тесты могут поломаться из-за изменения receiver list (раньше один получатель, теперь два). Прочитать каждый failing test, обновить ассерты на новый receiver list (creator + assignee, минус actor).

- [ ] **Step 7: Полный suite**

Run: `cd /root/rugpt && venv/bin/pytest tests/ -x --ignore=tests/integration -q`
Expected: всё зелёное.


---

## Task 8: Engine routes — POST/DELETE participant + GET /participating + GET /done + extend serializer

**Files:**
- Modify: `/root/rugpt/src/engine/routes/tasks.py`
- Test: `/root/rugpt/tests/test_routes_task_participants.py` (route-level)

- [ ] **Step 1: Тесты route-уровня**

`/root/rugpt/tests/test_routes_task_participants.py`:

```python
"""Route-layer tests via FastAPI TestClient."""
import os, pytest, pytest_asyncio, asyncpg
from uuid import UUID
from httpx import AsyncClient, ASGITransport

from src.engine.app import app
from src.engine.services.engine_service import get_engine_service

DSN = os.environ.get("DATABASE_URL", "postgresql://postgres@localhost/rugpt")


@pytest_asyncio.fixture
async def setup():
    engine = get_engine_service()
    await engine.init()
    pool = await asyncpg.create_pool(DSN)
    async with pool.acquire() as conn:
        org = await conn.fetchval(
            "INSERT INTO organizations (id, name) VALUES (gen_random_uuid(), 'rt') RETURNING id"
        )
        creator = await conn.fetchval(
            "INSERT INTO users (id, org_id, username, name, password_hash) "
            "VALUES (gen_random_uuid(), $1, 'rt_c', 'C', 'x') RETURNING id", org,
        )
        assignee = await conn.fetchval(
            "INSERT INTO users (id, org_id, username, name, password_hash) "
            "VALUES (gen_random_uuid(), $1, 'rt_a', 'A', 'x') RETURNING id", org,
        )
        part = await conn.fetchval(
            "INSERT INTO users (id, org_id, username, name, password_hash) "
            "VALUES (gen_random_uuid(), $1, 'rt_p', 'P', 'x') RETURNING id", org,
        )
    yield {"engine": engine, "org": org, "creator": creator, "assignee": assignee, "part": part}
    async with pool.acquire() as conn:
        await conn.execute("DELETE FROM task_participants WHERE task_id IN (SELECT id FROM tasks WHERE org_id = $1)", org)
        await conn.execute("DELETE FROM tasks WHERE org_id = $1", org)
        await conn.execute("DELETE FROM users WHERE org_id = $1", org)
        await conn.execute("DELETE FROM organizations WHERE id = $1", org)
    await pool.close()


def _auth_dep_override(user_id, org_id, is_admin=False):
    """FastAPI dependency override for get_current_user."""
    from src.engine.routes.auth import get_current_user
    async def _override():
        return {"user_id": user_id, "org_id": org_id, "is_admin": is_admin, "is_head": False}
    return get_current_user, _override


@pytest.mark.asyncio
async def test_post_participant_201_and_409_on_duplicate(setup):
    from src.engine.routes.auth import get_current_user
    eng = setup["engine"]
    task = await eng.task_service.create(
        org_id=setup["org"], title="T",
        assignee_user_id=setup["assignee"], created_by_user_id=setup["creator"],
    )
    dep, override = _auth_dep_override(setup["creator"], setup["org"])
    app.dependency_overrides[dep] = override
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            r1 = await c.post(
                f"/api/v1/tasks/{task.id}/participants",
                json={"user_id": str(setup["part"])},
            )
            assert r1.status_code == 201
            assert r1.json()["id"] == str(setup["part"])

            r2 = await c.post(
                f"/api/v1/tasks/{task.id}/participants",
                json={"user_id": str(setup["part"])},
            )
            assert r2.status_code == 409
    finally:
        app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_delete_participant_204_then_404(setup):
    eng = setup["engine"]
    task = await eng.task_service.create(
        org_id=setup["org"], title="T",
        assignee_user_id=setup["assignee"], created_by_user_id=setup["creator"],
        participant_user_ids=[setup["part"]],
    )
    dep, override = _auth_dep_override(setup["creator"], setup["org"])
    app.dependency_overrides[dep] = override
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            r1 = await c.delete(f"/api/v1/tasks/{task.id}/participants/{setup['part']}")
            assert r1.status_code == 204
            r2 = await c.delete(f"/api/v1/tasks/{task.id}/participants/{setup['part']}")
            assert r2.status_code == 404
    finally:
        app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_get_participating_returns_participant_tasks(setup):
    eng = setup["engine"]
    task = await eng.task_service.create(
        org_id=setup["org"], title="T",
        assignee_user_id=setup["assignee"], created_by_user_id=setup["creator"],
        participant_user_ids=[setup["part"]],
    )
    dep, override = _auth_dep_override(setup["part"], setup["org"])
    app.dependency_overrides[dep] = override
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            r = await c.get("/api/v1/tasks/participating")
            assert r.status_code == 200
            ids = [t["id"] for t in r.json()]
            assert str(task.id) in ids
            # participants populated in serialized response
            entry = next(t for t in r.json() if t["id"] == str(task.id))
            assert any(p["id"] == str(setup["part"]) for p in entry.get("participants", []))
    finally:
        app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_get_done_unions_roles(setup):
    eng = setup["engine"]
    task = await eng.task_service.create(
        org_id=setup["org"], title="T",
        assignee_user_id=setup["assignee"], created_by_user_id=setup["creator"],
        participant_user_ids=[setup["part"]],
    )
    # Force to done
    from src.engine.models.user import User
    a_user = User(id=setup["assignee"], org_id=setup["org"], username="rt_a", name="A", password_hash="x")
    c_user = User(id=setup["creator"], org_id=setup["org"], username="rt_c", name="C", password_hash="x")
    await eng.task_service.take_task(task.id, a_user)
    await eng.task_service.mark_done(task.id, a_user)
    await eng.task_service.accept_task(task.id, c_user)

    for who in (setup["creator"], setup["assignee"], setup["part"]):
        dep, override = _auth_dep_override(who, setup["org"])
        app.dependency_overrides[dep] = override
        try:
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
                r = await c.get("/api/v1/tasks/done")
                assert r.status_code == 200
                ids = [t["id"] for t in r.json()]
                assert str(task.id) in ids, f"expected for user {who}"
        finally:
            app.dependency_overrides.clear()
```

- [ ] **Step 2: FAIL — endpoints отсутствуют**

Run: `cd /root/rugpt && venv/bin/pytest tests/test_routes_task_participants.py -v`
Expected: 404 на новых routes.

- [ ] **Step 3: Добавить endpoints в `routes/tasks.py`**

В начале файла рядом с другими request models:

```python
class AddParticipantRequest(BaseModel):
    user_id: str
```

Расширить `_serialize_entry` — после assignee добавить participants:

```python
def _serialize_entry(entry: dict) -> dict:
    out = entry["task"].to_dict()
    if entry.get("creator"):
        out["creator"] = {
            "id": str(entry["creator"]["id"]),
            "name": entry["creator"]["name"],
            "is_admin": entry["creator"]["is_admin"],
            "is_head": entry["creator"]["is_head"],
        }
    if entry.get("assignee"):
        out["assignee"] = {
            "id": str(entry["assignee"]["id"]),
            "name": entry["assignee"]["name"],
            "is_head": entry["assignee"]["is_head"],
        }
    if entry.get("participants") is not None:
        out["participants"] = [
            {"id": str(p["id"]), "name": p["name"]} for p in entry["participants"]
        ]
    return out
```

Helper для дотягивания participants списочными endpoint'ами (после `_load_user`):

```python
async def _hydrate_participants(engine, entries: list) -> list:
    """Bulk-fetch active participants for entries and attach as entry['participants']."""
    if not entries:
        return entries
    task_ids = [e["task"].id for e in entries]
    by_task = await engine.task_participant_storage.get_for_tasks(task_ids)
    for e in entries:
        e["participants"] = by_task.get(e["task"].id, [])
    return entries
```

Обернуть `list_my_tasks`, `list_created_by_me`, `list_archive` чтобы заполняли participants. Например для `list_my_tasks` (line ~118):

```python
@router.get("/my")
async def list_my_tasks(
    project_id: Optional[str] = Query(None),
    current_user: dict = Depends(get_current_user),
):
    """Tasks where current user is assignee, status != done."""
    engine = get_engine_service()
    entries = await engine.task_service.list_my_tasks(
        current_user["user_id"], include_done=False,
    )
    if project_id:
        try:
            pid = UUID(project_id)
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid project_id")
        entries = [e for e in entries if e["task"].project_id == pid]
    entries = await _hydrate_participants(engine, entries)
    return [_serialize_entry(e) for e in entries]
```

Аналогично для `list_created_by_me` (убрать параметр `include_done` из query — done уезжает в /done) и `list_archive` (там как было).

Новые endpoints (вставить после `list_archive`):

```python
@router.get("/participating")
async def list_participating(
    project_id: Optional[str] = Query(None),
    current_user: dict = Depends(get_current_user),
):
    """Tasks where current user is in task_participants, status != done."""
    engine = get_engine_service()
    entries = await engine.task_service.list_participating(
        current_user["user_id"], include_done=False,
    )
    if project_id:
        try:
            pid = UUID(project_id)
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid project_id")
        entries = [e for e in entries if e["task"].project_id == pid]
    entries = await _hydrate_participants(engine, entries)
    return [_serialize_entry(e) for e in entries]


@router.get("/done")
async def list_done(current_user: dict = Depends(get_current_user)):
    """All status='done' tasks where user is creator OR assignee OR participant."""
    engine = get_engine_service()
    entries = await engine.task_service.list_done(current_user["user_id"])
    entries = await _hydrate_participants(engine, entries)
    return [_serialize_entry(e) for e in entries]
```

`get_task` (line ~229) — расширить response чтобы дотягивать participants и расширить `_can_see_task`:

```python
async def _can_see_task_async(task, user, engine) -> bool:
    if task.org_id != user.org_id and not user.is_admin:
        return False
    if user.id == task.created_by_user_id:
        return True
    if user.id == task.assignee_user_id:
        return True
    if user.is_admin and task.org_id == user.org_id:
        return True
    # Participant access
    parts = await engine.task_participant_storage.list_user_ids(task.id)
    if user.id in parts:
        return True
    return False
```

`get_task` использовать новый async-checker, и в response добавить participants:

```python
@router.get("/{task_id}")
async def get_task(task_id: str, current_user: dict = Depends(get_current_user)):
    engine = get_engine_service()
    try:
        task_uuid = UUID(task_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid task ID")

    task = await engine.task_service.get(task_uuid)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")

    user = await _load_user(engine, current_user["user_id"])
    if user is None or not await _can_see_task_async(task, user, engine):
        raise HTTPException(status_code=403, detail="Access denied")

    out = task.to_dict()
    out["participants"] = await engine.task_participant_storage.list_active_user_dicts(task_uuid)
    return out
```

Маппинг `_can_see_task` для `/{task_id}/chat` и `/{task_id}/events` — также обновить через async check (заменить вызовы).

`create_task` — добавить participant_user_ids:

```python
class CreateTaskRequest(BaseModel):
    title: str
    description: Optional[str] = None
    assignee_user_id: str
    deadline: Optional[str] = None
    project_id: Optional[str] = None
    priority: Optional[int] = None
    participant_user_ids: Optional[list[str]] = None  # NEW
```

В обработчике (после deadline parsing):

```python
    participant_uuids: Optional[list] = None
    if request.participant_user_ids:
        try:
            participant_uuids = [UUID(p) for p in request.participant_user_ids]
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid participant_user_ids")
```

И передать в `create(...)`:

```python
        task = await engine.task_service.create(
            ...
            participant_user_ids=participant_uuids,
        )
```

Новые endpoints для участников:

```python
@router.post("/{task_id}/participants", status_code=201)
async def add_participant(
    task_id: str,
    request: AddParticipantRequest,
    current_user: dict = Depends(get_current_user),
):
    engine = get_engine_service()
    try:
        task_uuid = UUID(task_id)
        user_uuid = UUID(request.user_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid id")

    task = await engine.task_service.get(task_uuid)
    if task is None:
        raise HTTPException(status_code=404, detail="Task not found")
    if task.org_id != current_user["org_id"]:
        raise HTTPException(status_code=403, detail="Access denied")

    actor = await _load_user(engine, current_user["user_id"])
    if actor is None:
        raise HTTPException(status_code=401, detail="User not found")

    try:
        return await engine.task_service.add_participant(task_uuid, user_uuid, actor)
    except PermissionError as e:
        raise HTTPException(status_code=403, detail=str(e))
    except ValueError as e:
        msg = str(e)
        if "already a participant" in msg:
            raise HTTPException(status_code=409, detail=msg)
        raise HTTPException(status_code=400, detail=msg)


@router.delete("/{task_id}/participants/{user_id}", status_code=204)
async def remove_participant(
    task_id: str,
    user_id: str,
    current_user: dict = Depends(get_current_user),
):
    engine = get_engine_service()
    try:
        task_uuid = UUID(task_id)
        user_uuid = UUID(user_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid id")

    task = await engine.task_service.get(task_uuid)
    if task is None:
        raise HTTPException(status_code=404, detail="Task not found")
    if task.org_id != current_user["org_id"]:
        raise HTTPException(status_code=403, detail="Access denied")

    actor = await _load_user(engine, current_user["user_id"])
    if actor is None:
        raise HTTPException(status_code=401, detail="User not found")

    try:
        ok = await engine.task_service.remove_participant(task_uuid, user_uuid, actor)
    except PermissionError as e:
        raise HTTPException(status_code=403, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    if not ok:
        raise HTTPException(status_code=404, detail="Participant not found")
    return None  # 204 No Content
```

- [ ] **Step 4: PASS**

Run: `cd /root/rugpt && venv/bin/pytest tests/test_routes_task_participants.py -v`
Expected: 4 passed.

- [ ] **Step 5: Полный suite + smoke что движок стартует**

Run: `cd /root/rugpt && venv/bin/pytest tests/ -x --ignore=tests/integration -q`
Expected: всё зелёное.

Run: `cd /root/rugpt && venv/bin/python -c "from src.engine.app import app; print('app loaded')"`
Expected: `app loaded`.


---

## Task 9: Engine integration — chat sync end-to-end

**Files:**
- Test: `/root/rugpt/tests/integration/test_task_participants_chat_sync.py`

- [ ] **Step 1: Тест — добавление participant'а попадает в task chat и project chat**

`/root/rugpt/tests/integration/test_task_participants_chat_sync.py`:

```python
import os, pytest, pytest_asyncio, asyncpg
from uuid import UUID

from src.engine.services.engine_service import EngineService
from src.engine.models.user import User

DSN = os.environ.get("DATABASE_URL", "postgresql://postgres@localhost/rugpt")


@pytest_asyncio.fixture
async def env():
    e = EngineService(); await e.init()
    pool = await asyncpg.create_pool(DSN)
    async with pool.acquire() as conn:
        org = await conn.fetchval(
            "INSERT INTO organizations (id, name) VALUES (gen_random_uuid(), 'is') RETURNING id"
        )
        users = {}
        for tag in ("c", "a", "p1", "p2"):
            users[tag] = await conn.fetchval(
                "INSERT INTO users (id, org_id, username, name, password_hash) "
                "VALUES (gen_random_uuid(), $1, $2, $2, 'x') RETURNING id",
                org, f"is_{tag}",
            )
        project = await conn.fetchval(
            "INSERT INTO projects (id, org_id, name, created_by_user_id) "
            "VALUES (gen_random_uuid(), $1, 'P', $2) RETURNING id",
            org, users["c"],
        )
    yield {"engine": e, "org": org, "project": project, **users}
    async with pool.acquire() as conn:
        await conn.execute(
            "DELETE FROM task_participants WHERE task_id IN "
            "(SELECT id FROM tasks WHERE org_id = $1)", org,
        )
        await conn.execute("DELETE FROM messages WHERE chat_id IN "
                           "(SELECT id FROM chats WHERE org_id = $1)", org)
        await conn.execute("DELETE FROM chats WHERE org_id = $1", org)
        await conn.execute("DELETE FROM tasks WHERE org_id = $1", org)
        await conn.execute("DELETE FROM projects WHERE org_id = $1", org)
        await conn.execute("DELETE FROM users WHERE org_id = $1", org)
        await conn.execute("DELETE FROM organizations WHERE id = $1", org)
    await pool.close()
    await e.close()


def _user(uid, org, *, is_admin=False):
    return User(id=uid, org_id=org, username="x", name="x", password_hash="x", is_admin=is_admin)


@pytest.mark.asyncio
async def test_add_participant_appears_in_task_chat(env):
    svc = env["engine"].task_service
    task = await svc.create(
        org_id=env["org"], title="T",
        assignee_user_id=env["a"], created_by_user_id=env["c"],
    )
    creator = _user(env["c"], env["org"])
    await svc.add_participant(task.id, env["p1"], creator)
    chat = await env["engine"].chat_service.get_task_chat(task.id)
    assert env["p1"] in chat.participants


@pytest.mark.asyncio
async def test_add_participant_appears_in_project_chat(env):
    svc = env["engine"].task_service
    task = await svc.create(
        org_id=env["org"], title="T",
        assignee_user_id=env["a"], created_by_user_id=env["c"],
        project_id=env["project"],
    )
    creator = _user(env["c"], env["org"])
    await svc.add_participant(task.id, env["p1"], creator)
    chat = await env["engine"].chat_service.chat_storage.get_by_project_id(env["project"])
    assert env["p1"] in chat.participants


@pytest.mark.asyncio
async def test_remove_participant_drops_from_chat(env):
    svc = env["engine"].task_service
    task = await svc.create(
        org_id=env["org"], title="T",
        assignee_user_id=env["a"], created_by_user_id=env["c"],
        participant_user_ids=[env["p1"]],
    )
    creator = _user(env["c"], env["org"])
    await svc.remove_participant(task.id, env["p1"], creator)
    chat = await env["engine"].chat_service.get_task_chat(task.id)
    assert env["p1"] not in chat.participants


@pytest.mark.asyncio
async def test_remove_participant_keeps_user_in_project_if_in_other_task(env):
    """User должен остаться в project chat если он participant другой задачи проекта."""
    svc = env["engine"].task_service
    creator = _user(env["c"], env["org"])
    t1 = await svc.create(
        org_id=env["org"], title="T1",
        assignee_user_id=env["a"], created_by_user_id=env["c"],
        project_id=env["project"], participant_user_ids=[env["p1"]],
    )
    t2 = await svc.create(
        org_id=env["org"], title="T2",
        assignee_user_id=env["a"], created_by_user_id=env["c"],
        project_id=env["project"], participant_user_ids=[env["p1"]],
    )
    await svc.remove_participant(t1.id, env["p1"], creator)
    pchat = await env["engine"].chat_service.chat_storage.get_by_project_id(env["project"])
    assert env["p1"] in pchat.participants  # ещё в t2

    await svc.remove_participant(t2.id, env["p1"], creator)
    pchat = await env["engine"].chat_service.chat_storage.get_by_project_id(env["project"])
    assert env["p1"] not in pchat.participants  # больше нигде


@pytest.mark.asyncio
async def test_assignee_swap_in_chat(env):
    """После reassign старый assignee остаётся в чате, новый тоже в чате — оба видны."""
    svc = env["engine"].task_service
    task = await svc.create(
        org_id=env["org"], title="T",
        assignee_user_id=env["a"], created_by_user_id=env["c"],
        participant_user_ids=[env["p1"]],
    )
    await svc.update(
        task_id=task.id,
        assignee_user_id=env["p1"],
        actor_user_id=env["c"],
    )
    chat = await env["engine"].chat_service.get_task_chat(task.id)
    assert env["a"] in chat.participants  # старый assignee остался (теперь participant)
    assert env["p1"] in chat.participants  # новый assignee
    assert env["c"] in chat.participants  # creator
```

- [ ] **Step 2: PASS**

Run: `cd /root/rugpt && venv/bin/pytest tests/integration/test_task_participants_chat_sync.py -v`
Expected: 5 passed.


---

## Task 10: WebClient backend — adapter command + types

**Files:**
- Modify: `/root/webclient_rugpt/packages/backend/src/engine/adapters/rugpt.adapter.ts`
- Modify: `/root/webclient_rugpt/packages/common/src/types/task.ts` (или tasks-related файл)

- [ ] **Step 1: Найти текущий случай отображения task adapter и команды**

Run:
```bash
grep -n "case 'tasks_my'\|case 'tasks_created\|case 'tasks_archive'" /root/webclient_rugpt/packages/backend/src/engine/adapters/rugpt.adapter.ts
```

Identify pattern. Существующие task command'ы — пример для копирования.

- [ ] **Step 2: Добавить 4 новых command'а в `rugpt.adapter.ts`**

В блок `switch (command)` после существующих task case добавить:

```typescript
case 'tasks_participating': {
  return this.get('/tasks/participating', payload?.headers);
}
case 'tasks_done': {
  return this.get('/tasks/done', payload?.headers);
}
case 'add_task_participant': {
  const { task_id, user_id, headers } = payload;
  return this.post(`/tasks/${encodeURIComponent(task_id)}/participants`, { user_id }, headers);
}
case 'remove_task_participant': {
  const { task_id, user_id, headers } = payload;
  return this.delete(`/tasks/${encodeURIComponent(task_id)}/participants/${encodeURIComponent(user_id)}`, headers);
}
```

Также **расширить существующий `case 'create_task'`** body — пробросить `participant_user_ids`:

Найти его (grep `case 'create_task'`) и добавить в body:

```typescript
const body: any = {
  title: payload.title,
  description: payload.description,
  assignee_user_id: payload.assignee_user_id,
  deadline: payload.deadline ?? null,
  project_id: payload.project_id ?? null,
  priority: payload.priority ?? null,
  participant_user_ids: payload.participant_user_ids ?? null,  // NEW
};
```

- [ ] **Step 3: Расширить общий тип `TaskRow` в common**

В `/root/webclient_rugpt/packages/common/src/types/task.ts` (если файла нет — найти где TaskRow определён через `grep -rn "TaskRow\|interface Task" /root/webclient_rugpt/packages/common/src/`):

Добавить:

```typescript
export interface TaskParticipant {
  id: string;
  name: string;
}

// In TaskRow:
participants?: TaskParticipant[];  // optional, present in /my, /created-by-me, /participating, /done, /archive, GET /{id}
```

- [ ] **Step 4: Build backend для сохранности типов**

Run: `cd /root/webclient_rugpt/packages/backend && npm run build`
Expected: build success, no TS errors.


---

## Task 11: WebClient backend — task service + controller routes

**Files:**
- Modify: `/root/webclient_rugpt/packages/backend/src/task/task.service.ts`
- Modify: `/root/webclient_rugpt/packages/backend/src/task/task.controller.ts`

- [ ] **Step 1: Добавить методы в task.service.ts**

```typescript
async listParticipating(headers: any) {
  return this.engineAdapter.execute('tasks_participating', { headers });
}

async listDone(headers: any) {
  return this.engineAdapter.execute('tasks_done', { headers });
}

async addParticipant(taskId: string, userId: string, headers: any) {
  return this.engineAdapter.execute('add_task_participant', {
    task_id: taskId, user_id: userId, headers,
  });
}

async removeParticipant(taskId: string, userId: string, headers: any) {
  return this.engineAdapter.execute('remove_task_participant', {
    task_id: taskId, user_id: userId, headers,
  });
}
```

И расширить `createTask` чтобы принимал `participant_user_ids`:

```typescript
async createTask(body: CreateTaskDto & { participant_user_ids?: string[] }, headers: any) {
  return this.engineAdapter.execute('create_task', { ...body, headers });
}
```

- [ ] **Step 2: Добавить routes в task.controller.ts**

После существующих GET routes:

```typescript
@Get('participating')
async listParticipating(@Req() req) {
  return this.taskService.listParticipating(req.headers);
}

@Get('done')
async listDone(@Req() req) {
  return this.taskService.listDone(req.headers);
}
```

После CRUD routes:

```typescript
@Post(':taskId/participants')
async addParticipant(
  @Param('taskId') taskId: string,
  @Body() body: { user_id: string },
  @Req() req,
) {
  return this.taskService.addParticipant(taskId, body.user_id, req.headers);
}

@Delete(':taskId/participants/:userId')
@HttpCode(204)
async removeParticipant(
  @Param('taskId') taskId: string,
  @Param('userId') userId: string,
  @Req() req,
) {
  return this.taskService.removeParticipant(taskId, userId, req.headers);
}
```

(Импорт `HttpCode` from `@nestjs/common` если его ещё нет.)

- [ ] **Step 3: Build + e2e smoke**

Run: `cd /root/webclient_rugpt/packages/backend && npm run build`
Expected: success.

Run: `cd /root/webclient_rugpt && ./dev.sh restart` (если есть, иначе запустить вручную). Смоук `curl http://localhost:4000/api/tasks/participating` — ожидаем 401 без токена (а не 404).


---

## Task 12: Frontend — tabs «Участвую» + «Выполненные», убрать чекбокс include_done

**Files:**
- Modify: `/root/webclient_rugpt/packages/frontend/src/app/tasks/page.tsx`

- [ ] **Step 1: Обновить тип Tab и стейт**

В начале файла (строка ~17):

```typescript
type Tab = 'my' | 'created' | 'participating' | 'done' | 'archive';
```

Удалить `includeDone` state и связанный чекбокс UI.

- [ ] **Step 2: Расширить fetchTasks (строка ~125)**

```typescript
const fetchTasks = useCallback(async () => {
  setLoading(true);
  setError(null);
  try {
    const api = getApiClient();
    let path: string;
    if (tab === 'archive') path = '/api/tasks/archive';
    else if (tab === 'my') path = '/api/tasks/my';
    else if (tab === 'created') path = '/api/tasks/created-by-me';
    else if (tab === 'participating') path = '/api/tasks/participating';
    else path = '/api/tasks/done';
    const data = await api.signedGet<TaskRow[]>(path, currentUser?.id);
    setTasks(data || []);
  } catch (e) {
    setError(e instanceof Error ? e.message : 'Ошибка загрузки');
  } finally {
    setLoading(false);
  }
}, [tab, currentUser?.id]);
```

- [ ] **Step 3: Обновить tab buttons UI (строка ~337)**

```tsx
<button
  onClick={() => setTab('my')}
  className={tab === 'my' ? activeClass : inactiveClass}
>
  Мои <span className="text-xs opacity-60">({myCount})</span>
</button>
<button
  onClick={() => setTab('created')}
  className={tab === 'created' ? activeClass : inactiveClass}
>
  Поставленные мной
</button>
<button
  onClick={() => setTab('participating')}
  className={tab === 'participating' ? activeClass : inactiveClass}
>
  Участвую
</button>
<button
  onClick={() => setTab('done')}
  className={tab === 'done' ? activeClass : inactiveClass}
>
  Выполненные
</button>
<button
  onClick={() => setTab('archive')}
  className={tab === 'archive' ? activeClass : inactiveClass}
>
  Архив
</button>
```

(Где `activeClass` / `inactiveClass` — те же что в существующем коде; не выдумывать новое стилирование.)

Удалить блок `{tab !== 'archive' && (...)}` который содержит checkbox include_done.

`myCount` — простой подсчёт текущего списка задач при `tab='my'`. Если хотим точный счётчик независимо от вкладки, требуется отдельный API. **Решение для первой итерации:** подсчёт показываем только когда вкладка активна, иначе скрываем. Реализовать:

```typescript
const tabCount = (t: Tab): string => (tab === t ? `(${tasks.length})` : '');
```

И в кнопках использовать `{tabCount('my')}` и т.д. Это явный compromise — для глобальных счётчиков нужен отдельный backend endpoint, его в эту задачу не включаем.

- [ ] **Step 4: Обновить колонку «Создатель»/«Исполнитель» (строка ~415)**

```tsx
<th className="...">{
  tab === 'my' ? 'Создатель'
  : tab === 'created' ? 'Исполнитель'
  : tab === 'participating' ? 'Создатель'
  : tab === 'done' ? 'Исполнитель'
  : 'Исполнитель'
}</th>
```

Логика какое имя выводить:

```tsx
const otherPartyName = (t: TaskRow): string => {
  if (tab === 'my' || tab === 'participating') return t.creator?.name || '—';
  return t.assignee?.name || '—';
};
```

- [ ] **Step 5: Smoke в браузере**

Запустить webclient (`cd /root/webclient_rugpt && ./dev.sh`).
Открыть `http://localhost:3000/tasks`. Проверить:
- 5 кнопок вкладок: Мои / Поставленные мной / Участвую / Выполненные / Архив.
- Чекбокс «Показывать выполненные» отсутствует.
- Клик по `Участвую` дёргает `/api/tasks/participating`. Без участия — пустой список.
- Клик по `Выполненные` — `/api/tasks/done`.


---

## Task 13: Frontend — render participants chips в строке списка

**Files:**
- Modify: `/root/webclient_rugpt/packages/frontend/src/app/tasks/page.tsx`

- [ ] **Step 1: Добавить компонент-helper для chip**

Где-то рядом с другими render helpers:

```tsx
function ParticipantChips({ participants }: { participants?: { id: string; name: string }[] }) {
  if (!participants || participants.length === 0) return null;
  const visible = participants.slice(0, 3);
  const overflow = participants.length - visible.length;
  return (
    <div className="flex items-center gap-1 mt-1">
      {visible.map(p => (
        <span
          key={p.id}
          title={p.name}
          className="inline-flex items-center justify-center w-6 h-6 rounded-full bg-gray-200 text-gray-700 text-xs font-medium"
        >
          {p.name.slice(0, 1).toUpperCase()}
        </span>
      ))}
      {overflow > 0 && (
        <span className="text-xs text-gray-500 ml-1">+{overflow}</span>
      )}
    </div>
  );
}
```

- [ ] **Step 2: Встроить в строку таблицы**

В ячейке task title (или отдельной small ячейке) после имени задачи:

```tsx
<div>
  <div className="font-medium">{t.title}</div>
  <ParticipantChips participants={t.participants} />
</div>
```

- [ ] **Step 3: Smoke**

Открыть `/tasks`, выбрать задачу с participants (создать через UI после Task 14, или через psql вручную для теста). Видим круглые avatars-инициалы.


---

## Task 14: Frontend — multiselect участников в Create modal

**Files:**
- Modify: `/root/webclient_rugpt/packages/frontend/src/app/tasks/page.tsx`

- [ ] **Step 1: Расширить createForm state**

```typescript
const [createForm, setCreateForm] = useState<{
  title: string;
  description: string;
  assigneeUserId: string;
  deadline: string;
  projectId: string;
  priority: number;
  participantUserIds: string[];  // NEW
}>({
  title: '', description: '', assigneeUserId: '',
  deadline: '', projectId: '', priority: 1,
  participantUserIds: [],
});
```

- [ ] **Step 2: Добавить multiselect под select assignee (строка ~787)**

После `<select>` ассайни:

```tsx
<div className="mt-3">
  <label className="block text-sm font-medium mb-1">Участники</label>
  <select
    multiple
    value={createForm.participantUserIds}
    onChange={(e) => {
      const opts = Array.from(e.target.selectedOptions).map(o => o.value);
      setCreateForm({ ...createForm, participantUserIds: opts });
    }}
    className="w-full border rounded p-2 h-32"
  >
    {users
      .filter(u =>
        !u.is_system
        && u.id !== createForm.assigneeUserId
        && u.id !== currentUser?.id
      )
      .map(u => (
        <option key={u.id} value={u.id}>{u.name}</option>
      ))
    }
  </select>
  <p className="text-xs text-gray-500 mt-1">
    Удерживайте Ctrl/Cmd для выбора нескольких
  </p>
</div>
```

- [ ] **Step 3: Передать в submit**

В обработчике submit модалки create (строка ~256):

```typescript
const body: any = {
  title: trimmedTitle,
  description: createForm.description,
  assignee_user_id: createForm.assigneeUserId,
  deadline: createForm.deadline || null,
  project_id: createForm.projectId || null,
  priority: createForm.priority,
};
if (createForm.participantUserIds.length > 0) {
  body.participant_user_ids = createForm.participantUserIds;
}
```

И reset на `participantUserIds: []` после успешного создания (строка ~272).

- [ ] **Step 4: Smoke**

Создать задачу с 2-3 участниками, проверить:
- Сетка participants chips в строке появилась
- В БД `psql ... "SELECT * FROM task_participants WHERE task_id = '<id>'"` — 2-3 строки


---

## Task 15: Frontend — управление участниками в Edit modal

**Files:**
- Modify: `/root/webclient_rugpt/packages/frontend/src/app/tasks/page.tsx`

- [ ] **Step 1: Локальный state для participants в edit modal**

```typescript
const [editParticipants, setEditParticipants] = useState<{ id: string; name: string }[]>([]);
const [addParticipantPickerOpen, setAddParticipantPickerOpen] = useState(false);
```

При открытии edit modal заполняется из `t.participants`. При закрытии — сброс.

- [ ] **Step 2: Helper — может ли текущий юзер управлять списком**

```typescript
const canManageParticipants = (t: TaskRow): boolean => {
  if (currentUser?.is_admin) return true;
  if ((currentUser as any)?.is_head) return true;
  return t.creator?.id === currentUser?.id;
};
```

- [ ] **Step 3: Блок UI в edit modal**

Найти секцию edit modal (строка ~520-580 в файле). Добавить блок участников:

```tsx
<div className="mt-4">
  <label className="block text-sm font-medium mb-2">Участники</label>
  <div className="flex flex-wrap gap-2">
    {editParticipants.map(p => (
      <span
        key={p.id}
        className="inline-flex items-center gap-1 px-3 py-1 bg-gray-100 rounded-full text-sm"
      >
        {p.name}
        {canManageParticipants(editingTask!) && (
          <button
            type="button"
            onClick={() => removeEditParticipant(p.id)}
            className="text-gray-500 hover:text-red-600 ml-1"
            title="Убрать"
          >
            ×
          </button>
        )}
      </span>
    ))}
    {canManageParticipants(editingTask!) && (
      <button
        type="button"
        onClick={() => setAddParticipantPickerOpen(v => !v)}
        className="text-sm text-blue-600 hover:underline"
      >
        + Добавить участника
      </button>
    )}
  </div>
  {addParticipantPickerOpen && canManageParticipants(editingTask!) && (
    <select
      onChange={(e) => {
        if (e.target.value) {
          addEditParticipant(e.target.value);
          e.target.value = '';
        }
      }}
      className="mt-2 w-full border rounded p-2"
      defaultValue=""
    >
      <option value="" disabled>Выберите пользователя...</option>
      {users
        .filter(u =>
          !u.is_system
          && u.id !== editingTask!.assignee?.id
          && u.id !== editingTask!.creator?.id
          && !editParticipants.some(p => p.id === u.id)
        )
        .map(u => (
          <option key={u.id} value={u.id}>{u.name}</option>
        ))
      }
    </select>
  )}
</div>
```

- [ ] **Step 4: Добавить inline API вызовы**

```typescript
const addEditParticipant = async (userId: string) => {
  if (!editingTask) return;
  try {
    const api = getApiClient();
    const added: { id: string; name: string } = await api.signedPost(
      `/api/tasks/${editingTask.id}/participants`,
      { user_id: userId },
      currentUser?.id,
    );
    setEditParticipants(prev => [...prev, added]);
    fetchTasks();  // refresh list
  } catch (e) {
    setEditError(e instanceof Error ? e.message : 'Ошибка добавления участника');
  }
};

const removeEditParticipant = async (userId: string) => {
  if (!editingTask) return;
  try {
    const api = getApiClient();
    await api.signedDelete(
      `/api/tasks/${editingTask.id}/participants/${userId}`,
      currentUser?.id,
    );
    setEditParticipants(prev => prev.filter(p => p.id !== userId));
    fetchTasks();
  } catch (e) {
    setEditError(e instanceof Error ? e.message : 'Ошибка удаления участника');
  }
};
```

При open modal — заполнить `editParticipants` из `t.participants`:

В обработчике `handleEdit` (строка ~200):

```typescript
setEditParticipants(t.participants || []);
```

При close — сброс `setEditParticipants([])`.

- [ ] **Step 5: Smoke**

Открыть edit modal задачи. Видим список участников. Если ты creator — есть × и кнопка `+ Добавить`. Если ты participant/assignee — read-only.

Clicking + → выбор → POST → бейдж появляется. Clicking × → DELETE → бейдж исчезает.

В строке таблицы chips обновляются (через `fetchTasks()`).


---

## Task 16: End-to-end smoke test

**Goal:** Прокатить полный сценарий через UI на dev.

- [ ] **Step 1: Подготовка**

Run: `cd /root/rugpt && ./migrate.sh`  (миграция 032 если не применилась)
Run: engine + webclient запущены.

- [ ] **Step 2: Сценарий 1 — создание задачи с участниками**

1. Залогиниться creator'ом.
2. Открыть `/tasks`, нажать «Создать задачу».
3. В форме: title, assignee, выбрать 2 участников.
4. Submit.
5. Проверить:
   - Задача появилась во вкладке «Поставленные мной».
   - В строке таблицы видны 2 chips с инициалами.
   - В модалке (edit) показаны участники + кнопка `+`.

- [ ] **Step 3: Сценарий 2 — участник видит задачу в «Участвую»**

1. Перелогин в одного из participants.
2. Открыть `/tasks`, вкладка «Участвую».
3. Проверить — задача в списке.
4. Открыть task chat — пользователь там есть, может писать.

- [ ] **Step 4: Сценарий 3 — добавление/удаление участника inline**

1. Под creator'ом open edit modal задачи.
2. Кнопка `+ Добавить участника` → выбрать.
3. Бейдж появился, в БД есть строка.
4. Кликнуть `×` на любом участнике → исчезает.

- [ ] **Step 5: Сценарий 4 — reassign auto-swap**

1. У задачи где есть participant P, поменять assignee на P через edit modal.
2. После сохранения: P пропал из участников, старый assignee появился в участниках.
3. Task chat: оба видны, состав не упал.

- [ ] **Step 6: Сценарий 5 — done переезжает во вкладку «Выполненные»**

1. Assignee → take → mark_done.
2. Creator → accept.
3. Задача исчезла из «Поставленные мной» (где раньше была).
4. Появилась во вкладке «Выполненные».
5. Тот же эффект для assignee и для participant.

- [ ] **Step 7: Сценарий 6 — уведомления**

1. У задачи есть participant P.
2. Assignee делает mark_done.
3. P получает PM-сообщение в свой direct чат «{actor} отметил задачу «X» готовой...».


---

## Spec coverage check

Прошёлся по спеке — все требования покрыты:

| Spec section | Plan task |
|---|---|
| Migration 032 schema | Task 1 |
| TaskParticipant model | Task 2 |
| TaskParticipantStorage with bulk + sync | Task 3 |
| `user_has_any_active_task_in_project` + list_done + list_by_participant | Task 4 |
| Wire in EngineService | Tasks 5, 6, 7 |
| API: POST/DELETE participants | Task 8 |
| API: GET /participating + /done | Task 8 |
| Removed `?include_done` from /my, /created-by-me | Task 8 |
| `participants[]` in serialized response | Task 8 |
| `participant_user_ids` in POST /tasks | Tasks 6, 8 |
| Permissions creator/head/admin | Task 6 (`_check_creator_or_head`) |
| Reject add of assignee/creator (400) | Task 6 |
| Hook: add_participant → chat sync, project chat sync, audit, notify | Task 6 |
| Hook: remove_participant → cleanup из chat и project chat | Task 6 |
| Hook: reassign auto-swap | Task 6 |
| `_resolve_recipients` helper | Task 7 |
| 3 текстовые правки (accept, accept_proposed, reject_proposed) | Task 7 |
| Notify added/removed as participant | Task 7 |
| Inactive users filtered | Tasks 3, 7 (хранилище фильтрует, helper тоже) |
| Concurrency PK 409 | Task 3 (storage) + Task 6 (service maps to ValueError) |
| WebClient adapter commands | Task 10 |
| WebClient backend routes | Task 11 |
| Frontend tabs Участвую/Выполненные | Task 12 |
| Frontend убрать include_done checkbox | Task 12 |
| Frontend chips в строке | Task 13 |
| Frontend create modal multiselect | Task 14 |
| Frontend edit modal управление | Task 15 |
| Smoke test | Task 16 |
