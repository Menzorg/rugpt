# Task Ownership & Prioritization Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Добавить владение задачами (creator), приоритет по роли создателя, новый статус `awaiting_review` с приёмкой работы только создателем, негоциацию дедлайна, и UI с двумя вкладками "Мои задачи" / "Поставленные мной".

**Architecture:** Расширение существующей системы задач (Engine). Миграция БД добавляет 4 новых поля на `tasks`. TaskService получает новые методы для статусных переходов и негоциации, TaskStorage — JOIN на users для приоритета. Routes `/tasks/*` дополняются семантическими endpoint'ами (`/take`, `/mark-done`, `/accept`, `/reject`, `/deadline*`). WebClient получает новые вкладки и inline-действия в строках таблицы.

**Tech Stack:** Python 3.10+ / FastAPI / asyncpg / pytest (Engine), NestJS / Next.js / Tailwind CSS (WebClient).

**Spec:** `/root/rugpt/docs/superpowers/specs/2026-04-13-task-ownership-design.md`

---

## File Structure

**Engine — новые файлы:**
- `src/engine/migrations/016_task_ownership.sql` — миграция БД
- `tests/test_task_ownership.py` — unit-тесты для новых методов TaskService

**Engine — изменённые файлы:**
- `src/engine/models/task.py` — новые поля + расширение `to_dict()`
- `src/engine/storage/task_storage.py` — новые методы (`list_by_creator`, `get_with_creator`), обновление существующих
- `src/engine/services/task_service.py` — новые методы переходов статуса, методы дедлайна, вычисление приоритета
- `src/engine/routes/tasks.py` — новые эндпоинты, обновление существующих
- `src/engine/agents/tools/task_tool.py` — прокидывать `created_by_user_id` в `task_create`

**WebClient backend — изменённые файлы:**
- `packages/backend/src/engine/adapters/rugpt.adapter.ts` — новые команды для семантических переходов
- `packages/backend/src/task/task.service.ts` — новые методы-обёртки
- `packages/backend/src/task/task.controller.ts` — новые роуты

**WebClient frontend — изменённые файлы:**
- `packages/common/src/types/task.ts` — расширить `Task` интерфейс новыми полями и `priority`
- `packages/frontend/src/app/tasks/page.tsx` — переписать с двумя вкладками, expand-row, inline-actions

---

## Phase 1 — Engine: миграция и модель данных

### Task 1: Миграция БД 016

**Files:**
- Create: `/root/rugpt/src/engine/migrations/016_task_ownership.sql`

- [ ] **Step 1: Создать миграцию**

```sql
-- Migration 016: Task ownership and deadline negotiation
ALTER TABLE tasks
    ADD COLUMN created_by_user_id UUID REFERENCES users(id),
    ADD COLUMN awaiting_review_at TIMESTAMP WITH TIME ZONE,
    ADD COLUMN proposed_deadline TIMESTAMP WITH TIME ZONE,
    ADD COLUMN proposed_deadline_by UUID REFERENCES users(id);

CREATE INDEX IF NOT EXISTS idx_tasks_created_by
    ON tasks(created_by_user_id)
    WHERE is_active = true;

COMMENT ON COLUMN tasks.created_by_user_id IS 'User who created the task (for priority calculation and ownership)';
COMMENT ON COLUMN tasks.awaiting_review_at IS 'Set when assignee moves task to awaiting_review; NULL otherwise';
COMMENT ON COLUMN tasks.proposed_deadline IS 'Alternative deadline proposed by assignee; NULL if no pending proposal';
COMMENT ON COLUMN tasks.proposed_deadline_by IS 'User who proposed the alternative deadline';
```

- [ ] **Step 2: Применить миграцию**

Run: `cd /root/rugpt && ./migrate.sh`
Expected: миграция 016 применяется без ошибок, новые поля видны в `\d tasks` через `psql -U postgres -h localhost -d rugpt`.

- [ ] **Step 3: Verify через psql**

Run: `psql -U postgres -h localhost -d rugpt -c "\d tasks"`
Expected: вывод содержит `created_by_user_id`, `awaiting_review_at`, `proposed_deadline`, `proposed_deadline_by`. Все nullable.

---

### Task 2: Расширение модели Task

**Files:**
- Modify: `/root/rugpt/src/engine/models/task.py`

- [ ] **Step 1: Обновить dataclass и to_dict**

Полностью заменить содержимое файла на:

```python
"""
Task Model

Employee tasks managed by AI roles.
Created via chat (@@mention) or UI.
"""
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional
from uuid import UUID, uuid4


VALID_STATUSES = {"created", "in_progress", "awaiting_review", "done", "overdue"}


@dataclass
class Task:
    """
    Employee task.

    Statuses: created, in_progress, awaiting_review, done, overdue.
    """
    id: UUID = field(default_factory=uuid4)
    org_id: UUID = field(default_factory=uuid4)
    title: str = ""
    description: Optional[str] = None
    status: str = "created"
    assignee_user_id: UUID = field(default_factory=uuid4)
    created_by_user_id: Optional[UUID] = None
    deadline: Optional[datetime] = None
    awaiting_review_at: Optional[datetime] = None
    proposed_deadline: Optional[datetime] = None
    proposed_deadline_by: Optional[UUID] = None
    is_active: bool = True
    created_at: datetime = field(default_factory=datetime.utcnow)
    updated_at: datetime = field(default_factory=datetime.utcnow)

    def to_dict(self) -> dict:
        """Convert to dictionary for API response"""
        return {
            "id": str(self.id),
            "org_id": str(self.org_id),
            "title": self.title,
            "description": self.description,
            "status": self.status,
            "assignee_user_id": str(self.assignee_user_id),
            "created_by_user_id": str(self.created_by_user_id) if self.created_by_user_id else None,
            "deadline": self.deadline.isoformat() if self.deadline else None,
            "awaiting_review_at": self.awaiting_review_at.isoformat() if self.awaiting_review_at else None,
            "proposed_deadline": self.proposed_deadline.isoformat() if self.proposed_deadline else None,
            "proposed_deadline_by": str(self.proposed_deadline_by) if self.proposed_deadline_by else None,
            "is_active": self.is_active,
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
        }
```

- [ ] **Step 2: Импорт-проверка**

Run: `cd /root/rugpt && venv/bin/python -c "from src.engine.models.task import Task, VALID_STATUSES; t = Task(); print(t.to_dict())"`
Expected: словарь с новыми ключами `created_by_user_id`, `awaiting_review_at`, `proposed_deadline`, `proposed_deadline_by` (все None для пустого Task), без ошибок импорта.

---

### Task 3: Обновление TaskStorage — поля + JOIN на creator

**Files:**
- Modify: `/root/rugpt/src/engine/storage/task_storage.py`

- [ ] **Step 1: Обновить `_row_to_task` + `create` + `update`**

Полностью заменить содержимое файла:

```python
"""
Task Storage

PostgreSQL CRUD for tasks table.
"""
import logging
from datetime import datetime
from typing import Optional, List, Tuple
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
                 is_active, created_at, updated_at)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14)
            RETURNING *
        """
        row = await self.fetchrow(
            query,
            task.id, task.org_id, task.title, task.description, task.status,
            task.assignee_user_id, task.created_by_user_id, task.deadline,
            task.awaiting_review_at, task.proposed_deadline, task.proposed_deadline_by,
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

    async def get_with_creator(self, task_id: UUID) -> Optional[Tuple[Task, Optional[dict]]]:
        """
        Get task by id with creator's role info (is_admin, is_head).
        Returns (task, creator_dict_or_None). creator is None if created_by_user_id is NULL.
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
        task = self._row_to_task(row)
        creator = None
        if row["creator_id"]:
            creator = {
                "id": row["creator_id"],
                "name": row["creator_name"],
                "is_admin": row["creator_is_admin"],
                "is_head": row["creator_is_head"],
            }
        return task, creator

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

    async def list_by_assignee_with_priority(
        self,
        assignee_user_id: UUID,
        include_done: bool = False,
    ) -> List[dict]:
        """
        Tasks where user is assignee, sorted by priority (creator role) then deadline.
        Returns list of dicts {task, creator} for service to compute priority.
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
        Returns list of dicts {task, assignee, creator}.
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
                CASE
                    WHEN u.is_admin THEN 3
                    WHEN u.is_head THEN 2
                    ELSE 1
                END DESC,
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
            return_entry = entry
            result.append(return_entry)
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
                updated_at = $10
            WHERE id = $1 AND is_active = true
            RETURNING *
        """
        row = await self.fetchrow(
            query,
            task.id, task.title, task.description, task.status,
            task.assignee_user_id, task.deadline,
            task.awaiting_review_at, task.proposed_deadline, task.proposed_deadline_by,
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
            created_by_user_id=row.get("created_by_user_id"),
            deadline=row["deadline"],
            awaiting_review_at=row.get("awaiting_review_at"),
            proposed_deadline=row.get("proposed_deadline"),
            proposed_deadline_by=row.get("proposed_deadline_by"),
            is_active=row["is_active"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    def _row_with_creator(self, row) -> dict:
        """Convert row from JOIN-with-creator query to dict {task, creator}"""
        task = self._row_to_task(row)
        creator = None
        if row.get("creator_id"):
            creator = {
                "id": row["creator_id"],
                "name": row["creator_name"],
                "is_admin": row["creator_is_admin"],
                "is_head": row["creator_is_head"],
            }
        return {"task": task, "creator": creator}
```

- [ ] **Step 2: Импорт-проверка**

Run: `cd /root/rugpt && venv/bin/python -c "from src.engine.storage.task_storage import TaskStorage; print('ok')"`
Expected: `ok` без ошибок.

---

## Phase 2 — Engine: TaskService новые методы

### Task 4: Тесты для приоритета и переходов статуса

**Files:**
- Create: `/root/rugpt/tests/test_task_ownership.py`

- [ ] **Step 1: Написать failing-тесты**

Создать файл с восемью тестами:

```python
"""
Tests for TaskService ownership and prioritization (item 9).

Mocks task_storage and in_app_notification_service. Tests all branches:
- priority computation by creator role
- create sets created_by_user_id
- assignee can take and mark-done
- only creator can accept/reject
- deadline negotiation flow
- legacy task without creator
"""
import asyncio
import pytest
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4, UUID

from src.engine.models.task import Task
from src.engine.models.user import User
from src.engine.services.task_service import TaskService, compute_priority


def make_user(user_id=None, is_admin=False, is_head=False, name="Test"):
    return User(
        id=user_id or uuid4(),
        org_id=uuid4(),
        name=name,
        username=name.lower(),
        email=f"{name.lower()}@test.com",
        is_admin=is_admin,
        is_head=is_head,
    )


def make_task(
    task_id=None, status="created", assignee_id=None, creator_id=None,
    awaiting_review_at=None, proposed_deadline=None, proposed_by=None,
):
    return Task(
        id=task_id or uuid4(),
        org_id=uuid4(),
        title="Test task",
        status=status,
        assignee_user_id=assignee_id or uuid4(),
        created_by_user_id=creator_id,
        awaiting_review_at=awaiting_review_at,
        proposed_deadline=proposed_deadline,
        proposed_deadline_by=proposed_by,
    )


def make_service():
    storage = AsyncMock()
    notif = AsyncMock()
    return TaskService(storage, notif), storage, notif


# === Priority computation ===

def test_priority_admin():
    creator = {"is_admin": True, "is_head": False}
    assert compute_priority(creator) == 3

def test_priority_head():
    creator = {"is_admin": False, "is_head": True}
    assert compute_priority(creator) == 2

def test_priority_regular():
    creator = {"is_admin": False, "is_head": False}
    assert compute_priority(creator) == 1

def test_priority_legacy_none():
    assert compute_priority(None) == 1


# === Create task sets created_by_user_id ===

def test_create_sets_created_by():
    async def go():
        svc, storage, notif = make_service()
        creator = make_user()
        assignee = make_user()
        storage.create = AsyncMock(side_effect=lambda t: t)

        task = await svc.create(
            org_id=creator.org_id,
            title="Hello",
            assignee_user_id=assignee.id,
            created_by_user_id=creator.id,
        )

        assert task.created_by_user_id == creator.id
        assert task.assignee_user_id == assignee.id
        storage.create.assert_called_once()

    asyncio.run(go())


# === Status transitions: assignee actions ===

def test_take_assignee_only():
    async def go():
        svc, storage, notif = make_service()
        assignee = make_user()
        creator = make_user()
        task = make_task(status="created", assignee_id=assignee.id, creator_id=creator.id)
        storage.get_by_id = AsyncMock(return_value=task)
        storage.update = AsyncMock(side_effect=lambda t: t)

        result = await svc.take_task(task.id, assignee)
        assert result.status == "in_progress"

        # Wrong user can't take
        other = make_user()
        with pytest.raises(PermissionError):
            await svc.take_task(task.id, other)

    asyncio.run(go())


def test_mark_done_sets_awaiting_review():
    async def go():
        svc, storage, notif = make_service()
        assignee = make_user()
        creator = make_user()
        task = make_task(status="in_progress", assignee_id=assignee.id, creator_id=creator.id)
        storage.get_by_id = AsyncMock(return_value=task)
        storage.update = AsyncMock(side_effect=lambda t: t)

        result = await svc.mark_done(task.id, assignee)
        assert result.status == "awaiting_review"
        assert result.awaiting_review_at is not None

    asyncio.run(go())


def test_assignee_cannot_set_done():
    """Assignee cannot accept their own work — only creator can."""
    async def go():
        svc, storage, notif = make_service()
        assignee = make_user()
        creator = make_user()
        task = make_task(status="awaiting_review", assignee_id=assignee.id, creator_id=creator.id)
        storage.get_by_id = AsyncMock(return_value=task)

        with pytest.raises(PermissionError):
            await svc.accept_task(task.id, assignee)

    asyncio.run(go())


# === Status transitions: creator actions ===

def test_creator_accepts_awaiting_review():
    async def go():
        svc, storage, notif = make_service()
        assignee = make_user()
        creator = make_user()
        task = make_task(status="awaiting_review", assignee_id=assignee.id, creator_id=creator.id)
        storage.get_by_id = AsyncMock(return_value=task)
        storage.update = AsyncMock(side_effect=lambda t: t)

        result = await svc.accept_task(task.id, creator)
        assert result.status == "done"

    asyncio.run(go())


def test_creator_rejects_returns_to_in_progress():
    async def go():
        svc, storage, notif = make_service()
        assignee = make_user()
        creator = make_user()
        task = make_task(status="awaiting_review", assignee_id=assignee.id, creator_id=creator.id)
        storage.get_by_id = AsyncMock(return_value=task)
        storage.update = AsyncMock(side_effect=lambda t: t)

        result = await svc.reject_task(task.id, creator, comment="Не хватает раздела X")
        assert result.status == "in_progress"
        assert result.awaiting_review_at is None

    asyncio.run(go())


# === Deadline negotiation ===

def test_deadline_negotiation_flow():
    async def go():
        svc, storage, notif = make_service()
        assignee = make_user()
        creator = make_user()
        original_deadline = datetime(2026, 4, 20, 18, 0)
        proposed = datetime(2026, 4, 25, 18, 0)
        task = make_task(status="in_progress", assignee_id=assignee.id, creator_id=creator.id)
        task.deadline = original_deadline

        storage.get_by_id = AsyncMock(return_value=task)
        storage.update = AsyncMock(side_effect=lambda t: t)

        # Assignee proposes
        result = await svc.propose_deadline(task.id, assignee, proposed)
        assert result.proposed_deadline == proposed
        assert result.proposed_deadline_by == assignee.id

        # Creator accepts
        result2 = await svc.accept_proposed_deadline(task.id, creator)
        assert result2.deadline == proposed
        assert result2.proposed_deadline is None
        assert result2.proposed_deadline_by is None

    asyncio.run(go())


def test_assignee_cannot_set_deadline_directly():
    async def go():
        svc, storage, notif = make_service()
        assignee = make_user()
        creator = make_user()
        task = make_task(assignee_id=assignee.id, creator_id=creator.id)
        storage.get_by_id = AsyncMock(return_value=task)

        with pytest.raises(PermissionError):
            await svc.set_deadline(task.id, assignee, datetime.utcnow() + timedelta(days=7))

    asyncio.run(go())


# === Legacy task without creator ===

def test_legacy_task_no_creator_priority_one():
    """Legacy task without creator gets priority 1."""
    assert compute_priority(None) == 1


def test_legacy_task_only_admin_can_manage():
    """Legacy tasks (created_by NULL) can only be managed by admin."""
    async def go():
        svc, storage, notif = make_service()
        admin = make_user(is_admin=True)
        regular = make_user()
        task = make_task(status="awaiting_review", creator_id=None)
        storage.get_by_id = AsyncMock(return_value=task)
        storage.update = AsyncMock(side_effect=lambda t: t)

        # Regular user cannot accept
        with pytest.raises(PermissionError):
            await svc.accept_task(task.id, regular)

        # Admin can accept
        result = await svc.accept_task(task.id, admin)
        assert result.status == "done"

    asyncio.run(go())
```

- [ ] **Step 2: Запустить тесты — должны упасть на отсутствующих методах**

Run: `cd /root/rugpt && venv/bin/python -m pytest tests/test_task_ownership.py -v 2>&1 | tail -30`
Expected: тесты падают с `ImportError` на `compute_priority` или `AttributeError` на методах `take_task`/`mark_done`/`accept_task`/`reject_task`/`propose_deadline`/`accept_proposed_deadline`/`set_deadline`. Это нормально — методов ещё нет.

---

### Task 5: Реализация TaskService — компорарность и переходы

**Files:**
- Modify: `/root/rugpt/src/engine/services/task_service.py`

- [ ] **Step 1: Полностью переписать task_service.py**

```python
"""
Task Service

Business logic for employee task management.
Creates in-app notifications on task events.
"""
import logging
from datetime import datetime
from typing import Optional, List
from uuid import UUID

from ..models.task import Task, VALID_STATUSES
from ..models.user import User
from ..storage.task_storage import TaskStorage
from .in_app_notification_service import InAppNotificationService

logger = logging.getLogger("rugpt.services.task")


def compute_priority(creator: Optional[dict]) -> int:
    """
    Priority by task creator's role:
    3 = admin (org owner)
    2 = head (department head)
    1 = regular user (also default for legacy tasks without creator)
    """
    if creator is None:
        return 1
    if creator.get("is_admin"):
        return 3
    if creator.get("is_head"):
        return 2
    return 1


class TaskService:

    def __init__(
        self,
        storage: TaskStorage,
        in_app_notification_service: InAppNotificationService,
    ):
        self.storage = storage
        self.notification_service = in_app_notification_service

    async def create(
        self,
        org_id: UUID,
        title: str,
        assignee_user_id: UUID,
        description: Optional[str] = None,
        deadline: Optional[datetime] = None,
        created_by_user_id: Optional[UUID] = None,
    ) -> Task:
        """Create a new task and notify the assignee"""
        if not title:
            raise ValueError("Task title is required")

        task = Task(
            org_id=org_id,
            title=title,
            description=description,
            assignee_user_id=assignee_user_id,
            created_by_user_id=created_by_user_id,
            deadline=deadline,
        )
        created = await self.storage.create(task)
        logger.info(
            f"Created task '{title}' for user {assignee_user_id} in org {org_id} "
            f"by {created_by_user_id}"
        )

        # Notify assignee
        await self.notification_service.create(
            user_id=assignee_user_id,
            org_id=org_id,
            type="new_task",
            title=f"Новая задача: {title}",
            content=description,
            reference_type="task",
            reference_id=created.id,
        )

        return created

    async def get(self, task_id: UUID) -> Optional[Task]:
        """Get task by ID"""
        return await self.storage.get_by_id(task_id)

    async def list_by_assignee(
        self,
        assignee_user_id: UUID,
        status: Optional[str] = None,
    ) -> List[Task]:
        """List tasks assigned to a user (legacy method, used by scheduler)"""
        if status and status not in VALID_STATUSES:
            raise ValueError(f"Invalid status: {status}. Must be one of {VALID_STATUSES}")
        return await self.storage.list_by_assignee(assignee_user_id, status)

    async def list_by_org(
        self,
        org_id: UUID,
        status: Optional[str] = None,
    ) -> List[Task]:
        """List all tasks in an organization (for managers)"""
        if status and status not in VALID_STATUSES:
            raise ValueError(f"Invalid status: {status}. Must be one of {VALID_STATUSES}")
        return await self.storage.list_by_org(org_id, status)

    async def list_my_tasks(
        self,
        user_id: UUID,
        org_id: UUID,
        include_done: bool = False,
    ) -> List[dict]:
        """
        Tasks where user is assignee. Sorted by priority (creator role) + deadline.
        Returns list of dicts with `task`, `creator`, `priority` keys.
        """
        rows = await self.storage.list_by_assignee_with_priority(user_id, include_done)
        result = []
        for entry in rows:
            entry["priority"] = compute_priority(entry["creator"])
            result.append(entry)
        return result

    async def list_tasks_created_by(
        self,
        user_id: UUID,
        org_id: UUID,
        include_done: bool = False,
    ) -> List[dict]:
        """
        Tasks where user is creator. Includes assignee info.
        Returns list of dicts with `task`, `creator`, `assignee`, `priority` keys.
        """
        rows = await self.storage.list_by_creator_with_assignee(user_id, include_done)
        for entry in rows:
            entry["priority"] = compute_priority(entry["creator"])
        return rows

    async def update_status(
        self,
        task_id: UUID,
        new_status: str,
    ) -> Optional[Task]:
        """Update task status (legacy, used by scheduler for overdue)"""
        if new_status not in VALID_STATUSES:
            raise ValueError(f"Invalid status: {new_status}. Must be one of {VALID_STATUSES}")

        task = await self.storage.get_by_id(task_id)
        if not task:
            return None

        old_status = task.status
        task.status = new_status
        updated = await self.storage.update(task)
        logger.info(f"Task {task_id} status: {old_status} -> {new_status}")

        return updated

    async def update(
        self,
        task_id: UUID,
        title: Optional[str] = None,
        description: Optional[str] = None,
        assignee_user_id: Optional[UUID] = None,
        deadline: Optional[datetime] = None,
    ) -> Optional[Task]:
        """Update task fields (title/description/assignee/deadline). Status changes go through dedicated methods."""
        task = await self.storage.get_by_id(task_id)
        if not task:
            return None

        if title is not None:
            task.title = title
        if description is not None:
            task.description = description
        if assignee_user_id is not None:
            task.assignee_user_id = assignee_user_id
        if deadline is not None:
            task.deadline = deadline

        return await self.storage.update(task)

    async def deactivate(self, task_id: UUID) -> bool:
        """Soft-delete a task"""
        return await self.storage.deactivate(task_id)

    # ============================================
    # Status transitions
    # ============================================

    def _check_assignee(self, task: Task, user: User):
        if task.assignee_user_id != user.id:
            raise PermissionError("Only assignee can perform this action")

    def _check_creator(self, task: Task, user: User):
        if task.created_by_user_id is not None:
            if task.created_by_user_id != user.id and not user.is_admin:
                raise PermissionError("Only task creator can perform this action")
        else:
            # Legacy task: only admin can manage
            if not user.is_admin:
                raise PermissionError("Only admin can manage legacy tasks without creator")

    async def take_task(self, task_id: UUID, user: User) -> Task:
        """Assignee takes task: created → in_progress."""
        task = await self.storage.get_by_id(task_id)
        if not task:
            raise ValueError(f"Task {task_id} not found")
        self._check_assignee(task, user)
        if task.status != "created":
            raise ValueError(f"Cannot take task in status '{task.status}'")
        task.status = "in_progress"
        return await self.storage.update(task)

    async def mark_done(self, task_id: UUID, user: User) -> Task:
        """Assignee marks task done: in_progress → awaiting_review."""
        task = await self.storage.get_by_id(task_id)
        if not task:
            raise ValueError(f"Task {task_id} not found")
        self._check_assignee(task, user)
        if task.status != "in_progress":
            raise ValueError(f"Cannot mark done from status '{task.status}'")
        task.status = "awaiting_review"
        task.awaiting_review_at = datetime.utcnow()
        return await self.storage.update(task)

    async def accept_task(self, task_id: UUID, user: User) -> Task:
        """Creator accepts: awaiting_review → done."""
        task = await self.storage.get_by_id(task_id)
        if not task:
            raise ValueError(f"Task {task_id} not found")
        self._check_creator(task, user)
        if task.status != "awaiting_review":
            raise ValueError(f"Cannot accept from status '{task.status}'")
        task.status = "done"
        return await self.storage.update(task)

    async def reject_task(
        self, task_id: UUID, user: User, comment: Optional[str] = None,
    ) -> Task:
        """Creator rejects: awaiting_review → in_progress."""
        task = await self.storage.get_by_id(task_id)
        if not task:
            raise ValueError(f"Task {task_id} not found")
        self._check_creator(task, user)
        if task.status != "awaiting_review":
            raise ValueError(f"Cannot reject from status '{task.status}'")
        task.status = "in_progress"
        task.awaiting_review_at = None
        if comment:
            logger.info(f"Task {task_id} rejected by {user.id} with comment: {comment}")
        return await self.storage.update(task)

    # ============================================
    # Deadline negotiation
    # ============================================

    async def set_deadline(
        self, task_id: UUID, user: User, deadline: datetime,
    ) -> Task:
        """Creator changes deadline directly."""
        task = await self.storage.get_by_id(task_id)
        if not task:
            raise ValueError(f"Task {task_id} not found")
        self._check_creator(task, user)
        task.deadline = deadline
        # Clear any pending proposal
        task.proposed_deadline = None
        task.proposed_deadline_by = None
        return await self.storage.update(task)

    async def propose_deadline(
        self, task_id: UUID, user: User, proposed: datetime,
    ) -> Task:
        """Assignee proposes alternative deadline."""
        task = await self.storage.get_by_id(task_id)
        if not task:
            raise ValueError(f"Task {task_id} not found")
        self._check_assignee(task, user)
        task.proposed_deadline = proposed
        task.proposed_deadline_by = user.id
        return await self.storage.update(task)

    async def accept_proposed_deadline(self, task_id: UUID, user: User) -> Task:
        """Creator accepts assignee's deadline proposal."""
        task = await self.storage.get_by_id(task_id)
        if not task:
            raise ValueError(f"Task {task_id} not found")
        self._check_creator(task, user)
        if task.proposed_deadline is None:
            raise ValueError("No pending deadline proposal")
        task.deadline = task.proposed_deadline
        task.proposed_deadline = None
        task.proposed_deadline_by = None
        return await self.storage.update(task)

    async def reject_proposed_deadline(self, task_id: UUID, user: User) -> Task:
        """Creator rejects assignee's deadline proposal."""
        task = await self.storage.get_by_id(task_id)
        if not task:
            raise ValueError(f"Task {task_id} not found")
        self._check_creator(task, user)
        if task.proposed_deadline is None:
            raise ValueError("No pending deadline proposal")
        task.proposed_deadline = None
        task.proposed_deadline_by = None
        return await self.storage.update(task)

    # ============================================
    # Scheduler
    # ============================================

    async def check_overdue(self) -> List[Task]:
        """Check for overdue tasks and update their status. Called by scheduler."""
        now = datetime.utcnow()
        tasks = await self.storage.list_active_with_deadline()
        overdue_tasks = []

        for task in tasks:
            if task.deadline and task.deadline <= now:
                task.status = "overdue"
                await self.storage.update(task)
                overdue_tasks.append(task)
                logger.info(f"Task {task.id} marked as overdue: '{task.title}'")

                # Notify assignee
                await self.notification_service.create(
                    user_id=task.assignee_user_id,
                    org_id=task.org_id,
                    type="task_status_change",
                    title=f"Задача просрочена: {task.title}",
                    reference_type="task",
                    reference_id=task.id,
                )

        return overdue_tasks
```

- [ ] **Step 2: Запустить тесты — должны проходить**

Run: `cd /root/rugpt && venv/bin/python -m pytest tests/test_task_ownership.py -v 2>&1 | tail -40`
Expected: все 12 тестов проходят (4 priority + 1 create + 2 take/mark_done + 1 cannot_set_done + 2 creator_accept/reject + 2 deadline + 2 legacy).

---

## Phase 3 — Engine: Routes

### Task 6: Обновление существующих + новые endpoints в routes/tasks.py

**Files:**
- Modify: `/root/rugpt/src/engine/routes/tasks.py`

- [ ] **Step 1: Полностью переписать routes/tasks.py**

```python
"""
Task Management Routes

CRUD endpoints for employee tasks.
Item 9: ownership, prioritization, status transitions, deadline negotiation.
"""
import logging
from datetime import datetime
from typing import Optional, List
from uuid import UUID

from fastapi import APIRouter, HTTPException, Depends, Query
from pydantic import BaseModel

from ..services.engine_service import get_engine_service
from .auth import get_current_user

logger = logging.getLogger("rugpt.routes.tasks")
router = APIRouter(prefix="/tasks", tags=["tasks"])


class CreateTaskRequest(BaseModel):
    title: str
    description: Optional[str] = None
    assignee_user_id: str
    deadline: Optional[str] = None  # ISO 8601


class UpdateTaskRequest(BaseModel):
    title: Optional[str] = None
    description: Optional[str] = None
    assignee_user_id: Optional[str] = None
    deadline: Optional[str] = None


class DeadlineRequest(BaseModel):
    deadline: str  # ISO 8601


class ProposeDeadlineRequest(BaseModel):
    proposed_deadline: str  # ISO 8601


class RejectRequest(BaseModel):
    comment: Optional[str] = None


def _serialize_entry(entry: dict) -> dict:
    """Serialize a {task, creator?, assignee?, priority} entry to dict for API response."""
    out = entry["task"].to_dict()
    out["priority"] = entry.get("priority", 1)
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
    return out


async def _load_user(engine, user_id: UUID):
    """Helper: load user object from storage (for permission checks)."""
    return await engine.user_storage.get_by_id(user_id)


# ============================================
# Lists
# ============================================

@router.get("")
async def list_tasks(
    status: Optional[str] = Query(None),
    assignee_user_id: Optional[str] = Query(None),
    current_user: dict = Depends(get_current_user),
):
    """
    Legacy general list. Admins see all org tasks, employees see only their own.
    Use /tasks/my and /tasks/created-by-me for prioritized lists.
    """
    engine = get_engine_service()
    org_id = current_user["org_id"]

    try:
        if assignee_user_id:
            assignee_uuid = UUID(assignee_user_id)
            if not current_user.get("is_admin") and assignee_uuid != current_user["user_id"]:
                raise HTTPException(status_code=403, detail="Access denied")
            tasks = await engine.task_service.list_by_assignee(assignee_uuid, status)
        elif current_user.get("is_admin"):
            tasks = await engine.task_service.list_by_org(org_id, status)
        else:
            tasks = await engine.task_service.list_by_assignee(current_user["user_id"], status)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    if current_user.get("is_head") and not current_user.get("is_admin"):
        visible_ids = await engine.department_service.get_visible_user_ids(
            current_user["user_id"], current_user["org_id"],
        )
        tasks = [t for t in tasks if t.assignee_user_id in visible_ids]

    return [t.to_dict() for t in tasks]


@router.get("/my")
async def list_my_tasks(
    include_done: bool = Query(False),
    current_user: dict = Depends(get_current_user),
):
    """Tasks where current user is assignee. Sorted by priority + deadline."""
    engine = get_engine_service()
    entries = await engine.task_service.list_my_tasks(
        current_user["user_id"], current_user["org_id"], include_done,
    )
    return [_serialize_entry(e) for e in entries]


@router.get("/created-by-me")
async def list_created_by_me(
    include_done: bool = Query(False),
    current_user: dict = Depends(get_current_user),
):
    """Tasks where current user is creator."""
    engine = get_engine_service()
    entries = await engine.task_service.list_tasks_created_by(
        current_user["user_id"], current_user["org_id"], include_done,
    )
    return [_serialize_entry(e) for e in entries]


# ============================================
# Create / Get / Update / Delete
# ============================================

@router.post("")
async def create_task(
    request: CreateTaskRequest,
    current_user: dict = Depends(get_current_user),
):
    """Create a new task. created_by_user_id auto-set to current user."""
    engine = get_engine_service()

    try:
        assignee_uuid = UUID(request.assignee_user_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid assignee_user_id")

    if not await engine.department_service.check_visible(
        current_user["user_id"], assignee_uuid, current_user["org_id"],
    ):
        raise HTTPException(status_code=403, detail="Assignee not visible")

    deadline = None
    if request.deadline:
        try:
            deadline = datetime.fromisoformat(request.deadline)
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid deadline format (use ISO 8601)")

    try:
        task = await engine.task_service.create(
            org_id=current_user["org_id"],
            title=request.title,
            description=request.description,
            assignee_user_id=assignee_uuid,
            deadline=deadline,
            created_by_user_id=current_user["user_id"],
        )
        return task.to_dict()
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.get("/{task_id}")
async def get_task(task_id: str, current_user: dict = Depends(get_current_user)):
    """Get a single task by ID"""
    engine = get_engine_service()
    try:
        task_uuid = UUID(task_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid task ID")

    task = await engine.task_service.get(task_uuid)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    if task.org_id != current_user["org_id"]:
        raise HTTPException(status_code=403, detail="Access denied")
    if not current_user.get("is_admin"):
        if task.assignee_user_id != current_user["user_id"] and task.created_by_user_id != current_user["user_id"]:
            raise HTTPException(status_code=403, detail="Access denied")

    return task.to_dict()


@router.patch("/{task_id}")
async def update_task(
    task_id: str,
    request: UpdateTaskRequest,
    current_user: dict = Depends(get_current_user),
):
    """Update task fields (title/description/assignee/deadline). Status changes go through /take, /mark-done, /accept, /reject."""
    engine = get_engine_service()
    try:
        task_uuid = UUID(task_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid task ID")

    task = await engine.task_service.get(task_uuid)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    if task.org_id != current_user["org_id"]:
        raise HTTPException(status_code=403, detail="Access denied")

    # Only creator can update non-status fields (or admin)
    if task.created_by_user_id is not None:
        if task.created_by_user_id != current_user["user_id"] and not current_user.get("is_admin"):
            raise HTTPException(status_code=403, detail="Only creator can update task")
    elif not current_user.get("is_admin"):
        raise HTTPException(status_code=403, detail="Only admin can update legacy task")

    assignee_uuid = None
    if request.assignee_user_id:
        try:
            assignee_uuid = UUID(request.assignee_user_id)
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid assignee_user_id")
        if not await engine.department_service.check_visible(
            current_user["user_id"], assignee_uuid, current_user["org_id"],
        ):
            raise HTTPException(status_code=403, detail="Assignee not visible")

    deadline = None
    if request.deadline:
        try:
            deadline = datetime.fromisoformat(request.deadline)
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid deadline format")

    try:
        updated = await engine.task_service.update(
            task_id=task_uuid,
            title=request.title,
            description=request.description,
            assignee_user_id=assignee_uuid,
            deadline=deadline,
        )
        return updated.to_dict()
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.delete("/{task_id}")
async def deactivate_task(task_id: str, current_user: dict = Depends(get_current_user)):
    """Soft-delete a task (only creator or admin)"""
    engine = get_engine_service()
    try:
        task_uuid = UUID(task_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid task ID")

    task = await engine.task_service.get(task_uuid)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    if task.org_id != current_user["org_id"]:
        raise HTTPException(status_code=403, detail="Access denied")
    if task.created_by_user_id is not None and task.created_by_user_id != current_user["user_id"] and not current_user.get("is_admin"):
        raise HTTPException(status_code=403, detail="Only creator can delete task")

    await engine.task_service.deactivate(task_uuid)
    return {"success": True, "message": "Task deactivated"}


# ============================================
# Status transitions
# ============================================

@router.post("/{task_id}/take")
async def take_task(task_id: str, current_user: dict = Depends(get_current_user)):
    """Assignee takes task: created → in_progress."""
    engine = get_engine_service()
    try:
        task_uuid = UUID(task_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid task ID")

    user = await _load_user(engine, current_user["user_id"])
    if not user:
        raise HTTPException(status_code=401, detail="User not found")

    try:
        task = await engine.task_service.take_task(task_uuid, user)
        return task.to_dict()
    except PermissionError as e:
        raise HTTPException(status_code=403, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/{task_id}/mark-done")
async def mark_done(task_id: str, current_user: dict = Depends(get_current_user)):
    """Assignee marks task done: in_progress → awaiting_review."""
    engine = get_engine_service()
    try:
        task_uuid = UUID(task_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid task ID")

    user = await _load_user(engine, current_user["user_id"])
    if not user:
        raise HTTPException(status_code=401, detail="User not found")

    try:
        task = await engine.task_service.mark_done(task_uuid, user)
        return task.to_dict()
    except PermissionError as e:
        raise HTTPException(status_code=403, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/{task_id}/accept")
async def accept_task(task_id: str, current_user: dict = Depends(get_current_user)):
    """Creator accepts: awaiting_review → done."""
    engine = get_engine_service()
    try:
        task_uuid = UUID(task_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid task ID")

    user = await _load_user(engine, current_user["user_id"])
    if not user:
        raise HTTPException(status_code=401, detail="User not found")

    try:
        task = await engine.task_service.accept_task(task_uuid, user)
        return task.to_dict()
    except PermissionError as e:
        raise HTTPException(status_code=403, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/{task_id}/reject")
async def reject_task(
    task_id: str,
    request: RejectRequest,
    current_user: dict = Depends(get_current_user),
):
    """Creator rejects: awaiting_review → in_progress. Comment optional, logged only."""
    engine = get_engine_service()
    try:
        task_uuid = UUID(task_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid task ID")

    user = await _load_user(engine, current_user["user_id"])
    if not user:
        raise HTTPException(status_code=401, detail="User not found")

    try:
        task = await engine.task_service.reject_task(task_uuid, user, comment=request.comment)
        return task.to_dict()
    except PermissionError as e:
        raise HTTPException(status_code=403, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


# ============================================
# Deadline
# ============================================

@router.patch("/{task_id}/deadline")
async def set_deadline(
    task_id: str,
    request: DeadlineRequest,
    current_user: dict = Depends(get_current_user),
):
    """Creator sets deadline directly."""
    engine = get_engine_service()
    try:
        task_uuid = UUID(task_id)
        deadline = datetime.fromisoformat(request.deadline)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid task ID or deadline format")

    user = await _load_user(engine, current_user["user_id"])
    if not user:
        raise HTTPException(status_code=401, detail="User not found")

    try:
        task = await engine.task_service.set_deadline(task_uuid, user, deadline)
        return task.to_dict()
    except PermissionError as e:
        raise HTTPException(status_code=403, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/{task_id}/deadline-proposal")
async def propose_deadline(
    task_id: str,
    request: ProposeDeadlineRequest,
    current_user: dict = Depends(get_current_user),
):
    """Assignee proposes alternative deadline."""
    engine = get_engine_service()
    try:
        task_uuid = UUID(task_id)
        proposed = datetime.fromisoformat(request.proposed_deadline)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid task ID or deadline format")

    user = await _load_user(engine, current_user["user_id"])
    if not user:
        raise HTTPException(status_code=401, detail="User not found")

    try:
        task = await engine.task_service.propose_deadline(task_uuid, user, proposed)
        return task.to_dict()
    except PermissionError as e:
        raise HTTPException(status_code=403, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/{task_id}/deadline-proposal/accept")
async def accept_proposed_deadline(task_id: str, current_user: dict = Depends(get_current_user)):
    """Creator accepts proposed deadline."""
    engine = get_engine_service()
    try:
        task_uuid = UUID(task_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid task ID")

    user = await _load_user(engine, current_user["user_id"])
    if not user:
        raise HTTPException(status_code=401, detail="User not found")

    try:
        task = await engine.task_service.accept_proposed_deadline(task_uuid, user)
        return task.to_dict()
    except PermissionError as e:
        raise HTTPException(status_code=403, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/{task_id}/deadline-proposal/reject")
async def reject_proposed_deadline(task_id: str, current_user: dict = Depends(get_current_user)):
    """Creator rejects proposed deadline."""
    engine = get_engine_service()
    try:
        task_uuid = UUID(task_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid task ID")

    user = await _load_user(engine, current_user["user_id"])
    if not user:
        raise HTTPException(status_code=401, detail="User not found")

    try:
        task = await engine.task_service.reject_proposed_deadline(task_uuid, user)
        return task.to_dict()
    except PermissionError as e:
        raise HTTPException(status_code=403, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
```

- [ ] **Step 2: Импорт-проверка**

Run: `cd /root/rugpt && venv/bin/python -c "from src.engine.routes.tasks import router; print(len(router.routes), 'routes')"`
Expected: число роутов >= 13 (примерно: list, my, created-by-me, post, get, patch, delete, take, mark-done, accept, reject, set deadline, propose, accept-proposal, reject-proposal).

- [ ] **Step 3: Запустить engine локально и проверить, что app загружается**

Run: `cd /root/rugpt && venv/bin/python -c "from src.engine.app import app; print('app loaded, routes:', len(app.routes))"`
Expected: app загружается без ошибок.

---

## Phase 4 — Engine: AI tools

### Task 7: Обновление task_create — прокидывать created_by_user_id

**Files:**
- Modify: `/root/rugpt/src/engine/agents/tools/task_tool.py`

- [ ] **Step 1: Найти вызовы task_service.create в _task_create**

Run: `grep -n "task_service.create" /root/rugpt/src/engine/agents/tools/task_tool.py`
Expected: показать строки с `task_service.create(` в `_task_create` функции.

- [ ] **Step 2: Добавить created_by_user_id в оба вызова**

Найти блок (сейчас примерно строки 113-122 и 126-134):

```python
                        lambda: asyncio.run(
                            task_service.create(
                                org_id=default_org_id or UUID('00000000-0000-0000-0000-000000000000'),
                                title=title,
                                description=description or None,
                                assignee_user_id=assignee_uuid,
                                deadline=dl,
                            )
```

Заменить на:

```python
                        lambda: asyncio.run(
                            task_service.create(
                                org_id=default_org_id or UUID('00000000-0000-0000-0000-000000000000'),
                                title=title,
                                description=description or None,
                                assignee_user_id=assignee_uuid,
                                deadline=dl,
                                created_by_user_id=UUID(user_id) if user_id else None,
                            )
```

И аналогично второй вызов (без `lambda: asyncio.run(...)` обертки):

```python
                task = loop.run_until_complete(
                    task_service.create(
                        org_id=default_org_id or UUID('00000000-0000-0000-0000-000000000000'),
                        title=title,
                        description=description or None,
                        assignee_user_id=assignee_uuid,
                        deadline=dl,
                        created_by_user_id=UUID(user_id) if user_id else None,
                    )
                )
```

- [ ] **Step 3: Импорт-проверка**

Run: `cd /root/rugpt && venv/bin/python -c "from src.engine.agents.tools.task_tool import create_task_tools; print('ok')"`
Expected: `ok` без ошибок.

---

## Phase 5 — Engine: интеграционная проверка

### Task 8: Запуск Engine и smoke-тест endpoints

**Files:**
- Нет изменений, только проверки

- [ ] **Step 1: Запустить тесты ещё раз**

Run: `cd /root/rugpt && venv/bin/python -m pytest tests/test_task_ownership.py tests/test_department_visibility.py -v 2>&1 | tail -30`
Expected: все тесты проходят, без регрессий в существующих тестах департаментов.

- [ ] **Step 2: Стартовать Engine локально**

Run: `cd /root/rugpt && source venv/bin/activate && nohup uvicorn src.engine.app:app --host 127.0.0.1 --port 8100 > /tmp/engine.log 2>&1 &`
Expected: процесс запускается. Проверить `tail /tmp/engine.log` — должно быть `Application startup complete.`

- [ ] **Step 3: Smoke-test через curl**

Run: `curl -s http://127.0.0.1:8100/health`
Expected: JSON с `{"status": "ok"}` или похожим.

Run: `curl -s http://127.0.0.1:8100/openapi.json | python -m json.tool | grep -E '"/api/v1/tasks/(my|created-by-me|.*\\{task_id\\}/(take|mark-done|accept|reject|deadline))"' | head -20`
Expected: вывод содержит новые роуты (`/tasks/my`, `/tasks/created-by-me`, `/tasks/{task_id}/take`, etc).

- [ ] **Step 4: Остановить Engine**

Run: `pkill -f "uvicorn src.engine.app" || true`
Expected: процесс останавливается.

---

## Phase 6 — WebClient backend (NestJS proxy)

### Task 9: Новые команды в engine adapter

**Files:**
- Modify: `/root/webclient_rugpt/packages/backend/src/engine/adapters/rugpt.adapter.ts`

- [ ] **Step 1: Найти секцию tasks в adapter**

Run: `grep -n "case 'get_tasks'\|case 'create_task'\|case 'update_task'" /root/webclient_rugpt/packages/backend/src/engine/adapters/rugpt.adapter.ts`
Expected: вывод с номерами строк существующих task-команд.

- [ ] **Step 2: Добавить новые команды в switch**

После последнего существующего `case` для tasks (например, `case 'delete_task':`) добавить блок:

```typescript
      case 'list_my_tasks': {
        const inc = payload.include_done ? '?include_done=true' : '';
        return this.request('GET', `/api/v1/tasks/my${inc}`, null, headers);
      }

      case 'list_tasks_created_by_me': {
        const inc = payload.include_done ? '?include_done=true' : '';
        return this.request('GET', `/api/v1/tasks/created-by-me${inc}`, null, headers);
      }

      case 'take_task':
        return this.request('POST', `/api/v1/tasks/${payload.task_id}/take`, null, headers);

      case 'mark_task_done':
        return this.request('POST', `/api/v1/tasks/${payload.task_id}/mark-done`, null, headers);

      case 'accept_task':
        return this.request('POST', `/api/v1/tasks/${payload.task_id}/accept`, null, headers);

      case 'reject_task':
        return this.request(
          'POST',
          `/api/v1/tasks/${payload.task_id}/reject`,
          { comment: payload.comment || null },
          headers,
        );

      case 'set_task_deadline':
        return this.request(
          'PATCH',
          `/api/v1/tasks/${payload.task_id}/deadline`,
          { deadline: payload.deadline },
          headers,
        );

      case 'propose_task_deadline':
        return this.request(
          'POST',
          `/api/v1/tasks/${payload.task_id}/deadline-proposal`,
          { proposed_deadline: payload.proposed_deadline },
          headers,
        );

      case 'accept_proposed_deadline':
        return this.request(
          'POST',
          `/api/v1/tasks/${payload.task_id}/deadline-proposal/accept`,
          null,
          headers,
        );

      case 'reject_proposed_deadline':
        return this.request(
          'POST',
          `/api/v1/tasks/${payload.task_id}/deadline-proposal/reject`,
          null,
          headers,
        );
```

- [ ] **Step 3: Сборка**

Run: `cd /root/webclient_rugpt/packages/backend && npm run build 2>&1 | tail -10`
Expected: build success без TypeScript ошибок.

---

### Task 10: Расширение TaskService (NestJS)

**Files:**
- Modify: `/root/webclient_rugpt/packages/backend/src/task/task.service.ts`

- [ ] **Step 1: Прочитать текущий файл**

Run: `wc -l /root/webclient_rugpt/packages/backend/src/task/task.service.ts`
Expected: показывает количество строк (для понимания размера).

- [ ] **Step 2: Добавить новые методы в класс TaskService**

После последнего существующего метода в классе добавить:

```typescript
  async listMy(currentUser: any, includeDone = false): Promise<any[]> {
    const [ok, data] = await this.engineAdapter.execute('list_my_tasks', {
      include_done: includeDone,
      token: currentUser.engineToken,
    });
    if (!ok) return [];
    return data || [];
  }

  async listCreatedByMe(currentUser: any, includeDone = false): Promise<any[]> {
    const [ok, data] = await this.engineAdapter.execute('list_tasks_created_by_me', {
      include_done: includeDone,
      token: currentUser.engineToken,
    });
    if (!ok) return [];
    return data || [];
  }

  async take(taskId: string, currentUser: any): Promise<any> {
    const [ok, data] = await this.engineAdapter.execute('take_task', {
      task_id: taskId,
      token: currentUser.engineToken,
    });
    if (!ok) throw new Error(data || 'Failed to take task');
    return data;
  }

  async markDone(taskId: string, currentUser: any): Promise<any> {
    const [ok, data] = await this.engineAdapter.execute('mark_task_done', {
      task_id: taskId,
      token: currentUser.engineToken,
    });
    if (!ok) throw new Error(data || 'Failed to mark task done');
    return data;
  }

  async accept(taskId: string, currentUser: any): Promise<any> {
    const [ok, data] = await this.engineAdapter.execute('accept_task', {
      task_id: taskId,
      token: currentUser.engineToken,
    });
    if (!ok) throw new Error(data || 'Failed to accept task');
    return data;
  }

  async reject(taskId: string, comment: string | null, currentUser: any): Promise<any> {
    const [ok, data] = await this.engineAdapter.execute('reject_task', {
      task_id: taskId,
      comment,
      token: currentUser.engineToken,
    });
    if (!ok) throw new Error(data || 'Failed to reject task');
    return data;
  }

  async setDeadline(taskId: string, deadline: string, currentUser: any): Promise<any> {
    const [ok, data] = await this.engineAdapter.execute('set_task_deadline', {
      task_id: taskId,
      deadline,
      token: currentUser.engineToken,
    });
    if (!ok) throw new Error(data || 'Failed to set deadline');
    return data;
  }

  async proposeDeadline(taskId: string, proposedDeadline: string, currentUser: any): Promise<any> {
    const [ok, data] = await this.engineAdapter.execute('propose_task_deadline', {
      task_id: taskId,
      proposed_deadline: proposedDeadline,
      token: currentUser.engineToken,
    });
    if (!ok) throw new Error(data || 'Failed to propose deadline');
    return data;
  }

  async acceptProposedDeadline(taskId: string, currentUser: any): Promise<any> {
    const [ok, data] = await this.engineAdapter.execute('accept_proposed_deadline', {
      task_id: taskId,
      token: currentUser.engineToken,
    });
    if (!ok) throw new Error(data || 'Failed to accept proposed deadline');
    return data;
  }

  async rejectProposedDeadline(taskId: string, currentUser: any): Promise<any> {
    const [ok, data] = await this.engineAdapter.execute('reject_proposed_deadline', {
      task_id: taskId,
      token: currentUser.engineToken,
    });
    if (!ok) throw new Error(data || 'Failed to reject proposed deadline');
    return data;
  }
```

- [ ] **Step 3: Сборка**

Run: `cd /root/webclient_rugpt/packages/backend && npm run build 2>&1 | tail -10`
Expected: build success.

---

### Task 11: Новые роуты в TaskController

**Files:**
- Modify: `/root/webclient_rugpt/packages/backend/src/task/task.controller.ts`

- [ ] **Step 1: Прочитать текущий файл и найти последний @Method**

Run: `wc -l /root/webclient_rugpt/packages/backend/src/task/task.controller.ts && grep -n '@Get\|@Post\|@Patch\|@Delete' /root/webclient_rugpt/packages/backend/src/task/task.controller.ts`
Expected: показать структуру существующих роутов.

- [ ] **Step 2: Добавить новые методы в класс TaskController**

После последнего существующего метода (но перед закрывающей `}` класса) добавить:

```typescript
  @Get('my')
  @ApiOperation({ summary: 'List my tasks (where I am assignee)' })
  async listMy(
    @Query('include_done') includeDone: string,
    @Request() req: any,
  ) {
    return this.svc.listMy(req.user, includeDone === 'true');
  }

  @Get('created-by-me')
  @ApiOperation({ summary: 'List tasks created by me' })
  async listCreatedByMe(
    @Query('include_done') includeDone: string,
    @Request() req: any,
  ) {
    return this.svc.listCreatedByMe(req.user, includeDone === 'true');
  }

  @Post(':id/take')
  @ApiOperation({ summary: 'Take task in work (assignee)' })
  async take(@Param('id') id: string, @Request() req: any) {
    return this.svc.take(id, req.user);
  }

  @Post(':id/mark-done')
  @ApiOperation({ summary: 'Mark task done, awaiting review (assignee)' })
  async markDone(@Param('id') id: string, @Request() req: any) {
    return this.svc.markDone(id, req.user);
  }

  @Post(':id/accept')
  @ApiOperation({ summary: 'Accept completed task (creator only)' })
  async accept(@Param('id') id: string, @Request() req: any) {
    return this.svc.accept(id, req.user);
  }

  @Post(':id/reject')
  @ApiOperation({ summary: 'Reject task back to in_progress (creator only)' })
  async reject(
    @Param('id') id: string,
    @Body() body: { comment?: string },
    @Request() req: any,
  ) {
    return this.svc.reject(id, body.comment || null, req.user);
  }

  @Patch(':id/deadline')
  @ApiOperation({ summary: 'Set task deadline directly (creator only)' })
  async setDeadline(
    @Param('id') id: string,
    @Body() body: { deadline: string },
    @Request() req: any,
  ) {
    return this.svc.setDeadline(id, body.deadline, req.user);
  }

  @Post(':id/deadline-proposal')
  @ApiOperation({ summary: 'Propose new deadline (assignee only)' })
  async proposeDeadline(
    @Param('id') id: string,
    @Body() body: { proposedDeadline: string },
    @Request() req: any,
  ) {
    return this.svc.proposeDeadline(id, body.proposedDeadline, req.user);
  }

  @Post(':id/deadline-proposal/accept')
  @ApiOperation({ summary: 'Accept assignee deadline proposal (creator only)' })
  async acceptProposedDeadline(@Param('id') id: string, @Request() req: any) {
    return this.svc.acceptProposedDeadline(id, req.user);
  }

  @Post(':id/deadline-proposal/reject')
  @ApiOperation({ summary: 'Reject assignee deadline proposal (creator only)' })
  async rejectProposedDeadline(@Param('id') id: string, @Request() req: any) {
    return this.svc.rejectProposedDeadline(id, req.user);
  }
```

**Важно:** все специфичные роуты (`my`, `created-by-me`, и все `:id/...`) должны быть размещены **перед** общим `@Get(':id')` в файле, иначе NestJS перехватит `my` как `id`. Если в существующем файле `@Get(':id')` уже есть, новые роуты `my`/`created-by-me` нужно добавить выше него.

Проверить: если `@Get(':id')` уже есть в файле, переместить его после новых маршрутов или убедиться что `my`/`created-by-me` стоят раньше в файле.

- [ ] **Step 3: Импорты — убедиться что есть @Body, @Query, @Param, @Patch**

Run: `grep "^import" /root/webclient_rugpt/packages/backend/src/task/task.controller.ts | head -5`
Expected: импорты `@nestjs/common` содержат всё необходимое (Body, Query, Param, Patch). Если нет — добавить в импорт.

- [ ] **Step 4: Сборка**

Run: `cd /root/webclient_rugpt/packages/backend && npm run build 2>&1 | tail -10`
Expected: build success.

---

## Phase 7 — WebClient frontend

### Task 12: Расширение типа Task

**Files:**
- Modify: `/root/webclient_rugpt/packages/common/src/types/task.ts`

- [ ] **Step 1: Прочитать текущий файл**

Run: `cat /root/webclient_rugpt/packages/common/src/types/task.ts`
Expected: показать текущий интерфейс Task.

- [ ] **Step 2: Расширить интерфейс**

В существующий interface `Task` добавить новые поля (по образцу того что уже там):

```typescript
  createdByUserId?: string | null;
  awaitingReviewAt?: string | null;
  proposedDeadline?: string | null;
  proposedDeadlineBy?: string | null;
  priority?: number;  // 1, 2, 3 — computed by backend
  creator?: {
    id: string;
    name: string;
    isAdmin: boolean;
    isHead: boolean;
  } | null;
  assignee?: {
    id: string;
    name: string;
    isHead: boolean;
  } | null;
```

- [ ] **Step 3: Сборка common пакета**

Run: `cd /root/webclient_rugpt/packages/common && npm run build 2>&1 | tail -5`
Expected: clean build, нет TypeScript ошибок.

---

### Task 13: Новая страница /tasks с двумя вкладками

**Files:**
- Modify: `/root/webclient_rugpt/packages/frontend/src/app/tasks/page.tsx`

Это самая большая задача — переписываем страницу. Текущий файл может содержать legacy-логику; полностью заменим.

- [ ] **Step 1: Прочитать существующий файл для понимания структуры layout**

Run: `head -50 /root/webclient_rugpt/packages/frontend/src/app/tasks/page.tsx`
Expected: показать текущие импорты (Sidebar, useAuth, etc), структуру exports.

- [ ] **Step 2: Полностью переписать файл**

Заменить содержимое на:

```tsx
'use client';

import { useState, useEffect, useCallback } from 'react';
import { useRouter } from 'next/navigation';
import { useAuth, useAuthStore } from '../hooks/useAuth';
import { useUsers } from '../hooks/useUsers';
import { useDepartments } from '../hooks/useDepartments';
import { Sidebar } from '../components/Sidebar';
import { ChatNavigation } from '../components/ChatNavigation';
import { getApiClient } from '../../transport/apiClient';

type Tab = 'my' | 'created';

interface TaskRow {
  id: string;
  title: string;
  description?: string | null;
  status: string;
  deadline?: string | null;
  awaitingReviewAt?: string | null;
  proposedDeadline?: string | null;
  proposedDeadlineBy?: string | null;
  priority: number;
  creator?: { id: string; name: string; isAdmin: boolean; isHead: boolean } | null;
  assignee?: { id: string; name: string; isHead: boolean } | null;
}

const STATUS_LABELS: Record<string, string> = {
  created: 'Создана',
  in_progress: 'В работе',
  awaiting_review: 'На приёмке',
  done: 'Выполнена',
  overdue: 'Просрочена',
};

const STATUS_COLORS: Record<string, string> = {
  created: 'bg-gray-200 text-gray-800',
  in_progress: 'bg-blue-200 text-blue-800',
  awaiting_review: 'bg-yellow-200 text-yellow-800',
  done: 'bg-green-200 text-green-800',
  overdue: 'bg-red-200 text-red-800',
};

const PRIORITY_LABELS: Record<number, { label: string; color: string }> = {
  3: { label: 'Срочно', color: 'bg-red-100 text-red-700 border-red-300' },
  2: { label: 'Важно', color: 'bg-yellow-100 text-yellow-700 border-yellow-300' },
  1: { label: 'Обычно', color: 'bg-gray-100 text-gray-700 border-gray-300' },
};

function fmtDate(iso?: string | null): string {
  if (!iso) return '—';
  try {
    return new Date(iso).toLocaleString('ru-RU', {
      day: '2-digit', month: '2-digit', year: '2-digit',
      hour: '2-digit', minute: '2-digit',
    });
  } catch {
    return iso;
  }
}

export default function TasksPage() {
  const { user: currentUser } = useAuth();
  const { users } = useUsers();
  const { departments } = useDepartments();
  const router = useRouter();

  const [tab, setTab] = useState<Tab>('my');
  const [tasks, setTasks] = useState<TaskRow[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [expandedId, setExpandedId] = useState<string | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [includeDone, setIncludeDone] = useState(false);

  // Create modal state
  const [showCreate, setShowCreate] = useState(false);
  const [createForm, setCreateForm] = useState({
    title: '',
    description: '',
    assigneeUserId: '',
    deadline: '',
  });

  // Inline action popovers
  const [editDeadlineId, setEditDeadlineId] = useState<string | null>(null);
  const [deadlineInput, setDeadlineInput] = useState('');
  const [proposeDeadlineId, setProposeDeadlineId] = useState<string | null>(null);
  const [rejectingId, setRejectingId] = useState<string | null>(null);
  const [rejectComment, setRejectComment] = useState('');

  const fetchTasks = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const api = getApiClient();
      const path = tab === 'my'
        ? `/api/tasks/my${includeDone ? '?include_done=true' : ''}`
        : `/api/tasks/created-by-me${includeDone ? '?include_done=true' : ''}`;
      const data = await api.signedGet<TaskRow[]>(path, currentUser?.id);
      setTasks(data || []);
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Ошибка загрузки');
    } finally {
      setLoading(false);
    }
  }, [tab, includeDone, currentUser?.id]);

  useEffect(() => {
    if (currentUser) fetchTasks();
  }, [fetchTasks, currentUser]);

  const action = async (
    fn: () => Promise<void>,
    busyKey: string,
  ) => {
    if (busy) return;
    setBusy(busyKey);
    setError(null);
    try {
      await fn();
      await fetchTasks();
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Ошибка');
    } finally {
      setBusy(null);
    }
  };

  const takeTask = (id: string) => action(async () => {
    const api = getApiClient();
    await api.signedPost(`/api/tasks/${id}/take`, {}, currentUser?.id);
  }, `take-${id}`);

  const markDone = (id: string) => action(async () => {
    const api = getApiClient();
    await api.signedPost(`/api/tasks/${id}/mark-done`, {}, currentUser?.id);
  }, `done-${id}`);

  const acceptTask = (id: string) => action(async () => {
    const api = getApiClient();
    await api.signedPost(`/api/tasks/${id}/accept`, {}, currentUser?.id);
  }, `accept-${id}`);

  const rejectTask = (id: string, comment: string) => action(async () => {
    const api = getApiClient();
    await api.signedPost(`/api/tasks/${id}/reject`, { comment: comment || null }, currentUser?.id);
    setRejectingId(null);
    setRejectComment('');
  }, `reject-${id}`);

  const setDeadline = (id: string, deadline: string) => action(async () => {
    const api = getApiClient();
    await api.signedPatch(`/api/tasks/${id}/deadline`, { deadline }, currentUser?.id);
    setEditDeadlineId(null);
    setDeadlineInput('');
  }, `deadline-${id}`);

  const proposeDeadline = (id: string, proposed: string) => action(async () => {
    const api = getApiClient();
    await api.signedPost(`/api/tasks/${id}/deadline-proposal`, { proposedDeadline: proposed }, currentUser?.id);
    setProposeDeadlineId(null);
    setDeadlineInput('');
  }, `propose-${id}`);

  const acceptProposed = (id: string) => action(async () => {
    const api = getApiClient();
    await api.signedPost(`/api/tasks/${id}/deadline-proposal/accept`, {}, currentUser?.id);
  }, `accept-proposed-${id}`);

  const rejectProposed = (id: string) => action(async () => {
    const api = getApiClient();
    await api.signedPost(`/api/tasks/${id}/deadline-proposal/reject`, {}, currentUser?.id);
  }, `reject-proposed-${id}`);

  const createTask = async () => {
    if (!createForm.title.trim() || !createForm.assigneeUserId) return;
    setError(null);
    try {
      const api = getApiClient();
      await api.signedPost('/api/tasks', {
        title: createForm.title.trim(),
        description: createForm.description.trim() || null,
        assigneeUserId: createForm.assigneeUserId,
        deadline: createForm.deadline || null,
      }, currentUser?.id);
      setShowCreate(false);
      setCreateForm({ title: '', description: '', assigneeUserId: '', deadline: '' });
      await fetchTasks();
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Ошибка создания задачи');
    }
  };

  const isAssigneeOf = (t: TaskRow) => t.assignee?.id === currentUser?.id || tab === 'my';
  const isCreatorOf = (t: TaskRow) => t.creator?.id === currentUser?.id || tab === 'created';

  const visibleUsers = users.filter((u) => !u.isSystem && u.id !== currentUser?.id);
  const deptMap = new Map(departments.map((d) => [d.id, d.name]));

  return (
    <div className="h-dvh bg-neutral-light-lightest dark:bg-gray-900 transition-colors pl-[70px]">
      <Sidebar
        chats={visibleUsers.map((u) => ({
          id: u.id,
          username: u.username,
          name: u.name,
          avatar: u.avatarUrl,
          roleName: u.roleName,
          isGroup: false,
          departmentId: u.departmentId,
          departmentName: u.departmentId ? deptMap.get(u.departmentId) || null : null,
          isHead: u.isHead,
          isSystem: u.isSystem,
        }))}
      />

      <div className="h-full flex flex-col">
        <ChatNavigation title="Задачи" />

        <div className="flex-1 overflow-y-auto px-6 py-6">
          <div className="max-w-6xl mx-auto">
            {/* Tabs */}
            <div className="flex border-b border-gray-200 dark:border-gray-700 mb-4">
              <button
                onClick={() => setTab('my')}
                className={`px-4 py-2 text-sm font-medium border-b-2 transition-colors ${
                  tab === 'my'
                    ? 'border-indigo-600 text-indigo-600 dark:text-indigo-400'
                    : 'border-transparent text-gray-500 hover:text-gray-700 dark:text-gray-400'
                }`}
              >
                Мои задачи
              </button>
              <button
                onClick={() => setTab('created')}
                className={`px-4 py-2 text-sm font-medium border-b-2 transition-colors ${
                  tab === 'created'
                    ? 'border-indigo-600 text-indigo-600 dark:text-indigo-400'
                    : 'border-transparent text-gray-500 hover:text-gray-700 dark:text-gray-400'
                }`}
              >
                Поставленные мной
              </button>

              <div className="ml-auto flex items-center gap-3">
                <label className="text-sm text-gray-600 dark:text-gray-400 flex items-center gap-1">
                  <input
                    type="checkbox"
                    checked={includeDone}
                    onChange={(e) => setIncludeDone(e.target.checked)}
                  />
                  Показать завершённые
                </label>
                <button
                  onClick={() => setShowCreate(true)}
                  className="bg-indigo-600 text-white px-4 py-2 rounded-md text-sm hover:bg-indigo-700"
                >
                  + Создать задачу
                </button>
              </div>
            </div>

            {error && (
              <div role="alert" aria-live="assertive" className="p-3 mb-4 bg-red-100 dark:bg-red-900/40 text-red-800 dark:text-red-200 rounded">
                {error}
              </div>
            )}

            {loading ? (
              <div className="text-gray-500">Загрузка...</div>
            ) : tasks.length === 0 ? (
              <div className="text-gray-500">Нет задач.</div>
            ) : (
              <div className="bg-white dark:bg-gray-800 rounded-lg shadow overflow-hidden">
                <table className="min-w-full divide-y divide-gray-200 dark:divide-gray-700">
                  <thead className="bg-gray-50 dark:bg-gray-900">
                    <tr>
                      <th className="px-4 py-3 text-left text-xs font-medium text-gray-500 uppercase">Название</th>
                      <th className="px-4 py-3 text-left text-xs font-medium text-gray-500 uppercase">Статус</th>
                      <th className="px-4 py-3 text-left text-xs font-medium text-gray-500 uppercase">Приоритет</th>
                      <th className="px-4 py-3 text-left text-xs font-medium text-gray-500 uppercase">Дедлайн</th>
                      <th className="px-4 py-3 text-left text-xs font-medium text-gray-500 uppercase">
                        {tab === 'my' ? 'Создатель' : 'Исполнитель'}
                      </th>
                      <th className="px-4 py-3 text-right text-xs font-medium text-gray-500 uppercase">Действия</th>
                    </tr>
                  </thead>
                  <tbody className="divide-y divide-gray-200 dark:divide-gray-700">
                    {tasks.map((t) => {
                      const expanded = expandedId === t.id;
                      const prio = PRIORITY_LABELS[t.priority] || PRIORITY_LABELS[1];
                      const isMyTab = tab === 'my';
                      const isCreator = isCreatorOf(t);
                      const isAssignee = isAssigneeOf(t);
                      return (
                        <>
                          <tr
                            key={t.id}
                            onClick={() => setExpandedId(expanded ? null : t.id)}
                            className="hover:bg-gray-50 dark:hover:bg-gray-700 cursor-pointer"
                          >
                            <td className="px-4 py-3 text-sm text-gray-900 dark:text-white">
                              {expanded ? '▼' : '▶'} {t.title}
                            </td>
                            <td className="px-4 py-3">
                              <span className={`px-2 py-1 text-xs rounded ${STATUS_COLORS[t.status] || 'bg-gray-200'}`}>
                                {STATUS_LABELS[t.status] || t.status}
                              </span>
                            </td>
                            <td className="px-4 py-3">
                              <span className={`px-2 py-1 text-xs rounded border ${prio.color}`}>
                                {prio.label}
                              </span>
                            </td>
                            <td className="px-4 py-3 text-sm text-gray-700 dark:text-gray-300">
                              {fmtDate(t.deadline)}
                              {t.proposedDeadline && (
                                <div className="text-xs text-yellow-600 dark:text-yellow-400">
                                  Предложен: {fmtDate(t.proposedDeadline)}
                                </div>
                              )}
                            </td>
                            <td className="px-4 py-3 text-sm text-gray-700 dark:text-gray-300">
                              {isMyTab ? (t.creator?.name || '—') : (t.assignee?.name || '—')}
                            </td>
                            <td className="px-4 py-3 text-right text-sm whitespace-nowrap" onClick={(e) => e.stopPropagation()}>
                              {/* Assignee actions */}
                              {isAssignee && t.status === 'created' && (
                                <button
                                  onClick={() => takeTask(t.id)}
                                  disabled={busy === `take-${t.id}`}
                                  className="text-indigo-600 hover:text-indigo-800 disabled:opacity-50 mr-2"
                                >
                                  Взять
                                </button>
                              )}
                              {isAssignee && t.status === 'in_progress' && (
                                <>
                                  <button
                                    onClick={() => markDone(t.id)}
                                    disabled={busy === `done-${t.id}`}
                                    className="text-green-600 hover:text-green-800 disabled:opacity-50 mr-2"
                                  >
                                    Готово
                                  </button>
                                  <button
                                    onClick={() => { setProposeDeadlineId(t.id); setDeadlineInput(''); }}
                                    className="text-gray-600 hover:text-gray-800 mr-2"
                                  >
                                    Предложить срок
                                  </button>
                                </>
                              )}

                              {/* Creator actions */}
                              {isCreator && t.status === 'awaiting_review' && (
                                <>
                                  <button
                                    onClick={() => acceptTask(t.id)}
                                    disabled={busy === `accept-${t.id}`}
                                    className="text-green-600 hover:text-green-800 disabled:opacity-50 mr-2"
                                  >
                                    Принять
                                  </button>
                                  <button
                                    onClick={() => { setRejectingId(t.id); setRejectComment(''); }}
                                    className="text-red-600 hover:text-red-800 mr-2"
                                  >
                                    Вернуть
                                  </button>
                                </>
                              )}
                              {isCreator && (
                                <button
                                  onClick={() => { setEditDeadlineId(t.id); setDeadlineInput(t.deadline?.slice(0, 16) || ''); }}
                                  className="text-gray-600 hover:text-gray-800"
                                >
                                  Срок
                                </button>
                              )}
                            </td>
                          </tr>

                          {expanded && (
                            <tr className="bg-gray-50 dark:bg-gray-900">
                              <td colSpan={6} className="px-4 py-4">
                                <div className="space-y-3">
                                  {t.description && (
                                    <div>
                                      <div className="text-xs font-medium text-gray-500 dark:text-gray-400 mb-1">Описание</div>
                                      <div className="text-sm text-gray-800 dark:text-gray-200 whitespace-pre-wrap">{t.description}</div>
                                    </div>
                                  )}
                                  <div className="grid grid-cols-2 gap-4 text-sm">
                                    <div>
                                      <div className="text-xs font-medium text-gray-500 dark:text-gray-400">Создатель</div>
                                      <div className="text-gray-800 dark:text-gray-200">{t.creator?.name || '— (legacy)'}</div>
                                    </div>
                                    <div>
                                      <div className="text-xs font-medium text-gray-500 dark:text-gray-400">Исполнитель</div>
                                      <div className="text-gray-800 dark:text-gray-200">{t.assignee?.name || '—'}</div>
                                    </div>
                                  </div>
                                  {t.proposedDeadline && isCreator && (
                                    <div className="p-3 bg-yellow-50 dark:bg-yellow-900/20 rounded">
                                      <div className="text-sm text-gray-800 dark:text-gray-200 mb-2">
                                        Исполнитель предложил новый срок: <b>{fmtDate(t.proposedDeadline)}</b>
                                      </div>
                                      <div className="flex gap-2">
                                        <button
                                          onClick={() => acceptProposed(t.id)}
                                          disabled={busy === `accept-proposed-${t.id}`}
                                          className="bg-green-600 text-white px-3 py-1 rounded text-xs hover:bg-green-700 disabled:opacity-50"
                                        >
                                          Принять
                                        </button>
                                        <button
                                          onClick={() => rejectProposed(t.id)}
                                          disabled={busy === `reject-proposed-${t.id}`}
                                          className="bg-red-600 text-white px-3 py-1 rounded text-xs hover:bg-red-700 disabled:opacity-50"
                                        >
                                          Отклонить
                                        </button>
                                      </div>
                                    </div>
                                  )}

                                  {/* Inline popovers */}
                                  {editDeadlineId === t.id && (
                                    <div className="p-3 bg-white dark:bg-gray-800 border border-gray-300 dark:border-gray-600 rounded">
                                      <div className="text-sm mb-2 text-gray-800 dark:text-gray-200">Изменить срок</div>
                                      <input
                                        type="datetime-local"
                                        value={deadlineInput}
                                        onChange={(e) => setDeadlineInput(e.target.value)}
                                        className="border border-gray-300 dark:border-gray-600 rounded px-2 py-1 text-sm bg-white dark:bg-gray-700"
                                      />
                                      <div className="flex gap-2 mt-2">
                                        <button
                                          onClick={() => deadlineInput && setDeadline(t.id, new Date(deadlineInput).toISOString())}
                                          disabled={!deadlineInput || busy === `deadline-${t.id}`}
                                          className="bg-indigo-600 text-white px-3 py-1 rounded text-xs disabled:opacity-50"
                                        >
                                          Сохранить
                                        </button>
                                        <button
                                          onClick={() => { setEditDeadlineId(null); setDeadlineInput(''); }}
                                          className="text-gray-600 px-3 py-1 text-xs"
                                        >
                                          Отмена
                                        </button>
                                      </div>
                                    </div>
                                  )}
                                  {proposeDeadlineId === t.id && (
                                    <div className="p-3 bg-white dark:bg-gray-800 border border-gray-300 dark:border-gray-600 rounded">
                                      <div className="text-sm mb-2 text-gray-800 dark:text-gray-200">Предложить новый срок</div>
                                      <input
                                        type="datetime-local"
                                        value={deadlineInput}
                                        onChange={(e) => setDeadlineInput(e.target.value)}
                                        className="border border-gray-300 dark:border-gray-600 rounded px-2 py-1 text-sm bg-white dark:bg-gray-700"
                                      />
                                      <div className="flex gap-2 mt-2">
                                        <button
                                          onClick={() => deadlineInput && proposeDeadline(t.id, new Date(deadlineInput).toISOString())}
                                          disabled={!deadlineInput || busy === `propose-${t.id}`}
                                          className="bg-indigo-600 text-white px-3 py-1 rounded text-xs disabled:opacity-50"
                                        >
                                          Предложить
                                        </button>
                                        <button
                                          onClick={() => { setProposeDeadlineId(null); setDeadlineInput(''); }}
                                          className="text-gray-600 px-3 py-1 text-xs"
                                        >
                                          Отмена
                                        </button>
                                      </div>
                                    </div>
                                  )}
                                  {rejectingId === t.id && (
                                    <div className="p-3 bg-white dark:bg-gray-800 border border-gray-300 dark:border-gray-600 rounded">
                                      <div className="text-sm mb-2 text-gray-800 dark:text-gray-200">Причина возврата (опционально)</div>
                                      <textarea
                                        value={rejectComment}
                                        onChange={(e) => setRejectComment(e.target.value)}
                                        placeholder="Что нужно доработать..."
                                        rows={3}
                                        className="w-full border border-gray-300 dark:border-gray-600 rounded px-2 py-1 text-sm bg-white dark:bg-gray-700"
                                      />
                                      <div className="flex gap-2 mt-2">
                                        <button
                                          onClick={() => rejectTask(t.id, rejectComment)}
                                          disabled={busy === `reject-${t.id}`}
                                          className="bg-red-600 text-white px-3 py-1 rounded text-xs disabled:opacity-50"
                                        >
                                          Вернуть
                                        </button>
                                        <button
                                          onClick={() => { setRejectingId(null); setRejectComment(''); }}
                                          className="text-gray-600 px-3 py-1 text-xs"
                                        >
                                          Отмена
                                        </button>
                                      </div>
                                    </div>
                                  )}
                                </div>
                              </td>
                            </tr>
                          )}
                        </>
                      );
                    })}
                  </tbody>
                </table>
              </div>
            )}
          </div>
        </div>
      </div>

      {/* Create modal */}
      {showCreate && (
        <div className="fixed inset-0 bg-black bg-opacity-50 flex items-center justify-center z-50">
          <div className="bg-white dark:bg-gray-800 rounded-lg p-6 w-full max-w-md mx-4">
            <h3 className="text-lg font-semibold mb-4 text-gray-900 dark:text-white">Создать задачу</h3>
            <div className="space-y-3">
              <input
                type="text"
                value={createForm.title}
                onChange={(e) => setCreateForm({ ...createForm, title: e.target.value })}
                placeholder="Название"
                className="w-full border border-gray-300 dark:border-gray-600 rounded-md bg-white dark:bg-gray-700 text-gray-900 dark:text-white px-3 py-2"
                autoFocus
              />
              <textarea
                value={createForm.description}
                onChange={(e) => setCreateForm({ ...createForm, description: e.target.value })}
                placeholder="Описание (опционально)"
                rows={3}
                className="w-full border border-gray-300 dark:border-gray-600 rounded-md bg-white dark:bg-gray-700 text-gray-900 dark:text-white px-3 py-2"
              />
              <select
                value={createForm.assigneeUserId}
                onChange={(e) => setCreateForm({ ...createForm, assigneeUserId: e.target.value })}
                className="w-full border border-gray-300 dark:border-gray-600 rounded-md bg-white dark:bg-gray-700 text-gray-900 dark:text-white px-3 py-2"
              >
                <option value="">— Выберите исполнителя —</option>
                {visibleUsers.map((u) => (
                  <option key={u.id} value={u.id}>{u.name}</option>
                ))}
              </select>
              <input
                type="datetime-local"
                value={createForm.deadline}
                onChange={(e) => setCreateForm({ ...createForm, deadline: e.target.value ? new Date(e.target.value).toISOString() : '' })}
                className="w-full border border-gray-300 dark:border-gray-600 rounded-md bg-white dark:bg-gray-700 text-gray-900 dark:text-white px-3 py-2"
              />
            </div>
            <div className="flex justify-end gap-2 mt-4">
              <button
                onClick={() => { setShowCreate(false); setCreateForm({ title: '', description: '', assigneeUserId: '', deadline: '' }); }}
                className="px-4 py-2 text-gray-700 dark:text-gray-300 hover:bg-gray-100 dark:hover:bg-gray-700 rounded"
              >
                Отмена
              </button>
              <button
                onClick={createTask}
                disabled={!createForm.title.trim() || !createForm.assigneeUserId}
                className="bg-indigo-600 text-white px-4 py-2 rounded hover:bg-indigo-700 disabled:opacity-50"
              >
                Создать
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
```

- [ ] **Step 3: Typecheck**

Run: `cd /root/webclient_rugpt/packages/frontend && npx tsc --noEmit 2>&1 | grep -i "tasks/page" || echo "clean"`
Expected: `clean` или нет ошибок в `tasks/page`.

---

## Phase 8 — Финальная проверка

### Task 14: Smoke-test всего пайплайна

**Files:**
- Нет изменений

- [ ] **Step 1: Запустить engine локально**

Run: `cd /root/rugpt && source venv/bin/activate && nohup uvicorn src.engine.app:app --host 127.0.0.1 --port 8100 > /tmp/engine.log 2>&1 &`
Expected: процесс стартует, в `/tmp/engine.log` появляется `Application startup complete.`

- [ ] **Step 2: Проверить новые роуты в OpenAPI**

Run: `curl -s http://127.0.0.1:8100/openapi.json | python -c "import json, sys; data = json.load(sys.stdin); paths = [p for p in data['paths'] if '/tasks' in p]; print('\n'.join(sorted(paths)))"`
Expected: вывод содержит:
```
/api/v1/tasks
/api/v1/tasks/created-by-me
/api/v1/tasks/my
/api/v1/tasks/{task_id}
/api/v1/tasks/{task_id}/accept
/api/v1/tasks/{task_id}/deadline
/api/v1/tasks/{task_id}/deadline-proposal
/api/v1/tasks/{task_id}/deadline-proposal/accept
/api/v1/tasks/{task_id}/deadline-proposal/reject
/api/v1/tasks/{task_id}/mark-done
/api/v1/tasks/{task_id}/reject
/api/v1/tasks/{task_id}/take
```

- [ ] **Step 3: Запустить все тесты ещё раз**

Run: `cd /root/rugpt && venv/bin/python -m pytest tests/ -v 2>&1 | tail -20`
Expected: все тесты проходят.

- [ ] **Step 4: Остановить engine**

Run: `pkill -f "uvicorn src.engine.app" || true`

- [ ] **Step 5: Сборка backend webclient**

Run: `cd /root/webclient_rugpt/packages/backend && npm run build 2>&1 | tail -5`
Expected: clean build.

- [ ] **Step 6: Typecheck frontend webclient**

Run: `cd /root/webclient_rugpt/packages/frontend && npx tsc --noEmit 2>&1 | grep -v "useFiles" | tail -10`
Expected: нет ошибок (кроме pre-existing useFiles.ts).

---

## Спек-ковередж (self-review)

| Спек | Задача |
|---|---|
| Миграция 016 (новые поля) | Task 1 |
| Расширение модели Task + статус awaiting_review | Task 2 |
| TaskStorage с JOIN на creator | Task 3 |
| Тесты priority + transitions + deadline + legacy | Task 4 |
| TaskService.compute_priority + новые методы переходов | Task 5 |
| TaskService методы negotiation дедлайна | Task 5 |
| API: GET /tasks/my | Task 6 |
| API: GET /tasks/created-by-me | Task 6 |
| API: POST /tasks/{id}/take | Task 6 |
| API: POST /tasks/{id}/mark-done | Task 6 |
| API: POST /tasks/{id}/accept | Task 6 |
| API: POST /tasks/{id}/reject (с comment) | Task 6 |
| API: PATCH /tasks/{id}/deadline | Task 6 |
| API: POST /tasks/{id}/deadline-proposal + accept/reject | Task 6 |
| AI tools: created_by_user_id в task_create | Task 7 |
| WebClient adapter: новые команды | Task 9 |
| WebClient TaskService: методы-обёртки | Task 10 |
| WebClient TaskController: новые роуты | Task 11 |
| Common types: priority + creator + assignee | Task 12 |
| WebClient /tasks page: вкладки + expand-row + inline actions + create modal | Task 13 |
| Smoke-test пайплайна | Task 14 |

Все требования спека покрыты задачами.
