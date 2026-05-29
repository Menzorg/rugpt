# Support Chat Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Добавить чат тех. поддержки в RuGPT с категориями обращений (`how_to`/`bug`/`other`), AI первой линии для типовых вопросов и эскалацией на оператора.

**Architecture:** Отдельная сущность `support_ticket` (новая таблица + storage + сервис + routes). Операторы — юзеры новой орг RuGPT Support. Чат тикета — `ChatType.SUPPORT` с точечным cross-org exemption строго по типу чата. AI первой линии — системный юзер `support_ai` через существующий `try_auto_respond`.

**Tech Stack:**
- Engine: Python 3.10+, FastAPI, asyncpg, LangChain/LangGraph, Kafka (aiokafka)
- WebClient backend: NestJS 11, TypeScript, Socket.IO, axios
- WebClient frontend: Next.js 15, React 19, Zustand, Tailwind

**Spec:** `/root/rugpt/docs/2026-04-26-support-chat-design.md`

---

## File Structure

### Engine (`/root/rugpt/`)

**Создаются:**
- `src/engine/migrations/023_support_tickets.sql` — миграция: орг RuGPT Support, роль `support_assistant`, system user `support_ai`, расширение `chats.type`, поле `chats.support_ticket_id`, таблицы `support_tickets` + `support_ticket_events`.
- `src/engine/models/support_ticket.py` — `SupportTicket`, `SupportTicketCategory`, `SupportTicketStatus` dataclass+enums.
- `src/engine/models/support_ticket_event.py` — `SupportTicketEvent`, `SupportTicketEventType`, `SupportTicketActorRole`.
- `src/engine/storage/support_ticket_storage.py` — `SupportTicketStorage` (CRUD + atomic CAS `take`).
- `src/engine/storage/support_ticket_event_storage.py` — `SupportTicketEventStorage` (insert + list).
- `src/engine/services/support_ticket_service.py` — бизнес-логика тикетов (create/escalate/take/close/reopen).
- `src/engine/services/support_notification_service.py` — fan-out операторам через `InAppNotificationService`.
- `src/engine/routes/support.py` — все `/api/v1/support/*` endpoints.
- `src/engine/prompts/support_assistant.md` — промпт AI первой линии.
- `tests/test_support_ticket_storage.py`, `tests/test_support_ticket_service.py`, `tests/test_support_routes.py`, `tests/test_cross_org_chat_isolation.py`.

**Модифицируются:**
- `src/engine/models/chat.py:13-18` — добавить `SUPPORT = "support"` в `ChatType` enum, `_coerce_chat_type` обновить.
- `src/engine/models/chat.py:38-79` — добавить поле `support_ticket_id: Optional[UUID]` в `Chat` dataclass + `to_dict`/`from_dict`.
- `src/engine/storage/chat_storage.py` — без правок: storage уже cross-org transparent (`get_by_id` без orgship, `list_by_user` фильтрует только по `participants`). Регрессионные тесты в `tests/test_chat_storage_support_cross_org.py` страхуют, что никто не добавит orgship-фильтр обратно. Реальный access-чек живёт в `ChatService.can_user_access_chat` (Task 9).
- `src/engine/services/chat_service.py` — метод `can_user_access_chat(user, chat)` с exemption для `SUPPORT`. Hook в `add_message` для auto-reopen тикета.
- `src/engine/storage/message_storage.py` — снять orgship-фильтр в `list_by_chat` и `add_message` (доверить access-чек сервису).
- `src/engine/services/engine_service.py` — добавить `support_ticket_storage`, `support_ticket_event_storage`, `support_ticket_service`, `support_notification_service` поля и инициализацию.
- `src/engine/app.py` — подключить `routes/support.py` router.
- `src/engine/config.py` — добавить `SUPPORT_REOPEN_WINDOW_DAYS=7`, `RUGPT_SUPPORT_ORG_ID`, `SUPPORT_AI_USER_USERNAME='support_ai'`.
- `src/engine/services/ai_service.py` — `try_auto_respond` должен срабатывать на `ChatType.SUPPORT` чатах с условиями раздела 6.3 спеки.

### WebClient backend (`/root/webclient_rugpt/packages/backend/`)

**Создаются:**
- `src/support/support.module.ts`
- `src/support/support.controller.ts` — endpoints `/api/support/*`.
- `src/support/support.service.ts` — proxy через `engineAdapter.execute`.
- `src/support/support.types.ts` — TypeScript типы (DTO).
- `test/support.controller.spec.ts`.

**Модифицируются:**
- `src/engine/adapters/rugpt.adapter.ts` — добавить кейсы команд: `support_create_ticket`, `support_get_my_tickets`, `support_get_ticket`, `support_escalate`, `support_close`, `support_queue`, `support_take`, `support_operator_my`, `support_get_chat`, `support_get_events`.
- `src/socket/socket.gateway.ts` — `handleJoinChat` (или эквивалент) — exempt `chat.type='support'` из orgship-чека через делегирование в Engine `can_user_access_chat`.
- `src/app.module.ts` — импорт `SupportModule`.

### WebClient frontend (`/root/webclient_rugpt/packages/frontend/`)

**Создаются:**
- `src/app/support/page.tsx` — лендинг (свитч категорий + список тикетов).
- `src/app/support/queue/page.tsx` — очередь оператора.
- `src/app/chat/support/[id]/page.tsx` — чат тикета.
- `src/app/hooks/useSupportTickets.ts`
- `src/app/hooks/useSupportQueue.ts`
- `src/app/hooks/useSupportOperatorMy.ts`
- `src/app/hooks/useSupportUnread.ts`
- `src/app/components/SupportCategorySwitch.tsx` — три кнопки категорий.
- `src/app/components/SupportSoftHandoffBlock.tsx` — блок «Продолжить с ИИ / Позвать оператора».
- `src/app/components/SupportTicketCard.tsx` — строка списка тикетов.

**Модифицируются:**
- `src/app/components/Sidebar.tsx` — добавить иконку «Тех. поддержка» (`HeadsetIcon`) под кнопкой «Персональный ИИ».
- `src/app/components/MessageBubble.tsx` — props `chatType` + `senderOrgId`, при `chatType==='support' && senderOrgId===RUGPT_SUPPORT_ORG_ID` подменять имя на «Тех. поддержка».
- `src/app/tasks/page.tsx` — для юзеров RuGPT Support орг рендерить табы «Задачи» / «Тикеты тех. поддержки».
- `packages/common/src/types/` — добавить `SupportTicket`, `SupportTicketEvent`, `SupportTicketCategory`, `SupportTicketStatus` типы.
- `packages/common/src/index.ts` — экспортировать новые типы.

### Shared

**Модифицируется:**
- `packages/common/src/types/chat.ts` — расширить `ChatType` enum значением `SUPPORT='support'`, добавить `support_ticket_id` в `Chat`.

---

## Phase 1 — Engine

### Task 1: Миграция 019

**Files:**
- Create: `src/engine/migrations/023_support_tickets.sql`

- [ ] **Step 1: Создать SQL миграцию**

Содержимое файла берётся **дословно** из спеки `2026-04-26-support-chat-design.md` раздел 3.1. Не отклоняться от схемы.

- [ ] **Step 2: Применить миграцию локально**

Run: `cd /root/rugpt && ./migrate.sh`
Expected: миграция 019 применилась без ошибок.

- [ ] **Step 3: Проверить структуру в psql**

Run:
```
psql -U postgres -h localhost -d rugpt -c "\d support_tickets"
psql -U postgres -h localhost -d rugpt -c "\d support_ticket_events"
psql -U postgres -h localhost -d rugpt -c "SELECT id, name FROM organizations WHERE slug='rugpt-support';"
psql -U postgres -h localhost -d rugpt -c "SELECT username FROM users WHERE username='support_ai';"
psql -U postgres -h localhost -d rugpt -c "SELECT code FROM roles WHERE code='support_assistant';"
```
Expected: таблицы есть, орг + системный юзер + роль созданы.

- [ ] **Step 4: Проверить что `chats.type` принимает `support`**

Run: `psql -U postgres -h localhost -d rugpt -c "SELECT con.consrc FROM pg_constraint con WHERE conname='chats_type_check';"` (или эквивалент через pg_get_constraintdef)
Expected: constraint содержит `'support'`.

- [ ] **Step 5: Commit**

_Skipped per user instruction: no git commits during this implementation._

---

### Task 2: Модель SupportTicket

**Files:**
- Create: `src/engine/models/support_ticket.py`
- Test: `tests/test_support_ticket_model.py`

- [ ] **Step 1: Написать failing test для to_dict/from_dict round-trip**

```python
# tests/test_support_ticket_model.py
from datetime import datetime
from uuid import uuid4
from src.engine.models.support_ticket import (
    SupportTicket, SupportTicketCategory, SupportTicketStatus
)

def test_round_trip_to_dict_from_dict():
    t = SupportTicket(
        id=uuid4(),
        requester_user_id=uuid4(),
        requester_org_id=uuid4(),
        category=SupportTicketCategory.HOW_TO,
        status=SupportTicketStatus.OPEN,
        title="как создать чат",
    )
    d = t.to_dict()
    t2 = SupportTicket.from_dict(d)
    assert t2.id == t.id
    assert t2.category == SupportTicketCategory.HOW_TO
    assert t2.status == SupportTicketStatus.OPEN
    assert t2.title == "как создать чат"
```

- [ ] **Step 2: Запустить тест — должен упасть**

Run: `cd /root/rugpt && source venv/bin/activate && pytest tests/test_support_ticket_model.py -v`
Expected: ImportError, модели нет.

- [ ] **Step 3: Реализовать модель**

```python
# src/engine/models/support_ticket.py
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Optional
from uuid import UUID, uuid4


class SupportTicketCategory(str, Enum):
    HOW_TO = "how_to"
    BUG = "bug"
    OTHER = "other"


class SupportTicketStatus(str, Enum):
    OPEN = "open"
    IN_PROGRESS = "in_progress"
    CLOSED = "closed"


class ClosedByRole(str, Enum):
    REQUESTER = "requester"
    OPERATOR = "operator"


@dataclass
class SupportTicket:
    id: UUID = field(default_factory=uuid4)
    requester_user_id: UUID = field(default_factory=uuid4)
    requester_org_id: UUID = field(default_factory=uuid4)
    category: SupportTicketCategory = SupportTicketCategory.HOW_TO
    status: SupportTicketStatus = SupportTicketStatus.OPEN
    assignee_user_id: Optional[UUID] = None
    ai_handoff_at: Optional[datetime] = None
    ai_first_response_at: Optional[datetime] = None
    closed_at: Optional[datetime] = None
    closed_by_user_id: Optional[UUID] = None
    closed_by_role: Optional[ClosedByRole] = None
    title: Optional[str] = None
    created_at: datetime = field(default_factory=datetime.utcnow)
    updated_at: datetime = field(default_factory=datetime.utcnow)

    def to_dict(self) -> dict:
        return {
            "id": str(self.id),
            "requester_user_id": str(self.requester_user_id),
            "requester_org_id": str(self.requester_org_id),
            "category": self.category.value,
            "status": self.status.value,
            "assignee_user_id": str(self.assignee_user_id) if self.assignee_user_id else None,
            "ai_handoff_at": self.ai_handoff_at.isoformat() if self.ai_handoff_at else None,
            "ai_first_response_at": self.ai_first_response_at.isoformat() if self.ai_first_response_at else None,
            "closed_at": self.closed_at.isoformat() if self.closed_at else None,
            "closed_by_user_id": str(self.closed_by_user_id) if self.closed_by_user_id else None,
            "closed_by_role": self.closed_by_role.value if self.closed_by_role else None,
            "title": self.title,
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
        }

    @classmethod
    def from_dict(cls, data: dict) -> "SupportTicket":
        def _u(v):
            return UUID(v) if isinstance(v, str) else v
        def _dt(v):
            return datetime.fromisoformat(v) if isinstance(v, str) else v
        return cls(
            id=_u(data["id"]),
            requester_user_id=_u(data["requester_user_id"]),
            requester_org_id=_u(data["requester_org_id"]),
            category=SupportTicketCategory(data["category"]),
            status=SupportTicketStatus(data["status"]),
            assignee_user_id=_u(data.get("assignee_user_id")) if data.get("assignee_user_id") else None,
            ai_handoff_at=_dt(data.get("ai_handoff_at")),
            ai_first_response_at=_dt(data.get("ai_first_response_at")),
            closed_at=_dt(data.get("closed_at")),
            closed_by_user_id=_u(data.get("closed_by_user_id")) if data.get("closed_by_user_id") else None,
            closed_by_role=ClosedByRole(data["closed_by_role"]) if data.get("closed_by_role") else None,
            title=data.get("title"),
            created_at=_dt(data["created_at"]) or datetime.utcnow(),
            updated_at=_dt(data["updated_at"]) or datetime.utcnow(),
        )
```

- [ ] **Step 4: Запустить тест — должен пройти**

Run: `pytest tests/test_support_ticket_model.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

`git add src/engine/models/support_ticket.py tests/test_support_ticket_model.py && git commit -m "feat(engine): SupportTicket model"`

---

### Task 3: Модель SupportTicketEvent

**Files:**
- Create: `src/engine/models/support_ticket_event.py`
- Test: `tests/test_support_ticket_event_model.py`

- [ ] **Step 1: Failing test (round-trip)**

```python
# tests/test_support_ticket_event_model.py
from uuid import uuid4
from src.engine.models.support_ticket_event import (
    SupportTicketEvent, SupportTicketEventType, SupportTicketActorRole
)

def test_event_round_trip():
    e = SupportTicketEvent(
        ticket_id=uuid4(),
        actor_user_id=uuid4(),
        actor_role=SupportTicketActorRole.OPERATOR,
        event_type=SupportTicketEventType.TAKEN,
        payload={"note": "взял"},
    )
    d = e.to_dict()
    e2 = SupportTicketEvent.from_dict(d)
    assert e2.actor_role == SupportTicketActorRole.OPERATOR
    assert e2.event_type == SupportTicketEventType.TAKEN
    assert e2.payload == {"note": "взял"}
```

- [ ] **Step 2: Run, expect fail (ImportError)**

Run: `pytest tests/test_support_ticket_event_model.py -v`

- [ ] **Step 3: Implement**

```python
# src/engine/models/support_ticket_event.py
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Optional
from uuid import UUID, uuid4


class SupportTicketEventType(str, Enum):
    CREATED = "created"
    AI_RESPONDED = "ai_responded"
    AI_HANDOFF = "ai_handoff"
    TAKEN = "taken"
    CLOSED = "closed"
    REOPENED = "reopened"
    MESSAGE = "message"


class SupportTicketActorRole(str, Enum):
    REQUESTER = "requester"
    OPERATOR = "operator"
    AI = "ai"
    SYSTEM = "system"


@dataclass
class SupportTicketEvent:
    id: UUID = field(default_factory=uuid4)
    ticket_id: UUID = field(default_factory=uuid4)
    actor_user_id: UUID = field(default_factory=uuid4)
    actor_role: SupportTicketActorRole = SupportTicketActorRole.SYSTEM
    event_type: SupportTicketEventType = SupportTicketEventType.CREATED
    payload: dict = field(default_factory=dict)
    created_at: datetime = field(default_factory=datetime.utcnow)

    def to_dict(self) -> dict:
        return {
            "id": str(self.id),
            "ticket_id": str(self.ticket_id),
            "actor_user_id": str(self.actor_user_id),
            "actor_role": self.actor_role.value,
            "event_type": self.event_type.value,
            "payload": self.payload,
            "created_at": self.created_at.isoformat(),
        }

    @classmethod
    def from_dict(cls, data: dict) -> "SupportTicketEvent":
        def _u(v):
            return UUID(v) if isinstance(v, str) else v
        def _dt(v):
            return datetime.fromisoformat(v) if isinstance(v, str) else v
        return cls(
            id=_u(data["id"]),
            ticket_id=_u(data["ticket_id"]),
            actor_user_id=_u(data["actor_user_id"]),
            actor_role=SupportTicketActorRole(data["actor_role"]),
            event_type=SupportTicketEventType(data["event_type"]),
            payload=data.get("payload", {}),
            created_at=_dt(data["created_at"]),
        )
```

- [ ] **Step 4: Run test, expect pass**

- [ ] **Step 5: Commit**

`git add src/engine/models/support_ticket_event.py tests/test_support_ticket_event_model.py && git commit -m "feat(engine): SupportTicketEvent model"`

---

### Task 4: Расширение модели Chat

**Files:**
- Modify: `src/engine/models/chat.py`
- Test: `tests/test_chat_model.py` (новый тест в существующий файл)

- [ ] **Step 1: Failing test для нового ChatType.SUPPORT и поля support_ticket_id**

```python
# Добавить в существующий tests/test_chat_model.py (если нет — создать)
from uuid import uuid4
from src.engine.models.chat import Chat, ChatType

def test_chat_support_type_and_ticket_id():
    ticket_id = uuid4()
    c = Chat(type=ChatType.SUPPORT, support_ticket_id=ticket_id)
    d = c.to_dict()
    assert d["type"] == "support"
    assert d["support_ticket_id"] == str(ticket_id)
    c2 = Chat.from_dict(d)
    assert c2.type == ChatType.SUPPORT
    assert c2.support_ticket_id == ticket_id
```

- [ ] **Step 2: Run, expect fail**

- [ ] **Step 3: Modify `src/engine/models/chat.py`**

В `ChatType` (строка 13-18) добавить:
```python
SUPPORT = "support"   # Support ticket chat (cross-org)
```

В `_coerce_chat_type` (строка 20-35) — никаких изменений, `ChatType("support")` сработает корректно.

В `Chat` dataclass (строка 38-79) добавить поле после `project_id`:
```python
support_ticket_id: Optional[UUID] = None  # Set iff type == SUPPORT
```

В `to_dict` добавить:
```python
"support_ticket_id": str(self.support_ticket_id) if self.support_ticket_id else None,
```

В `from_dict` добавить в `cls(...)`:
```python
support_ticket_id=_uuid_or_none(data.get("support_ticket_id")),
```

- [ ] **Step 4: Run test, expect pass**

- [ ] **Step 5: Commit**

`git add src/engine/models/chat.py tests/test_chat_model.py && git commit -m "feat(engine): ChatType.SUPPORT + support_ticket_id field"`

---

### Task 5: SupportTicketStorage с atomic CAS take

**Files:**
- Create: `src/engine/storage/support_ticket_storage.py`
- Test: `tests/test_support_ticket_storage.py`

- [ ] **Step 1: Failing tests — основные операции и concurrent take**

```python
# tests/test_support_ticket_storage.py
import asyncio
import pytest
from uuid import uuid4
from src.engine.models.support_ticket import (
    SupportTicket, SupportTicketCategory, SupportTicketStatus
)
from src.engine.storage.support_ticket_storage import SupportTicketStorage

# pytest fixtures `db_pool`, `seed_user`, `seed_org` предполагаются — посмотреть существующий conftest.py

@pytest.mark.asyncio
async def test_create_and_get(db_pool, seed_user, seed_org):
    storage = SupportTicketStorage(db_pool)
    t = SupportTicket(
        requester_user_id=seed_user.id,
        requester_org_id=seed_org.id,
        category=SupportTicketCategory.HOW_TO,
    )
    await storage.create(t)
    got = await storage.get_by_id(t.id)
    assert got is not None
    assert got.requester_user_id == seed_user.id

@pytest.mark.asyncio
async def test_take_atomic_cas_one_winner(db_pool, seed_user, seed_org, seed_operator):
    storage = SupportTicketStorage(db_pool)
    t = SupportTicket(
        requester_user_id=seed_user.id,
        requester_org_id=seed_org.id,
        category=SupportTicketCategory.BUG,
        status=SupportTicketStatus.OPEN,
    )
    await storage.create(t)
    # 3 одновременных попытки take
    results = await asyncio.gather(
        storage.take(t.id, seed_operator.id),
        storage.take(t.id, seed_operator.id),
        storage.take(t.id, seed_operator.id),
        return_exceptions=False,
    )
    successes = [r for r in results if r is not None]
    assert len(successes) == 1
```

- [ ] **Step 2: Run, expect fail (no module)**

- [ ] **Step 3: Implement storage**

Опираясь на паттерн существующего `task_storage.py` и `agent_run_storage.py` (для CAS-паттерна — посмотреть `mark_running`):

```python
# src/engine/storage/support_ticket_storage.py
from typing import List, Optional
from uuid import UUID
from src.engine.models.support_ticket import (
    SupportTicket, SupportTicketStatus, SupportTicketCategory, ClosedByRole
)
from src.engine.storage.base import BaseStorage


class SupportTicketStorage(BaseStorage):

    async def create(self, t: SupportTicket) -> SupportTicket:
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                INSERT INTO support_tickets (
                    id, requester_user_id, requester_org_id, category, status,
                    assignee_user_id, ai_handoff_at, ai_first_response_at,
                    closed_at, closed_by_user_id, closed_by_role,
                    title, created_at, updated_at
                ) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14)
                RETURNING *
                """,
                t.id, t.requester_user_id, t.requester_org_id,
                t.category.value, t.status.value,
                t.assignee_user_id, t.ai_handoff_at, t.ai_first_response_at,
                t.closed_at, t.closed_by_user_id,
                t.closed_by_role.value if t.closed_by_role else None,
                t.title, t.created_at, t.updated_at,
            )
            return self._row_to_ticket(row)

    async def get_by_id(self, ticket_id: UUID) -> Optional[SupportTicket]:
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow("SELECT * FROM support_tickets WHERE id=$1", ticket_id)
            return self._row_to_ticket(row) if row else None

    async def list_by_requester(self, user_id: UUID, status: Optional[SupportTicketStatus] = None, limit: int = 50) -> List[SupportTicket]:
        async with self.pool.acquire() as conn:
            if status:
                rows = await conn.fetch(
                    "SELECT * FROM support_tickets WHERE requester_user_id=$1 AND status=$2 ORDER BY created_at DESC LIMIT $3",
                    user_id, status.value, limit,
                )
            else:
                rows = await conn.fetch(
                    "SELECT * FROM support_tickets WHERE requester_user_id=$1 ORDER BY created_at DESC LIMIT $2",
                    user_id, limit,
                )
            return [self._row_to_ticket(r) for r in rows]

    async def list_queue(self, limit: int = 100) -> List[SupportTicket]:
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT * FROM support_tickets WHERE status='open' AND assignee_user_id IS NULL ORDER BY created_at ASC LIMIT $1",
                limit,
            )
            return [self._row_to_ticket(r) for r in rows]

    async def list_by_assignee(self, operator_id: UUID, limit: int = 100) -> List[SupportTicket]:
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT * FROM support_tickets WHERE assignee_user_id=$1 AND status='in_progress' ORDER BY created_at DESC LIMIT $2",
                operator_id, limit,
            )
            return [self._row_to_ticket(r) for r in rows]

    async def take(self, ticket_id: UUID, operator_id: UUID) -> Optional[SupportTicket]:
        """Atomic CAS: set assignee if NULL. Returns ticket on success, None if already taken."""
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                UPDATE support_tickets
                SET assignee_user_id=$1, status='in_progress', updated_at=NOW()
                WHERE id=$2 AND assignee_user_id IS NULL AND status='open'
                RETURNING *
                """,
                operator_id, ticket_id,
            )
            return self._row_to_ticket(row) if row else None

    async def set_ai_first_response(self, ticket_id: UUID) -> None:
        async with self.pool.acquire() as conn:
            await conn.execute(
                "UPDATE support_tickets SET ai_first_response_at=NOW(), updated_at=NOW() WHERE id=$1 AND ai_first_response_at IS NULL",
                ticket_id,
            )

    async def set_ai_handoff(self, ticket_id: UUID) -> None:
        async with self.pool.acquire() as conn:
            await conn.execute(
                "UPDATE support_tickets SET ai_handoff_at=NOW(), updated_at=NOW() WHERE id=$1 AND ai_handoff_at IS NULL",
                ticket_id,
            )

    async def close(self, ticket_id: UUID, by_user_id: UUID, by_role: ClosedByRole) -> Optional[SupportTicket]:
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                UPDATE support_tickets
                SET status='closed', closed_at=NOW(), closed_by_user_id=$1, closed_by_role=$2, updated_at=NOW()
                WHERE id=$3 AND status IN ('open', 'in_progress')
                RETURNING *
                """,
                by_user_id, by_role.value, ticket_id,
            )
            return self._row_to_ticket(row) if row else None

    async def reopen(self, ticket_id: UUID) -> Optional[SupportTicket]:
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                UPDATE support_tickets
                SET status='in_progress', closed_at=NULL, closed_by_user_id=NULL, closed_by_role=NULL, updated_at=NOW()
                WHERE id=$1 AND status='closed'
                RETURNING *
                """,
                ticket_id,
            )
            return self._row_to_ticket(row) if row else None

    @staticmethod
    def _row_to_ticket(row) -> SupportTicket:
        if row is None:
            return None
        return SupportTicket(
            id=row["id"],
            requester_user_id=row["requester_user_id"],
            requester_org_id=row["requester_org_id"],
            category=SupportTicketCategory(row["category"]),
            status=SupportTicketStatus(row["status"]),
            assignee_user_id=row["assignee_user_id"],
            ai_handoff_at=row["ai_handoff_at"],
            ai_first_response_at=row["ai_first_response_at"],
            closed_at=row["closed_at"],
            closed_by_user_id=row["closed_by_user_id"],
            closed_by_role=ClosedByRole(row["closed_by_role"]) if row["closed_by_role"] else None,
            title=row["title"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )
```

- [ ] **Step 4: Run, expect pass (включая concurrent CAS test)**

Run: `pytest tests/test_support_ticket_storage.py -v`

- [ ] **Step 5: Commit**

`git add src/engine/storage/support_ticket_storage.py tests/test_support_ticket_storage.py && git commit -m "feat(engine): SupportTicketStorage with atomic CAS take"`

---

### Task 6: SupportTicketEventStorage

**Files:**
- Create: `src/engine/storage/support_ticket_event_storage.py`
- Test: `tests/test_support_ticket_event_storage.py`

- [ ] **Step 1: Failing test**

```python
import pytest
from uuid import uuid4
from src.engine.models.support_ticket_event import (
    SupportTicketEvent, SupportTicketEventType, SupportTicketActorRole
)
from src.engine.storage.support_ticket_event_storage import SupportTicketEventStorage

@pytest.mark.asyncio
async def test_insert_and_list(db_pool, seed_ticket, seed_user):
    storage = SupportTicketEventStorage(db_pool)
    e = SupportTicketEvent(
        ticket_id=seed_ticket.id,
        actor_user_id=seed_user.id,
        actor_role=SupportTicketActorRole.REQUESTER,
        event_type=SupportTicketEventType.CREATED,
        payload={"category": "how_to"},
    )
    await storage.insert(e)
    events = await storage.list_by_ticket(seed_ticket.id)
    assert len(events) == 1
    assert events[0].event_type == SupportTicketEventType.CREATED
```

- [ ] **Step 2: Run, expect fail**

- [ ] **Step 3: Implement**

```python
# src/engine/storage/support_ticket_event_storage.py
from typing import List
from uuid import UUID
from src.engine.models.support_ticket_event import (
    SupportTicketEvent, SupportTicketEventType, SupportTicketActorRole
)
from src.engine.storage.base import BaseStorage


class SupportTicketEventStorage(BaseStorage):

    async def insert(self, e: SupportTicketEvent) -> SupportTicketEvent:
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                INSERT INTO support_ticket_events
                  (id, ticket_id, actor_user_id, actor_role, event_type, payload, created_at)
                VALUES ($1,$2,$3,$4,$5,$6,$7) RETURNING *
                """,
                e.id, e.ticket_id, e.actor_user_id,
                e.actor_role.value, e.event_type.value,
                e.payload, e.created_at,
            )
            return self._row_to_event(row)

    async def list_by_ticket(self, ticket_id: UUID, limit: int = 200) -> List[SupportTicketEvent]:
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT * FROM support_ticket_events WHERE ticket_id=$1 ORDER BY created_at DESC LIMIT $2",
                ticket_id, limit,
            )
            return [self._row_to_event(r) for r in rows]

    @staticmethod
    def _row_to_event(row) -> SupportTicketEvent:
        return SupportTicketEvent(
            id=row["id"],
            ticket_id=row["ticket_id"],
            actor_user_id=row["actor_user_id"],
            actor_role=SupportTicketActorRole(row["actor_role"]),
            event_type=SupportTicketEventType(row["event_type"]),
            payload=row["payload"] or {},
            created_at=row["created_at"],
        )
```

- [ ] **Step 4: Run, expect pass**

- [ ] **Step 5: Commit**

`git add src/engine/storage/support_ticket_event_storage.py tests/test_support_ticket_event_storage.py && git commit -m "feat(engine): SupportTicketEventStorage"`

---

### Task 7: Конфиг — RUGPT_SUPPORT_ORG_ID и SUPPORT_REOPEN_WINDOW_DAYS

**Files:**
- Modify: `src/engine/config.py`

- [ ] **Step 1: Прочитать существующий config.py**

Run: `grep -n "RUGPT_ORG_ID\|class Config\|SYSTEM_USER" src/engine/config.py | head -20`
Найти где объявлены аналогичные константы (например `RUGPT_SYSTEM_ORG_ID` или подобное).

- [ ] **Step 2: Добавить константы**

В `src/engine/config.py` рядом с существующими org/system константами добавить:

```python
# RuGPT Support — отдельная орг для операторов саппорта
RUGPT_SUPPORT_ORG_ID = UUID("00000001-0000-0000-0000-000000000000")

# Окно reopen для closed support тикетов через сообщение в чат
SUPPORT_REOPEN_WINDOW_DAYS = int(os.getenv("SUPPORT_REOPEN_WINDOW_DAYS", "7"))

# Username system user для AI первой линии саппорта
SUPPORT_AI_USERNAME = "support_ai"
```

(Если константы реально лежат в `Config` классе или в env-loader — следовать существующему паттерну.)

- [ ] **Step 3: Smoke test — импорт работает**

Run: `python -c "from src.engine.config import RUGPT_SUPPORT_ORG_ID, SUPPORT_REOPEN_WINDOW_DAYS, SUPPORT_AI_USERNAME; print(RUGPT_SUPPORT_ORG_ID, SUPPORT_REOPEN_WINDOW_DAYS)"`
Expected: печатает UUID + 7.

- [ ] **Step 4: Commit**

`git add src/engine/config.py && git commit -m "feat(engine): support config constants"`

---

### Task 8: Cross-org exemption в ChatStorage — RE-SCOPED (no code change)

**Files:**
- Verify only: `src/engine/storage/chat_storage.py` (no edits)
- Add: `tests/test_chat_storage_support_cross_org.py` (regression tests)

**Re-scope rationale:** план изначально предполагал, что `ChatStorage` имеет orgship-фильтры (`list_my_chats(user_id, org_id)`, `get_by_id(chat_id, org_id)`), которые надо ослабить для `chat.type='support'`. По факту:
- `ChatStorage.get_by_id(chat_id)` — `WHERE id = $1` без orgship.
- `ChatStorage.list_by_user(user_id, active_only, chat_type)` — фильтр только по `participants`, без orgship. Метода `list_my_chats` не существует.
- `ChatStorage.list_by_org(org_id, ...)` — есть, но это admin-style listing внутри одной орг, не пользовательский список чатов.

Таким образом, **storage-слой уже cross-org transparent**. Реальная orgship-проверка живёт в сервисном слое (`ChatService.can_user_access_chat`, Task 9).

**Steps:**

- [ ] **Step 1: Verify chat_storage.py is cross-org transparent**

Run: `grep -n "org_id\|list_by_user\|get_by_id" src/engine/storage/chat_storage.py | head -30`
Подтвердить: `get_by_id` без `org_id` в WHERE, `list_by_user` фильтрует только по `participants`.

- [ ] **Step 2: Add regression tests**

Создать `tests/test_chat_storage_support_cross_org.py` (см. файл) — три теста:
1. `test_get_by_id_returns_chat_regardless_of_caller_org` — get_by_id отдаёт support-чат независимо от орг вызывающего.
2. `test_list_by_user_returns_support_chat_for_operator_from_foreign_org` — оператор RuGPT Support видит cross-org support-чат через `list_by_user` (он в `participants`).
3. `test_list_by_user_does_NOT_return_chat_to_non_participant` — пользователь не в `participants` не видит чат, даже если в той же орг (storage участвует только как participant-filter).

Эти тесты — **regression guard**: упадут, если кто-нибудь добавит `WHERE org_id = $X` в `get_by_id` или `list_by_user`.

- [ ] **Step 3: Run tests**

Run: `pytest tests/test_chat_storage_support_cross_org.py -v`
Expected: 3 PASSED (или часть SKIPPED, если в dev DB нет нужных пользователей — это допустимо).

- [ ] **Step 4: Commit**

`git add tests/test_chat_storage_support_cross_org.py && git commit -m "test(engine): regression tests for ChatStorage cross-org transparency"`

---

### Task 9: ChatService.can_user_access_chat + cross-org regression suite

**Files:**
- Modify: `src/engine/services/chat_service.py`
- Create: `tests/test_cross_org_chat_isolation.py`

- [ ] **Step 1: Failing tests — главный security suite**

```python
# tests/test_cross_org_chat_isolation.py
import pytest
from src.engine.models.chat import ChatType
from src.engine.services.chat_service import ChatService

@pytest.mark.asyncio
async def test_support_chat_accessible_to_participant(
    chat_service, seed_support_chat, seed_user_in_chat
):
    ok = await chat_service.can_user_access_chat(seed_user_in_chat, seed_support_chat)
    assert ok is True

@pytest.mark.asyncio
async def test_support_chat_accessible_to_operator_in_queue(
    chat_service, seed_unassigned_support_chat, seed_operator_rugpt_support
):
    """Оператор RuGPT Support видит unassigned чат до взятия (queue)."""
    ok = await chat_service.can_user_access_chat(seed_operator_rugpt_support, seed_unassigned_support_chat)
    assert ok is True

@pytest.mark.asyncio
async def test_DIRECT_chat_NOT_accessible_cross_org(
    chat_service, seed_direct_chat_acme, seed_operator_rugpt_support
):
    """Регрессия: direct чат из чужой орг — 403 даже для оператора."""
    ok = await chat_service.can_user_access_chat(seed_operator_rugpt_support, seed_direct_chat_acme)
    assert ok is False

@pytest.mark.asyncio
async def test_TASK_chat_NOT_accessible_cross_org(
    chat_service, seed_task_chat_acme, seed_operator_rugpt_support
):
    ok = await chat_service.can_user_access_chat(seed_operator_rugpt_support, seed_task_chat_acme)
    assert ok is False

@pytest.mark.asyncio
async def test_PROJECT_chat_NOT_accessible_cross_org(
    chat_service, seed_project_chat_acme, seed_operator_rugpt_support
):
    ok = await chat_service.can_user_access_chat(seed_operator_rugpt_support, seed_project_chat_acme)
    assert ok is False
```

- [ ] **Step 2: Run, expect fail**

- [ ] **Step 3: Implement `can_user_access_chat` в `ChatService`**

```python
# src/engine/services/chat_service.py
from src.engine.config import RUGPT_SUPPORT_ORG_ID
from src.engine.models.chat import Chat, ChatType
from src.engine.models.user import User

class ChatService:
    # ... существующий код

    async def can_user_access_chat(self, user: User, chat: Chat) -> bool:
        """Единая точка проверки доступа к чату.

        ВАЖНО: support exemption строго привязан к chat.type == ChatType.SUPPORT.
        Никакого расширения на другие типы чатов.
        """
        # Support exemption — единственное cross-org правило
        if chat.type == ChatType.SUPPORT:
            if user.id in chat.participants:
                return True
            # Оператор RuGPT Support видит все support-чаты с привязанным тикетом (для очереди)
            if user.org_id == RUGPT_SUPPORT_ORG_ID and chat.support_ticket_id is not None:
                return True
            return False

        # Все остальные типы чатов — строгий orgship + participant
        if chat.org_id != user.org_id:
            return False
        return user.id in chat.participants
```

- [ ] **Step 4: Run tests, expect ALL 5 PASS (включая 3 регрессионных)**

Run: `pytest tests/test_cross_org_chat_isolation.py -v`

- [ ] **Step 5: Commit**

`git add src/engine/services/chat_service.py tests/test_cross_org_chat_isolation.py && git commit -m "feat(engine): ChatService.can_user_access_chat with support exemption"`

---

### Task 10: SupportTicketService — create / escalate / take / close / reopen

**Files:**
- Create: `src/engine/services/support_ticket_service.py`
- Test: `tests/test_support_ticket_service.py`

- [ ] **Step 1: Failing tests — основные сценарии**

```python
# tests/test_support_ticket_service.py
import pytest
from src.engine.models.support_ticket import SupportTicketCategory, SupportTicketStatus

@pytest.mark.asyncio
async def test_create_how_to_adds_ai_to_participants(
    support_service, seed_user_acme, support_ai_user
):
    ticket, chat = await support_service.create_ticket(
        requester=seed_user_acme,
        category=SupportTicketCategory.HOW_TO,
        initial_message="как создать чат?",
    )
    assert ticket.category == SupportTicketCategory.HOW_TO
    assert ticket.ai_handoff_at is None
    assert support_ai_user.id in chat.participants

@pytest.mark.asyncio
async def test_create_bug_sets_ai_handoff_immediately(support_service, seed_user_acme):
    ticket, chat = await support_service.create_ticket(
        requester=seed_user_acme,
        category=SupportTicketCategory.BUG,
        initial_message="не работает",
    )
    assert ticket.ai_handoff_at is not None

@pytest.mark.asyncio
async def test_escalate_sets_ai_handoff(support_service, seed_how_to_ticket, seed_user_acme):
    updated = await support_service.escalate(seed_how_to_ticket.id, by_user=seed_user_acme)
    assert updated.ai_handoff_at is not None

@pytest.mark.asyncio
async def test_close_by_requester_sets_role(support_service, seed_open_ticket, seed_user_acme):
    closed = await support_service.close_ticket(seed_open_ticket.id, by_user=seed_user_acme)
    assert closed.status == SupportTicketStatus.CLOSED
    assert closed.closed_by_role.value == "requester"

@pytest.mark.asyncio
async def test_close_by_operator_sets_role(support_service, seed_assigned_ticket, seed_operator):
    closed = await support_service.close_ticket(seed_assigned_ticket.id, by_user=seed_operator)
    assert closed.closed_by_role.value == "operator"

@pytest.mark.asyncio
async def test_reopen_within_window_succeeds(
    support_service, seed_just_closed_ticket
):
    reopened = await support_service.reopen_if_within_window(seed_just_closed_ticket.id)
    assert reopened is not None
    assert reopened.status == SupportTicketStatus.IN_PROGRESS

@pytest.mark.asyncio
async def test_reopen_outside_window_fails(
    support_service, seed_long_ago_closed_ticket
):
    reopened = await support_service.reopen_if_within_window(seed_long_ago_closed_ticket.id)
    assert reopened is None
```

- [ ] **Step 2: Run, expect fail**

- [ ] **Step 3: Implement service**

```python
# src/engine/services/support_ticket_service.py
from datetime import datetime, timedelta
from typing import Optional, Tuple
from uuid import UUID

from src.engine.config import (
    RUGPT_SUPPORT_ORG_ID, SUPPORT_REOPEN_WINDOW_DAYS, SUPPORT_AI_USERNAME
)
from src.engine.models.chat import Chat, ChatType
from src.engine.models.support_ticket import (
    SupportTicket, SupportTicketCategory, SupportTicketStatus, ClosedByRole
)
from src.engine.models.support_ticket_event import (
    SupportTicketEvent, SupportTicketEventType, SupportTicketActorRole
)
from src.engine.models.user import User


class SupportTicketService:

    def __init__(
        self,
        ticket_storage,
        event_storage,
        chat_service,
        message_storage,
        user_storage,
        notification_service,  # SupportNotificationService — Task 11
    ):
        self.ticket_storage = ticket_storage
        self.event_storage = event_storage
        self.chat_service = chat_service
        self.message_storage = message_storage
        self.user_storage = user_storage
        self.notification_service = notification_service

    async def create_ticket(
        self,
        requester: User,
        category: SupportTicketCategory,
        initial_message: str,
    ) -> Tuple[SupportTicket, Chat]:
        # 1. Создать тикет
        title = (initial_message or "").strip().split("\n")[0][:200] or "(без темы)"
        now = datetime.utcnow()
        ticket = SupportTicket(
            requester_user_id=requester.id,
            requester_org_id=requester.org_id,
            category=category,
            status=SupportTicketStatus.OPEN,
            title=title,
            ai_handoff_at=now if category != SupportTicketCategory.HOW_TO else None,
        )
        await self.ticket_storage.create(ticket)

        # 2. Создать чат: org_id = requester_org_id
        participants = [requester.id]
        if category == SupportTicketCategory.HOW_TO:
            support_ai = await self.user_storage.get_by_username(SUPPORT_AI_USERNAME)
            participants.append(support_ai.id)

        chat = Chat(
            org_id=requester.org_id,
            type=ChatType.SUPPORT,
            participants=participants,
            created_by=requester.id,
            support_ticket_id=ticket.id,
        )
        chat = await self.chat_service.create_chat(chat)

        # 3. Сохранить первое сообщение клиента
        await self.message_storage.add_message(
            chat_id=chat.id,
            org_id=chat.org_id,
            sender_id=requester.id,
            content=initial_message,
        )

        # 4. Event
        await self._record_event(
            ticket.id, requester.id, SupportTicketActorRole.REQUESTER,
            SupportTicketEventType.CREATED, {"category": category.value}
        )

        # 5. Уведомления
        if category != SupportTicketCategory.HOW_TO:
            await self.notification_service.notify_new_in_queue(ticket)

        return ticket, chat

    async def escalate(self, ticket_id: UUID, by_user: User) -> SupportTicket:
        ticket = await self.ticket_storage.get_by_id(ticket_id)
        if ticket is None:
            raise ValueError("ticket not found")
        if ticket.requester_user_id != by_user.id:
            raise PermissionError("only requester can escalate")
        if ticket.ai_handoff_at is not None:
            return ticket  # idempotent
        await self.ticket_storage.set_ai_handoff(ticket_id)
        await self._record_event(
            ticket_id, by_user.id, SupportTicketActorRole.REQUESTER,
            SupportTicketEventType.AI_HANDOFF, {}
        )
        ticket = await self.ticket_storage.get_by_id(ticket_id)
        await self.notification_service.notify_new_in_queue(ticket)
        return ticket

    async def take_ticket(self, ticket_id: UUID, operator: User) -> SupportTicket:
        if operator.org_id != RUGPT_SUPPORT_ORG_ID:
            raise PermissionError("only RuGPT Support operators can take tickets")
        result = await self.ticket_storage.take(ticket_id, operator.id)
        if result is None:
            raise ValueError("ticket already taken or not in open state")
        # Добавить оператора в participants чата
        chat = await self.chat_service.get_by_support_ticket(ticket_id)
        await self.chat_service.add_participant(chat.id, operator.id)
        # System message в чат + audit
        await self._post_system_message(chat, "Оператор взял тикет в работу")
        await self._record_event(
            ticket_id, operator.id, SupportTicketActorRole.OPERATOR,
            SupportTicketEventType.TAKEN, {}
        )
        # In-app уведомление клиенту (имя оператора скрыто)
        await self.notification_service.notify_taken(result)
        return result

    async def close_ticket(self, ticket_id: UUID, by_user: User) -> SupportTicket:
        ticket = await self.ticket_storage.get_by_id(ticket_id)
        if ticket is None:
            raise ValueError("not found")
        # Определить роль закрывающего
        if by_user.id == ticket.requester_user_id:
            role = ClosedByRole.REQUESTER
        elif by_user.org_id == RUGPT_SUPPORT_ORG_ID:
            role = ClosedByRole.OPERATOR
        else:
            raise PermissionError("not authorized to close")
        closed = await self.ticket_storage.close(ticket_id, by_user.id, role)
        if closed is None:
            raise ValueError("already closed or invalid state")
        chat = await self.chat_service.get_by_support_ticket(ticket_id)
        msg = "Клиент закрыл тикет" if role == ClosedByRole.REQUESTER else "Оператор закрыл тикет"
        await self._post_system_message(chat, msg)
        await self._record_event(
            ticket_id, by_user.id,
            SupportTicketActorRole.REQUESTER if role == ClosedByRole.REQUESTER else SupportTicketActorRole.OPERATOR,
            SupportTicketEventType.CLOSED, {"by_role": role.value}
        )
        await self.notification_service.notify_closed(closed)
        return closed

    async def reopen_if_within_window(self, ticket_id: UUID) -> Optional[SupportTicket]:
        ticket = await self.ticket_storage.get_by_id(ticket_id)
        if ticket is None or ticket.status != SupportTicketStatus.CLOSED:
            return None
        if ticket.closed_at is None:
            return None
        deadline = ticket.closed_at + timedelta(days=SUPPORT_REOPEN_WINDOW_DAYS)
        if datetime.utcnow() > deadline:
            return None
        reopened = await self.ticket_storage.reopen(ticket_id)
        if reopened:
            chat = await self.chat_service.get_by_support_ticket(ticket_id)
            await self._post_system_message(chat, "Тикет переоткрыт")
            await self._record_event(
                ticket_id, ticket.requester_user_id, SupportTicketActorRole.SYSTEM,
                SupportTicketEventType.REOPENED, {}
            )
            await self.notification_service.notify_reopened(reopened)
        return reopened

    async def _record_event(self, ticket_id, actor_id, actor_role, event_type, payload):
        e = SupportTicketEvent(
            ticket_id=ticket_id,
            actor_user_id=actor_id,
            actor_role=actor_role,
            event_type=event_type,
            payload=payload,
        )
        await self.event_storage.insert(e)

    async def _post_system_message(self, chat, content: str):
        # Использует тот же механизм что task_events → системное сообщение в чат.
        # На имплементации проверить как это делает task_event_service / chat_service
        # и переиспользовать sender_type. Если нет — создать общий примитив.
        await self.chat_service.post_system_message(chat.id, content)
```

- [ ] **Step 4: Run tests**

Run: `pytest tests/test_support_ticket_service.py -v`
Expected: PASS все 7.

- [ ] **Step 5: Commit**

`git add src/engine/services/support_ticket_service.py tests/test_support_ticket_service.py && git commit -m "feat(engine): SupportTicketService"`

---

### Task 11: SupportNotificationService — fan-out операторам

**Files:**
- Create: `src/engine/services/support_notification_service.py`
- Test: `tests/test_support_notification_service.py`

- [ ] **Step 1: Failing test — fan-out на всех активных операторов**

```python
# tests/test_support_notification_service.py
import pytest
from src.engine.services.support_notification_service import SupportNotificationService

@pytest.mark.asyncio
async def test_notify_new_in_queue_creates_notifications_for_all_operators(
    db_pool, in_app_storage, seed_ticket_in_queue, seed_three_operators
):
    svc = SupportNotificationService(
        in_app_notification_service=in_app_storage,
        user_storage=...,  # реальный или мок
    )
    await svc.notify_new_in_queue(seed_ticket_in_queue)
    # У каждого оператора должно появиться in-app уведомление
    for op in seed_three_operators:
        notifs = await in_app_storage.list_by_user(op.id)
        assert any(n.notification_type == "support_ticket_new" for n in notifs)
```

- [ ] **Step 2: Run, expect fail**

- [ ] **Step 3: Implement**

```python
# src/engine/services/support_notification_service.py
from src.engine.config import RUGPT_SUPPORT_ORG_ID
from src.engine.models.support_ticket import SupportTicket


class SupportNotificationService:

    def __init__(self, in_app_notification_service, user_storage):
        self.in_app = in_app_notification_service
        self.user_storage = user_storage

    async def notify_new_in_queue(self, ticket: SupportTicket) -> None:
        operators = await self.user_storage.list_active_by_org(RUGPT_SUPPORT_ORG_ID)
        for op in operators:
            await self.in_app.create(
                user_id=op.id,
                notification_type="support_ticket_new",
                title="Новый тикет в очереди",
                message=f"Категория: {ticket.category.value}",
                payload={"ticket_id": str(ticket.id), "category": ticket.category.value},
            )

    async def notify_taken(self, ticket: SupportTicket) -> None:
        await self.in_app.create(
            user_id=ticket.requester_user_id,
            notification_type="support_ticket_taken",
            title="Тех. поддержка взяла ваш тикет в работу",
            message="",
            payload={"ticket_id": str(ticket.id)},
        )

    async def notify_closed(self, ticket: SupportTicket) -> None:
        # Уведомление "другой стороне" (не той кто закрыл)
        if ticket.closed_by_role and ticket.closed_by_role.value == "operator":
            target = ticket.requester_user_id
        elif ticket.assignee_user_id:
            target = ticket.assignee_user_id
        else:
            return
        await self.in_app.create(
            user_id=target,
            notification_type="support_ticket_closed",
            title="Тикет закрыт",
            message="",
            payload={"ticket_id": str(ticket.id)},
        )

    async def notify_reopened(self, ticket: SupportTicket) -> None:
        if ticket.assignee_user_id is None:
            return
        await self.in_app.create(
            user_id=ticket.assignee_user_id,
            notification_type="support_ticket_reopened",
            title="Тикет переоткрыт клиентом",
            message="",
            payload={"ticket_id": str(ticket.id)},
        )
```

- [ ] **Step 4: Run, expect pass**

- [ ] **Step 5: Commit**

`git add src/engine/services/support_notification_service.py tests/test_support_notification_service.py && git commit -m "feat(engine): SupportNotificationService fan-out"`

---

### Task 12: AI первой линии — try_auto_respond hook для ChatType.SUPPORT

**Files:**
- Modify: `src/engine/services/ai_service.py`
- Test: `tests/test_ai_service_support.py`

- [ ] **Step 1: Прочитать существующий try_auto_respond**

Run: `grep -n "try_auto_respond\|ChatType\|system" src/engine/services/ai_service.py | head -30`
Зафиксировать как сейчас работает auto-respond для DIRECT чатов с system users.

- [ ] **Step 2: Failing tests**

```python
# tests/test_ai_service_support.py
import pytest
from src.engine.models.chat import ChatType
from src.engine.models.support_ticket import SupportTicketCategory

@pytest.mark.asyncio
async def test_try_auto_respond_triggers_for_how_to_without_handoff(
    ai_service, seed_how_to_chat_with_ai_in_participants, seed_user_message
):
    """AI должен ответить если category=how_to и ai_handoff_at=NULL."""
    response = await ai_service.try_auto_respond(
        chat=seed_how_to_chat_with_ai_in_participants,
        last_message=seed_user_message,
    )
    assert response is not None

@pytest.mark.asyncio
async def test_try_auto_respond_skips_after_handoff(
    ai_service, seed_how_to_chat_after_handoff, seed_user_message
):
    """После escalate AI не должен отвечать."""
    response = await ai_service.try_auto_respond(
        chat=seed_how_to_chat_after_handoff,
        last_message=seed_user_message,
    )
    assert response is None

@pytest.mark.asyncio
async def test_try_auto_respond_skips_for_bug_category(
    ai_service, seed_bug_chat, seed_user_message
):
    """Категория bug — AI вообще не подключается (ai_handoff_at сразу stamped)."""
    response = await ai_service.try_auto_respond(
        chat=seed_bug_chat,
        last_message=seed_user_message,
    )
    assert response is None

@pytest.mark.asyncio
async def test_first_response_stamps_ai_first_response_at(
    ai_service, ticket_storage, seed_how_to_chat_with_ai_in_participants
):
    chat = seed_how_to_chat_with_ai_in_participants
    await ai_service.try_auto_respond(chat=chat, last_message=...)
    ticket = await ticket_storage.get_by_id(chat.support_ticket_id)
    assert ticket.ai_first_response_at is not None
```

- [ ] **Step 3: Modify `try_auto_respond`**

В существующий метод добавить ветку для `ChatType.SUPPORT`:

```python
# src/engine/services/ai_service.py (внутри try_auto_respond)
async def try_auto_respond(self, chat, last_message):
    # ... существующий код для DIRECT с system users

    if chat.type == ChatType.SUPPORT:
        return await self._try_support_auto_respond(chat, last_message)

    # ... остаток существующего

async def _try_support_auto_respond(self, chat, last_message):
    """AI первая линия для саппорт-тикетов категории how_to."""
    if chat.support_ticket_id is None:
        return None
    ticket = await self.ticket_storage.get_by_id(chat.support_ticket_id)
    if ticket is None:
        return None
    if ticket.category != SupportTicketCategory.HOW_TO:
        return None
    if ticket.ai_handoff_at is not None:
        return None
    support_ai = await self.user_storage.get_by_username(SUPPORT_AI_USERNAME)
    if support_ai.id not in chat.participants:
        return None
    if last_message.sender_id == support_ai.id:
        return None  # не отвечаем на собственное сообщение

    # Запустить через существующий generate_response с ролью support_assistant
    response = await self.generate_response(
        chat=chat,
        as_user=support_ai,
        triggered_by_message=last_message,
    )
    # Stamp ai_first_response_at (idempotent)
    await self.ticket_storage.set_ai_first_response(ticket.id)
    # Audit event
    await self.event_storage.insert(SupportTicketEvent(
        ticket_id=ticket.id,
        actor_user_id=support_ai.id,
        actor_role=SupportTicketActorRole.AI,
        event_type=SupportTicketEventType.AI_RESPONDED,
        payload={"message_id": str(response.id) if response else None},
    ))
    return response
```

(Точное место вызова `try_auto_respond` — обычно после сохранения user-сообщения в `chat_service.add_message` или в Kafka handler. Не менять триггер, только тело.)

- [ ] **Step 4: Run tests, expect pass**

Run: `pytest tests/test_ai_service_support.py -v`

- [ ] **Step 5: Commit**

`git add src/engine/services/ai_service.py tests/test_ai_service_support.py && git commit -m "feat(engine): AI first-line for support how_to tickets"`

---

### Task 13: ChatService.add_message — auto-reopen hook

**Files:**
- Modify: `src/engine/services/chat_service.py`
- Test: `tests/test_chat_service_support_reopen.py`

- [ ] **Step 1: Failing tests**

```python
# tests/test_chat_service_support_reopen.py
import pytest
from datetime import datetime, timedelta
from freezegun import freeze_time
from src.engine.models.support_ticket import SupportTicketStatus

@pytest.mark.asyncio
async def test_message_in_window_reopens_ticket(
    chat_service, ticket_storage, seed_just_closed_support_chat, seed_user_acme
):
    chat = seed_just_closed_support_chat
    msg = await chat_service.add_message(
        chat_id=chat.id, sender=seed_user_acme, content="ой ещё вопрос"
    )
    ticket = await ticket_storage.get_by_id(chat.support_ticket_id)
    assert ticket.status == SupportTicketStatus.IN_PROGRESS
    assert ticket.closed_at is None

@pytest.mark.asyncio
async def test_message_outside_window_returns_403(
    chat_service, seed_long_ago_closed_support_chat, seed_user_acme
):
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as exc:
        await chat_service.add_message(
            chat_id=seed_long_ago_closed_support_chat.id,
            sender=seed_user_acme, content="запоздалый вопрос"
        )
    assert exc.value.status_code == 403

@pytest.mark.asyncio
async def test_message_in_non_support_closed_chat_unaffected(
    chat_service, seed_direct_chat_active, seed_user_acme
):
    """Регрессия: hook не лезет в не-SUPPORT чаты."""
    msg = await chat_service.add_message(
        chat_id=seed_direct_chat_active.id, sender=seed_user_acme, content="hi"
    )
    assert msg is not None
```

- [ ] **Step 2: Modify `ChatService.add_message`**

В начало метода после загрузки чата добавить hook:

```python
# src/engine/services/chat_service.py
from fastapi import HTTPException
from src.engine.models.chat import ChatType

async def add_message(self, chat_id, sender, content, **kwargs):
    chat = await self.chat_storage.get_by_id(chat_id)
    if chat is None:
        raise HTTPException(404, "chat not found")

    # Support-чат: проверка reopen окна
    if chat.type == ChatType.SUPPORT and chat.support_ticket_id:
        reopened = await self.support_ticket_service.reopen_if_within_window(chat.support_ticket_id)
        if reopened is None:
            ticket = await self.support_ticket_service.ticket_storage.get_by_id(chat.support_ticket_id)
            if ticket and ticket.status == SupportTicketStatus.CLOSED:
                raise HTTPException(403, "Тикет архивирован, создайте новый")

    # ... существующий код добавления сообщения
```

⚠️ Циклическая зависимость `ChatService` ↔ `SupportTicketService`: оба ссылаются друг на друга. Решение — `SupportTicketService` инжектится в `ChatService` через setter (lazy bind в `EngineService.__init__`).

- [ ] **Step 3: Run tests, expect pass**

Run: `pytest tests/test_chat_service_support_reopen.py -v`

- [ ] **Step 4: Commit**

`git add src/engine/services/chat_service.py tests/test_chat_service_support_reopen.py && git commit -m "feat(engine): support ticket auto-reopen via message in window"`

---

### Task 14: Routes — клиентские endpoints (POST /tickets, GET /tickets/my, GET /tickets/{id}, escalate, close)

**Files:**
- Create: `src/engine/routes/support.py`
- Test: `tests/test_support_routes_client.py`

- [ ] **Step 1: Failing test для POST /tickets**

```python
# tests/test_support_routes_client.py
import pytest
from httpx import AsyncClient

@pytest.mark.asyncio
async def test_create_ticket_how_to(client: AsyncClient, auth_headers_user_acme):
    r = await client.post(
        "/api/v1/support/tickets",
        headers=auth_headers_user_acme,
        json={"category": "how_to", "initial_message": "как создать чат?"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["ticket"]["category"] == "how_to"
    assert body["ticket"]["status"] == "open"
    assert "chat_id" in body

@pytest.mark.asyncio
async def test_create_ticket_bug(client, auth_headers_user_acme):
    r = await client.post(
        "/api/v1/support/tickets",
        headers=auth_headers_user_acme,
        json={"category": "bug", "initial_message": "не открывается"},
    )
    assert r.status_code == 200
    assert r.json()["ticket"]["ai_handoff_at"] is not None

@pytest.mark.asyncio
async def test_get_my_tickets(client, auth_headers_user_acme, seed_two_tickets_for_user):
    r = await client.get("/api/v1/support/tickets/my", headers=auth_headers_user_acme)
    assert r.status_code == 200
    assert len(r.json()) == 2

@pytest.mark.asyncio
async def test_get_ticket_includes_requester_info_for_operator(
    client, auth_headers_operator, seed_ticket_acme
):
    r = await client.get(f"/api/v1/support/tickets/{seed_ticket_acme.id}", headers=auth_headers_operator)
    assert r.status_code == 200
    body = r.json()
    assert "requester_name" in body
    assert "requester_org_name" in body

@pytest.mark.asyncio
async def test_get_ticket_does_NOT_include_requester_info_for_other_user(
    client, auth_headers_user_acme, seed_ticket_acme
):
    r = await client.get(f"/api/v1/support/tickets/{seed_ticket_acme.id}", headers=auth_headers_user_acme)
    assert r.status_code == 200
    body = r.json()
    # клиент сам видит свои данные но не как "requester_*" поля для оператора
    assert "requester_name" not in body or body.get("requester_name") is None

@pytest.mark.asyncio
async def test_escalate(client, auth_headers_user_acme, seed_how_to_ticket_acme):
    r = await client.post(
        f"/api/v1/support/tickets/{seed_how_to_ticket_acme.id}/escalate",
        headers=auth_headers_user_acme,
    )
    assert r.status_code == 200
    assert r.json()["ai_handoff_at"] is not None

@pytest.mark.asyncio
async def test_close_by_requester(client, auth_headers_user_acme, seed_open_ticket_acme):
    r = await client.post(
        f"/api/v1/support/tickets/{seed_open_ticket_acme.id}/close",
        headers=auth_headers_user_acme,
    )
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "closed"
    assert body["closed_by_role"] == "requester"
```

- [ ] **Step 2: Run, expect fail (no router)**

- [ ] **Step 3: Implement client routes**

```python
# src/engine/routes/support.py
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from typing import Optional
from uuid import UUID

from src.engine.services.engine_service import EngineService
from src.engine.routes.dependencies import get_engine, get_current_user

router = APIRouter(prefix="/api/v1/support", tags=["support"])


class CreateTicketRequest(BaseModel):
    category: str  # how_to | bug | other
    initial_message: str


@router.post("/tickets")
async def create_ticket(
    body: CreateTicketRequest,
    user=Depends(get_current_user),
    engine: EngineService = Depends(get_engine),
):
    from src.engine.models.support_ticket import SupportTicketCategory
    try:
        cat = SupportTicketCategory(body.category)
    except ValueError:
        raise HTTPException(400, "invalid category")
    ticket, chat = await engine.support_ticket_service.create_ticket(
        requester=user,
        category=cat,
        initial_message=body.initial_message,
    )
    return {"ticket": ticket.to_dict(), "chat_id": str(chat.id)}


@router.get("/tickets/my")
async def get_my_tickets(
    status: Optional[str] = None,
    limit: int = 50,
    user=Depends(get_current_user),
    engine: EngineService = Depends(get_engine),
):
    from src.engine.models.support_ticket import SupportTicketStatus
    s = SupportTicketStatus(status) if status else None
    tickets = await engine.support_ticket_storage.list_by_requester(user.id, s, limit)
    return [t.to_dict() for t in tickets]


@router.get("/tickets/{ticket_id}")
async def get_ticket(
    ticket_id: UUID,
    user=Depends(get_current_user),
    engine: EngineService = Depends(get_engine),
):
    from src.engine.config import RUGPT_SUPPORT_ORG_ID
    ticket = await engine.support_ticket_storage.get_by_id(ticket_id)
    if ticket is None:
        raise HTTPException(404)
    is_requester = user.id == ticket.requester_user_id
    is_operator = user.org_id == RUGPT_SUPPORT_ORG_ID
    if not (is_requester or is_operator):
        raise HTTPException(403)
    payload = ticket.to_dict()
    if is_operator:
        # Встроить cross-org профиль клиента
        requester = await engine.user_storage.get_by_id(ticket.requester_user_id)
        org = await engine.org_storage.get_by_id(ticket.requester_org_id)
        payload["requester_name"] = requester.name
        payload["requester_email"] = requester.email
        payload["requester_org_name"] = org.name
        payload["requester_org_id"] = str(org.id)
    return payload


@router.post("/tickets/{ticket_id}/escalate")
async def escalate(
    ticket_id: UUID,
    user=Depends(get_current_user),
    engine: EngineService = Depends(get_engine),
):
    try:
        ticket = await engine.support_ticket_service.escalate(ticket_id, user)
    except PermissionError as e:
        raise HTTPException(403, str(e))
    except ValueError as e:
        raise HTTPException(404, str(e))
    return ticket.to_dict()


@router.post("/tickets/{ticket_id}/close")
async def close_ticket(
    ticket_id: UUID,
    user=Depends(get_current_user),
    engine: EngineService = Depends(get_engine),
):
    try:
        ticket = await engine.support_ticket_service.close_ticket(ticket_id, user)
    except PermissionError as e:
        raise HTTPException(403, str(e))
    except ValueError as e:
        raise HTTPException(400, str(e))
    return ticket.to_dict()
```

- [ ] **Step 4: Run client tests, expect pass**

Run: `pytest tests/test_support_routes_client.py -v`

- [ ] **Step 5: Commit**

`git add src/engine/routes/support.py tests/test_support_routes_client.py && git commit -m "feat(engine): support routes - client endpoints"`

---

### Task 15: Routes — операторские endpoints (queue, take, operator/my)

**Files:**
- Modify: `src/engine/routes/support.py` (продолжение)
- Test: `tests/test_support_routes_operator.py`

- [ ] **Step 1: Failing tests**

```python
# tests/test_support_routes_operator.py
import pytest

@pytest.mark.asyncio
async def test_queue_returns_only_unassigned_open(
    client, auth_headers_operator, seed_one_open_one_assigned_ticket
):
    r = await client.get("/api/v1/support/queue", headers=auth_headers_operator)
    assert r.status_code == 200
    tickets = r.json()
    assert len(tickets) == 1
    assert tickets[0]["status"] == "open"
    assert tickets[0]["assignee_user_id"] is None

@pytest.mark.asyncio
async def test_queue_forbidden_for_non_operator(client, auth_headers_user_acme):
    r = await client.get("/api/v1/support/queue", headers=auth_headers_user_acme)
    assert r.status_code == 403

@pytest.mark.asyncio
async def test_take_success(client, auth_headers_operator, seed_open_unassigned_ticket):
    r = await client.post(
        f"/api/v1/support/tickets/{seed_open_unassigned_ticket.id}/take",
        headers=auth_headers_operator,
    )
    assert r.status_code == 200
    assert r.json()["status"] == "in_progress"

@pytest.mark.asyncio
async def test_take_conflict_when_already_assigned(
    client, auth_headers_operator, seed_already_assigned_ticket
):
    r = await client.post(
        f"/api/v1/support/tickets/{seed_already_assigned_ticket.id}/take",
        headers=auth_headers_operator,
    )
    assert r.status_code == 409

@pytest.mark.asyncio
async def test_operator_my(client, auth_headers_operator, seed_assigned_to_operator):
    r = await client.get("/api/v1/support/operator/my", headers=auth_headers_operator)
    assert r.status_code == 200
    assert len(r.json()) >= 1
```

- [ ] **Step 2: Добавить endpoints в `routes/support.py`**

```python
@router.get("/queue")
async def get_queue(
    limit: int = 100,
    user=Depends(get_current_user),
    engine: EngineService = Depends(get_engine),
):
    from src.engine.config import RUGPT_SUPPORT_ORG_ID
    if user.org_id != RUGPT_SUPPORT_ORG_ID:
        raise HTTPException(403, "operator access only")
    tickets = await engine.support_ticket_storage.list_queue(limit)
    return [t.to_dict() for t in tickets]


@router.post("/tickets/{ticket_id}/take")
async def take_ticket(
    ticket_id: UUID,
    user=Depends(get_current_user),
    engine: EngineService = Depends(get_engine),
):
    try:
        ticket = await engine.support_ticket_service.take_ticket(ticket_id, user)
    except PermissionError as e:
        raise HTTPException(403, str(e))
    except ValueError as e:
        raise HTTPException(409, str(e))
    return ticket.to_dict()


@router.get("/operator/my")
async def get_operator_my(
    limit: int = 100,
    user=Depends(get_current_user),
    engine: EngineService = Depends(get_engine),
):
    from src.engine.config import RUGPT_SUPPORT_ORG_ID
    if user.org_id != RUGPT_SUPPORT_ORG_ID:
        raise HTTPException(403)
    tickets = await engine.support_ticket_storage.list_by_assignee(user.id, limit)
    return [t.to_dict() for t in tickets]
```

- [ ] **Step 3: Run tests, expect pass**

Run: `pytest tests/test_support_routes_operator.py -v`

- [ ] **Step 4: Commit**

`git add src/engine/routes/support.py tests/test_support_routes_operator.py && git commit -m "feat(engine): support routes - operator endpoints"`

---

### Task 16: Routes — общие (GET /tickets/{id}/chat, GET /tickets/{id}/events)

**Files:**
- Modify: `src/engine/routes/support.py`
- Test: `tests/test_support_routes_common.py`

- [ ] **Step 1: Failing tests**

```python
@pytest.mark.asyncio
async def test_get_chat_id_for_ticket(client, auth_headers_user_acme, seed_ticket_with_chat):
    r = await client.get(
        f"/api/v1/support/tickets/{seed_ticket_with_chat.id}/chat",
        headers=auth_headers_user_acme,
    )
    assert r.status_code == 200
    assert r.json()["chat_id"]

@pytest.mark.asyncio
async def test_get_events(client, auth_headers_user_acme, seed_ticket_with_events):
    r = await client.get(
        f"/api/v1/support/tickets/{seed_ticket_with_events.id}/events",
        headers=auth_headers_user_acme,
    )
    assert r.status_code == 200
    events = r.json()
    assert any(e["event_type"] == "created" for e in events)
```

- [ ] **Step 2: Implement**

```python
@router.get("/tickets/{ticket_id}/chat")
async def get_ticket_chat(
    ticket_id: UUID,
    user=Depends(get_current_user),
    engine: EngineService = Depends(get_engine),
):
    chat = await engine.chat_service.get_by_support_ticket(ticket_id)
    if chat is None:
        raise HTTPException(404)
    if not await engine.chat_service.can_user_access_chat(user, chat):
        raise HTTPException(403)
    return {"chat_id": str(chat.id)}


@router.get("/tickets/{ticket_id}/events")
async def get_ticket_events(
    ticket_id: UUID,
    limit: int = 200,
    user=Depends(get_current_user),
    engine: EngineService = Depends(get_engine),
):
    from src.engine.config import RUGPT_SUPPORT_ORG_ID
    ticket = await engine.support_ticket_storage.get_by_id(ticket_id)
    if ticket is None:
        raise HTTPException(404)
    is_requester = user.id == ticket.requester_user_id
    is_operator = user.org_id == RUGPT_SUPPORT_ORG_ID
    if not (is_requester or is_operator):
        raise HTTPException(403)
    events = await engine.support_ticket_event_storage.list_by_ticket(ticket_id, limit)
    return [e.to_dict() for e in events]
```

- [ ] **Step 3: Run tests, expect pass**

- [ ] **Step 4: Commit**

`git add src/engine/routes/support.py tests/test_support_routes_common.py && git commit -m "feat(engine): support routes - chat and events"`

---

### Task 17: Prompt support_assistant.md

**Files:**
- Create: `src/engine/prompts/support_assistant.md`

- [ ] **Step 1: Создать файл**

```markdown
# Support Assistant

Ты — AI-ассистент тех. поддержки RuGPT. Отвечаешь на вопросы по использованию продукта.

## Что ты делаешь

- Отвечаешь кратко, по делу, без воды.
- Объясняешь как пользоваться функциями RuGPT: чаты, упоминания (@username, @@username), задачи, проекты, календарь, RAG, файлы.
- Если вопрос не про использование продукта (баг, претензия, фича-реквест) — рекомендуешь нажать «Позвать оператора» в шапке чата.

## Чего ты НЕ делаешь

- Не выдаёшь информацию про инфраструктуру / админ-функции / другие организации.
- Не обсуждаешь модели LLM, цены, внутренние процессы команды RuGPT.
- Не извиняешься без необходимости, не льёшь воду.
- Не ставишь подписи / приветствия в каждом сообщении — только когда уместно.

## Стиль

Простой русский. Короткие предложения. Если можно одной строкой — одной строкой.
Если несколько шагов — нумерованный список.
Технические детали без жаргона если контекст этого не требует.
```

- [ ] **Step 2: Smoke test — файл читается prompt cache'ем**

Run: `python -c "from src.engine.services.prompt_cache import get_prompt; print(get_prompt('support_assistant.md')[:100])"`
Expected: печатает первые 100 символов промпта.

- [ ] **Step 3: Commit**

`git add src/engine/prompts/support_assistant.md && git commit -m "feat(engine): support_assistant prompt"`

---

### Task 18: MessageStorage — re-scoped (no code change)

**Реальность после прочтения `src/engine/storage/message_storage.py`:** никакого orgship-фильтра в storage нет и быть не может — таблица `messages` (миграция 001) **не имеет** колонки `org_id`. Все методы (`create`, `get_by_id`, `list_by_chat`, `list_pending_review`, `update`, `validate`, `reject`, `delete`, `count_by_chat`) фильтруют только по `chat_id` / `id` / `sender_id`. Orgship — забота сервис/route-слоя через `ChatService.can_user_access_chat` (Task 9).

**Что сделано:**

- [x] Подтверждена прозрачность: код прочитан end-to-end, ни одна WHERE-секция не ссылается на `org_id` или `chats.org_id`.
- [x] Регрессионные тесты: `tests/test_message_storage_support_cross_org.py` (2 теста) — проверяют, что `list_by_chat` отдаёт сообщения вне зависимости от org звонящего, и что `create()` принимает сообщение от оператора чужой орг в `SUPPORT`-чат без жалоб. Если кто-то добавит orgship-WHERE или JOIN на `chats.org_id`, тесты упадут.
- [x] Spec обновлён (см. design doc 5.1, пункт 3).

**Изменений в продуктивном коде нет.**

---

### Task 19: Интеграция в EngineService и app.py

**Files:**
- Modify: `src/engine/services/engine_service.py`
- Modify: `src/engine/app.py`

- [ ] **Step 1: Прочитать engine_service.py**

Run: `grep -n "self\.task_\|self\.notification\|def __init__\|def initialize" src/engine/services/engine_service.py | head -40`
Найти паттерн инициализации сервисов.

- [ ] **Step 2: Добавить support-сервисы в EngineService**

```python
# src/engine/services/engine_service.py
from src.engine.storage.support_ticket_storage import SupportTicketStorage
from src.engine.storage.support_ticket_event_storage import SupportTicketEventStorage
from src.engine.services.support_ticket_service import SupportTicketService
from src.engine.services.support_notification_service import SupportNotificationService

class EngineService:
    async def initialize(self, ...):
        # ... существующее
        self.support_ticket_storage = SupportTicketStorage(self.db_pool)
        self.support_ticket_event_storage = SupportTicketEventStorage(self.db_pool)
        self.support_notification_service = SupportNotificationService(
            in_app_notification_service=self.in_app_notification_service,
            user_storage=self.user_storage,
        )
        self.support_ticket_service = SupportTicketService(
            ticket_storage=self.support_ticket_storage,
            event_storage=self.support_ticket_event_storage,
            chat_service=self.chat_service,
            message_storage=self.message_storage,
            user_storage=self.user_storage,
            notification_service=self.support_notification_service,
        )
        # Циклическая инжекция (Task 13)
        self.chat_service.support_ticket_service = self.support_ticket_service
        # Также нужно подкинуть ticket_storage в ai_service (Task 12)
        self.ai_service.ticket_storage = self.support_ticket_storage
        self.ai_service.event_storage = self.support_ticket_event_storage
```

- [ ] **Step 3: Подключить router в app.py**

```python
# src/engine/app.py
from src.engine.routes import support as support_routes

app.include_router(support_routes.router)
```

- [ ] **Step 4: Smoke test — Engine стартует**

Run: `cd /root/rugpt && source venv/bin/activate && uvicorn src.engine.app:app --port 8100 --host 127.0.0.1 &`
Wait 3 sec.
Run: `curl -s http://127.0.0.1:8100/health` 
Expected: `{"status":"healthy"...}`
Run: `curl -s http://127.0.0.1:8100/openapi.json | python -c "import sys,json; d=json.load(sys.stdin); print([p for p in d['paths'].keys() if 'support' in p])"`
Expected: список поддерживаемых support endpoints.
Run: `pkill -f "uvicorn src.engine.app"`

- [ ] **Step 5: Commit**

`git add src/engine/services/engine_service.py src/engine/app.py && git commit -m "feat(engine): wire support services and routes"`

---

## Phase 2 — WebClient Backend

### Task 20: TypeScript типы в shared common package

**Files:**
- Modify: `packages/common/src/types/chat.ts`
- Create: `packages/common/src/types/support.ts`
- Modify: `packages/common/src/index.ts`

- [ ] **Step 1: Расширить ChatType enum**

```typescript
// packages/common/src/types/chat.ts
export enum ChatType {
  DIRECT = 'direct',
  TASK = 'task',
  PROJECT = 'project',
  SUPPORT = 'support',  // новое
}

export interface Chat {
  // ... существующие поля
  supportTicketId?: string | null;  // новое
}
```

- [ ] **Step 2: Создать support типы**

```typescript
// packages/common/src/types/support.ts
export enum SupportTicketCategory {
  HOW_TO = 'how_to',
  BUG = 'bug',
  OTHER = 'other',
}

export enum SupportTicketStatus {
  OPEN = 'open',
  IN_PROGRESS = 'in_progress',
  CLOSED = 'closed',
}

export type ClosedByRole = 'requester' | 'operator';

export interface SupportTicket {
  id: string;
  requesterUserId: string;
  requesterOrgId: string;
  category: SupportTicketCategory;
  status: SupportTicketStatus;
  assigneeUserId: string | null;
  aiHandoffAt: string | null;
  aiFirstResponseAt: string | null;
  closedAt: string | null;
  closedByUserId: string | null;
  closedByRole: ClosedByRole | null;
  title: string | null;
  createdAt: string;
  updatedAt: string;
  // Для оператора (опциональные cross-org поля)
  requesterName?: string;
  requesterEmail?: string;
  requesterOrgName?: string;
}

export type SupportTicketEventType =
  | 'created' | 'ai_responded' | 'ai_handoff'
  | 'taken' | 'closed' | 'reopened' | 'message';

export type SupportTicketActorRole = 'requester' | 'operator' | 'ai' | 'system';

export interface SupportTicketEvent {
  id: string;
  ticketId: string;
  actorUserId: string;
  actorRole: SupportTicketActorRole;
  eventType: SupportTicketEventType;
  payload: Record<string, unknown>;
  createdAt: string;
}

export const RUGPT_SUPPORT_ORG_ID = '00000001-0000-0000-0000-000000000000';
```

- [ ] **Step 3: Экспортировать**

```typescript
// packages/common/src/index.ts
export * from './types/support';
```

- [ ] **Step 4: Build common package**

Run: `cd /root/webclient_rugpt/packages/common && npm run build`
Expected: success.

- [ ] **Step 5: Commit**

`git add packages/common/src/types/support.ts packages/common/src/types/chat.ts packages/common/src/index.ts && git commit -m "feat(common): support ticket types"`

---

### Task 21: RuGPTEngineAdapter — support команды

**Files:**
- Modify: `packages/backend/src/engine/adapters/rugpt.adapter.ts`
- Test: `packages/backend/test/rugpt-adapter-support.spec.ts`

- [ ] **Step 1: Прочитать существующий адаптер**

Run: `grep -n "case '\|EngineCommand\|switch" packages/backend/src/engine/adapters/rugpt.adapter.ts | head -40`
Зафиксировать паттерн команд.

- [ ] **Step 2: Добавить cases в `execute(command, payload)` switch**

```typescript
// packages/backend/src/engine/adapters/rugpt.adapter.ts
case 'support_create_ticket':
  return this.request('POST', '/api/v1/web/support/tickets', payload, headers);

case 'support_get_my_tickets':
  return this.request('GET', '/api/v1/web/support/tickets/my', payload, headers);

case 'support_get_ticket':
  return this.request('GET', `/api/v1/web/support/tickets/${payload.ticket_id}`, {}, headers);

case 'support_escalate':
  return this.request('POST', `/api/v1/web/support/tickets/${payload.ticket_id}/escalate`, {}, headers);

case 'support_close':
  return this.request('POST', `/api/v1/web/support/tickets/${payload.ticket_id}/close`, {}, headers);

case 'support_queue':
  return this.request('GET', '/api/v1/web/support/queue', payload, headers);

case 'support_take':
  return this.request('POST', `/api/v1/web/support/tickets/${payload.ticket_id}/take`, {}, headers);

case 'support_operator_my':
  return this.request('GET', '/api/v1/web/support/operator/my', payload, headers);

case 'support_get_chat':
  return this.request('GET', `/api/v1/web/support/tickets/${payload.ticket_id}/chat`, {}, headers);

case 'support_get_events':
  return this.request('GET', `/api/v1/web/support/tickets/${payload.ticket_id}/events`, payload, headers);
```

- [ ] **Step 3: Smoke test**

```typescript
// packages/backend/test/rugpt-adapter-support.spec.ts
describe('RuGPTEngineAdapter - support commands', () => {
  it('routes support_create_ticket to POST /support/tickets', async () => {
    const adapter = new RuGPTEngineAdapter(/* mocked http */);
    // verify URL + method
  });
});
```

- [ ] **Step 4: Run, expect pass**

- [ ] **Step 5: Commit**

`git add packages/backend/src/engine/adapters/rugpt.adapter.ts packages/backend/test/rugpt-adapter-support.spec.ts && git commit -m "feat(backend): support commands in engine adapter"`

---

### Task 22: SupportModule + SupportController + SupportService

**Files:**
- Create: `packages/backend/src/support/support.module.ts`
- Create: `packages/backend/src/support/support.controller.ts`
- Create: `packages/backend/src/support/support.service.ts`
- Create: `packages/backend/src/support/dto/create-ticket.dto.ts`
- Modify: `packages/backend/src/app.module.ts`
- Test: `packages/backend/test/support.controller.spec.ts`

- [ ] **Step 1: SupportService**

```typescript
// packages/backend/src/support/support.service.ts
import { Injectable } from '@nestjs/common';
import { EngineService } from '../engine/engine.service';

@Injectable()
export class SupportService {
  constructor(private readonly engine: EngineService) {}

  async createTicket(userHeaders: Record<string, string>, body: { category: string; initial_message: string }) {
    return this.engine.execute('support_create_ticket', body, userHeaders);
  }
  async getMyTickets(headers, query) { return this.engine.execute('support_get_my_tickets', query, headers); }
  async getTicket(headers, ticketId) { return this.engine.execute('support_get_ticket', { ticket_id: ticketId }, headers); }
  async escalate(headers, ticketId) { return this.engine.execute('support_escalate', { ticket_id: ticketId }, headers); }
  async close(headers, ticketId) { return this.engine.execute('support_close', { ticket_id: ticketId }, headers); }
  async getQueue(headers, query) { return this.engine.execute('support_queue', query, headers); }
  async take(headers, ticketId) { return this.engine.execute('support_take', { ticket_id: ticketId }, headers); }
  async operatorMy(headers, query) { return this.engine.execute('support_operator_my', query, headers); }
  async getChatId(headers, ticketId) { return this.engine.execute('support_get_chat', { ticket_id: ticketId }, headers); }
  async getEvents(headers, ticketId, query) { return this.engine.execute('support_get_events', { ticket_id: ticketId, ...query }, headers); }
}
```

- [ ] **Step 2: SupportController**

```typescript
// packages/backend/src/support/support.controller.ts
import { Controller, Get, Post, Body, Param, Query, UseGuards, Req } from '@nestjs/common';
import { JwtAuthGuard } from '../auth/jwt-auth.guard';
import { SignatureGuard } from '../auth/signature.guard';
import { SupportService } from './support.service';
import { CreateTicketDto } from './dto/create-ticket.dto';

@Controller('api/support')
@UseGuards(JwtAuthGuard, SignatureGuard)
export class SupportController {
  constructor(private readonly svc: SupportService) {}

  @Post('tickets')
  create(@Req() req, @Body() body: CreateTicketDto) {
    return this.svc.createTicket(req.headers, body);
  }

  @Get('tickets/my')
  myTickets(@Req() req, @Query() q) { return this.svc.getMyTickets(req.headers, q); }

  @Get('tickets/:id')
  getOne(@Req() req, @Param('id') id: string) { return this.svc.getTicket(req.headers, id); }

  @Post('tickets/:id/escalate')
  escalate(@Req() req, @Param('id') id: string) { return this.svc.escalate(req.headers, id); }

  @Post('tickets/:id/close')
  close(@Req() req, @Param('id') id: string) { return this.svc.close(req.headers, id); }

  @Get('queue')
  queue(@Req() req, @Query() q) { return this.svc.getQueue(req.headers, q); }

  @Post('tickets/:id/take')
  take(@Req() req, @Param('id') id: string) { return this.svc.take(req.headers, id); }

  @Get('operator/my')
  operatorMy(@Req() req, @Query() q) { return this.svc.operatorMy(req.headers, q); }

  @Get('tickets/:id/chat')
  chatId(@Req() req, @Param('id') id: string) { return this.svc.getChatId(req.headers, id); }

  @Get('tickets/:id/events')
  events(@Req() req, @Param('id') id: string, @Query() q) { return this.svc.getEvents(req.headers, id, q); }
}
```

- [ ] **Step 3: DTO**

```typescript
// packages/backend/src/support/dto/create-ticket.dto.ts
import { IsString, IsIn } from 'class-validator';
export class CreateTicketDto {
  @IsString() @IsIn(['how_to', 'bug', 'other']) category: string;
  @IsString() initial_message: string;
}
```

- [ ] **Step 4: SupportModule + регистрация в AppModule**

```typescript
// packages/backend/src/support/support.module.ts
import { Module } from '@nestjs/common';
import { SupportController } from './support.controller';
import { SupportService } from './support.service';
import { EngineModule } from '../engine/engine.module';
import { AuthModule } from '../auth/auth.module';

@Module({
  imports: [EngineModule, AuthModule],
  controllers: [SupportController],
  providers: [SupportService],
  exports: [SupportService],
})
export class SupportModule {}
```

```typescript
// packages/backend/src/app.module.ts
import { SupportModule } from './support/support.module';

@Module({ imports: [..., SupportModule], ... })
export class AppModule {}
```

- [ ] **Step 5: Smoke test — backend стартует и /api/support/* доступны**

Run: `cd /root/webclient_rugpt/packages/backend && npm run dev &`
Wait 5s.
Run: `curl -s http://localhost:4000/api/support/tickets/my -H "Authorization: Bearer <test_jwt>"` (нужны валидные тестовые auth headers — собрать через login flow или test fixture).
Expected: 200 либо 401 (если auth не прошёл) — ключевое что route существует, а не 404.
Run: `pkill -f "nest start"`.

- [ ] **Step 6: Commit**

`git add packages/backend/src/support/ packages/backend/src/app.module.ts && git commit -m "feat(backend): SupportModule controller and service"`

---

### Task 23: SocketGateway — re-scoped (no code change)

**Решение:** код уже корректен. `SocketGateway.handleChatJoin` использует только participant-check (`chat.participants.includes(userId)`) — без orgship-фильтра.

Cross-org для SUPPORT работает естественно:
- Клиент создал тикет → он в `chat.participants` → может join'нуться.
- Оператор взял тикет → `support_ticket_service.take_ticket` добавляет его в `chat.participants` через `chat_storage.add_participant` (Task 10) → может join'нуться.
- Foreign юзеры не в participants → отказ. Без orgship-узких мест.

Поведение проверяется E2E-сценарием в Task 32 (оператор берёт тикет, видит live-сообщения клиента из чужой орг через WS broadcast).

**Файлы:** ничего не трогаем.

---

## Phase 3 — WebClient Frontend

### Task 24: Sidebar — иконка тех. поддержки

**Files:**
- Modify: `packages/frontend/src/app/components/Sidebar.tsx`

- [ ] **Step 1: Найти место вставки**

В `Sidebar.tsx` (читать раздел между «RuGPT chats button» и «Divider» — строки ~254-272) после блока «Персональный ИИ» и **до** разделителя добавить новую кнопку «Тех. поддержка».

- [ ] **Step 2: Добавить кнопку**

```tsx
import { HeadsetIcon } from './icons/HeadsetIcon';
import { useSupportUnread } from '../hooks/useSupportUnread';

// внутри компонента:
const { unreadCount } = useSupportUnread();

// между "RuGPT chats button" и "Divider":
<div className={`${isExpanded ? 'w-full px-2' : ''} mb-3`}>
  <button
    onClick={() => navigateTo('/support')}
    className={`relative ${isExpanded ? 'w-full px-2 flex items-center gap-3 h-10 text-left' : 'w-10 h-10 justify-center'} rounded-avatar flex items-center transition-colors
      ${pathname === '/support' || pathname.startsWith('/chat/support/')
        ? 'bg-primary-lightest dark:bg-primary/20'
        : 'bg-highlight-lightest dark:bg-gray-700 hover:bg-primary-lightest dark:hover:bg-primary/20'}`}
    title="Тех. поддержка"
  >
    <div className="w-8 h-8 flex items-center justify-center shrink-0 relative">
      <HeadsetIcon className="w-4 h-4 text-primary dark:text-primary-light" />
      {unreadCount > 0 && (
        <span className="absolute -top-0.5 -right-0.5 min-w-[18px] h-[18px] flex items-center justify-center bg-rose-500 text-white text-[10px] font-bold rounded-full px-1 leading-none">
          {unreadCount > 99 ? '99+' : unreadCount}
        </span>
      )}
    </div>
    {isExpanded && (
      <span className="text-sm font-medium text-neutral-dark-darkest dark:text-white truncate">Тех. поддержка</span>
    )}
  </button>
</div>
```

- [ ] **Step 3: Smoke test — frontend стартует, иконка видна**

Run: `cd /root/webclient_rugpt/packages/frontend && npm run dev` (in background)
Browse: `http://localhost:3000` → авторизоваться → проверить что иконка «Тех. поддержка» появилась под «Персональный ИИ» и кликается → ведёт на `/support` (страница пока 404 — норм).

- [ ] **Step 4: Commit**

`git add packages/frontend/src/app/components/Sidebar.tsx && git commit -m "feat(frontend): support icon in sidebar"`

---

### Task 25: Frontend хуки — useSupportTickets, useSupportQueue, useSupportOperatorMy, useSupportUnread

**Files:**
- Create: `packages/frontend/src/app/hooks/useSupportTickets.ts`
- Create: `packages/frontend/src/app/hooks/useSupportQueue.ts`
- Create: `packages/frontend/src/app/hooks/useSupportOperatorMy.ts`
- Create: `packages/frontend/src/app/hooks/useSupportUnread.ts`

- [ ] **Step 1: useSupportTickets**

```typescript
// packages/frontend/src/app/hooks/useSupportTickets.ts
import { useEffect, useState } from 'react';
import type { SupportTicket, SupportTicketStatus } from '@webchat/common';
import { apiClient } from '../../transport/apiClient';

export function useSupportTickets(status?: SupportTicketStatus) {
  const [tickets, setTickets] = useState<SupportTicket[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const reload = async () => {
    setLoading(true);
    try {
      const q = status ? `?status=${status}` : '';
      const data = await apiClient.signedGet<SupportTicket[]>(`/api/support/tickets/my${q}`);
      setTickets(data);
    } catch (e: any) { setError(e.message); }
    finally { setLoading(false); }
  };

  useEffect(() => { void reload(); }, [status]);
  return { tickets, loading, error, reload };
}
```

- [ ] **Step 2: useSupportQueue**

```typescript
// packages/frontend/src/app/hooks/useSupportQueue.ts
import { useEffect, useState } from 'react';
import type { SupportTicket } from '@webchat/common';
import { apiClient } from '../../transport/apiClient';

export function useSupportQueue() {
  const [tickets, setTickets] = useState<SupportTicket[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const reload = async () => {
    setLoading(true);
    try {
      const data = await apiClient.signedGet<SupportTicket[]>('/api/support/queue');
      setTickets(data);
    } catch (e: any) { setError(e.message); }
    finally { setLoading(false); }
  };

  useEffect(() => { void reload(); }, []);
  return { tickets, loading, error, reload };
}
```

- [ ] **Step 3: useSupportOperatorMy** (аналогично useSupportQueue, endpoint `/api/support/operator/my`).

- [ ] **Step 4: useSupportUnread** — агрегатор: подписка на WS события `support_ticket_*` + initial fetch unread count из `/api/in-app-notifications/unread-count` фильтром по типам support_*.

```typescript
// packages/frontend/src/app/hooks/useSupportUnread.ts
import { useEffect, useState } from 'react';
import { apiClient } from '../../transport/apiClient';
import { useWsClient } from '../../transport/useWsClient';

export function useSupportUnread() {
  const [unreadCount, setUnreadCount] = useState(0);
  const ws = useWsClient();

  useEffect(() => {
    let mounted = true;
    const fetchInitial = async () => {
      const r = await apiClient.signedGet<{ count: number }>('/api/in-app-notifications/unread-count?type_prefix=support_');
      if (mounted) setUnreadCount(r.count);
    };
    void fetchInitial();
    const onNotif = (n: any) => {
      if (n.notification_type?.startsWith('support_')) setUnreadCount((c) => c + 1);
    };
    ws.on('notification', onNotif);
    return () => { mounted = false; ws.off('notification', onNotif); };
  }, [ws]);

  return { unreadCount, reset: () => setUnreadCount(0) };
}
```

⚠️ Endpoint `/api/in-app-notifications/unread-count?type_prefix=support_` может потребовать расширения существующего API — добавить query param. Если не нужна фильтрация на бэке, можно дёргать общий count и держать локальный счётчик от WS-событий.

- [ ] **Step 5: Commit**

`git add packages/frontend/src/app/hooks/useSupport*.ts && git commit -m "feat(frontend): support hooks"`

---

### Task 26: Components — SupportCategorySwitch, SupportSoftHandoffBlock, SupportTicketCard

**Files:**
- Create: `packages/frontend/src/app/components/SupportCategorySwitch.tsx`
- Create: `packages/frontend/src/app/components/SupportSoftHandoffBlock.tsx`
- Create: `packages/frontend/src/app/components/SupportTicketCard.tsx`

- [ ] **Step 1: SupportCategorySwitch**

```tsx
// SupportCategorySwitch.tsx
'use client';
import { SupportTicketCategory } from '@webchat/common';

interface Props {
  selected: SupportTicketCategory;
  onChange: (c: SupportTicketCategory) => void;
}

const LABELS: Record<SupportTicketCategory, string> = {
  [SupportTicketCategory.HOW_TO]: 'Что-то непонятно',
  [SupportTicketCategory.BUG]: 'Что-то не работает',
  [SupportTicketCategory.OTHER]: 'Что-то ещё',
};

export function SupportCategorySwitch({ selected, onChange }: Props) {
  return (
    <div className="flex gap-2 mb-4">
      {Object.values(SupportTicketCategory).map((c) => (
        <button
          key={c}
          onClick={() => onChange(c)}
          className={`px-4 py-2 rounded-pill text-sm font-medium transition-colors
            ${selected === c
              ? 'bg-primary text-white'
              : 'bg-neutral-light-medium dark:bg-gray-700 text-neutral-dark-darkest dark:text-white hover:bg-primary-lightest dark:hover:bg-primary/20'}`}
        >
          {LABELS[c]}
        </button>
      ))}
    </div>
  );
}
```

- [ ] **Step 2: SupportSoftHandoffBlock**

```tsx
// SupportSoftHandoffBlock.tsx
'use client';

interface Props {
  onContinueWithAi: () => void;
  onCallOperator: () => void;
  busy?: boolean;
}

export function SupportSoftHandoffBlock({ onContinueWithAi, onCallOperator, busy }: Props) {
  return (
    <div className="my-3 p-3 rounded-card bg-highlight-lightest dark:bg-gray-700 flex flex-col gap-2 max-w-md mx-auto">
      <p className="text-sm text-neutral-dark-medium dark:text-gray-300 text-center">
        Если ответ помог — продолжайте. Если нужен живой человек — позовите оператора.
      </p>
      <div className="flex gap-2 justify-center">
        <button
          onClick={onContinueWithAi}
          disabled={busy}
          className="px-4 py-2 rounded-pill bg-primary text-white text-sm font-medium hover:bg-primary-dark disabled:opacity-50"
        >
          Продолжить с ИИ
        </button>
        <button
          onClick={onCallOperator}
          disabled={busy}
          className="px-4 py-2 rounded-pill bg-neutral-light-medium dark:bg-gray-600 text-neutral-dark-darkest dark:text-white text-sm font-medium hover:bg-neutral-light-dark disabled:opacity-50"
        >
          Позвать оператора
        </button>
      </div>
    </div>
  );
}
```

- [ ] **Step 3: SupportTicketCard**

```tsx
// SupportTicketCard.tsx
'use client';
import { useRouter } from 'next/navigation';
import type { SupportTicket } from '@webchat/common';

const STATUS_LABELS = { open: 'Открыт', in_progress: 'В работе', closed: 'Закрыт' };
const CATEGORY_LABELS = { how_to: 'Вопрос', bug: 'Баг', other: 'Другое' };

export function SupportTicketCard({ ticket }: { ticket: SupportTicket }) {
  const router = useRouter();
  return (
    <button
      onClick={() => router.push(`/chat/support/${ticket.id}`)}
      className="w-full text-left p-3 rounded-card bg-neutral-light-medium dark:bg-gray-700 hover:bg-primary-lightest dark:hover:bg-primary/20 transition-colors"
    >
      <div className="flex justify-between items-start gap-2">
        <span className="text-sm font-medium text-neutral-dark-darkest dark:text-white truncate flex-1">
          {ticket.title || '(без темы)'}
        </span>
        <span className={`text-xs px-2 py-0.5 rounded-pill shrink-0
          ${ticket.status === 'closed' ? 'bg-neutral-light-dark text-neutral-dark-medium' : 'bg-primary text-white'}`}>
          {STATUS_LABELS[ticket.status]}
        </span>
      </div>
      <div className="text-xs text-neutral-dark-medium dark:text-gray-400 mt-1">
        {CATEGORY_LABELS[ticket.category]} · {new Date(ticket.createdAt).toLocaleString('ru')}
      </div>
    </button>
  );
}
```

- [ ] **Step 4: Smoke build**

Run: `cd /root/webclient_rugpt/packages/frontend && npm run build`
Expected: build success.

- [ ] **Step 5: Commit**

`git add packages/frontend/src/app/components/Support*.tsx && git commit -m "feat(frontend): support components"`

---

### Task 27: Page /support — лендинг

**Files:**
- Create: `packages/frontend/src/app/support/page.tsx`

- [ ] **Step 1: Implementation**

```tsx
// packages/frontend/src/app/support/page.tsx
'use client';
import { useState } from 'react';
import { useRouter } from 'next/navigation';
import { Sidebar } from '../components/Sidebar';
import { ChatNavigation } from '../components/ChatNavigation';
import { SupportCategorySwitch } from '../components/SupportCategorySwitch';
import { SupportTicketCard } from '../components/SupportTicketCard';
import { useSupportTickets } from '../hooks/useSupportTickets';
import { SupportTicketCategory } from '@webchat/common';
import { apiClient } from '../../transport/apiClient';

export default function SupportPage() {
  const router = useRouter();
  const [category, setCategory] = useState<SupportTicketCategory>(SupportTicketCategory.HOW_TO);
  const [message, setMessage] = useState('');
  const [submitting, setSubmitting] = useState(false);
  const { tickets, loading, reload } = useSupportTickets();

  const handleCreate = async () => {
    if (!message.trim()) return;
    setSubmitting(true);
    try {
      const { ticket, chat_id } = await apiClient.signedPost<{ ticket: any; chat_id: string }>(
        '/api/support/tickets',
        { category, initial_message: message.trim() },
      );
      router.push(`/chat/support/${ticket.id}`);
    } finally {
      setSubmitting(false);
    }
  };

  const open = tickets.filter((t) => t.status !== 'closed');
  const closed = tickets.filter((t) => t.status === 'closed');

  return (
    <div className="h-dvh bg-neutral-light-lightest dark:bg-gray-900 pl-[70px]">
      <Sidebar chats={[]} />
      <div className="h-full flex flex-col">
        <ChatNavigation title="Тех. поддержка" />
        <div className="flex-1 overflow-y-auto p-6">
          <div className="max-w-2xl mx-auto">
            <h2 className="text-heading-md font-bold mb-4 dark:text-white">Создать обращение</h2>
            <SupportCategorySwitch selected={category} onChange={setCategory} />
            <textarea
              className="w-full p-3 rounded-card bg-neutral-light-medium dark:bg-gray-700 dark:text-white"
              rows={4}
              placeholder="Опишите вопрос или проблему"
              value={message}
              onChange={(e) => setMessage(e.target.value)}
            />
            <button
              onClick={handleCreate}
              disabled={submitting || !message.trim()}
              className="mt-3 px-6 py-2 rounded-pill bg-primary text-white font-medium disabled:opacity-50"
            >
              {submitting ? 'Создаётся...' : 'Создать'}
            </button>

            <h2 className="text-heading-md font-bold mt-8 mb-4 dark:text-white">Мои обращения</h2>
            {loading ? <div>Загрузка...</div> : (
              <div className="flex flex-col gap-2">
                {open.map((t) => <SupportTicketCard key={t.id} ticket={t} />)}
                {closed.length > 0 && (
                  <details className="mt-4">
                    <summary className="cursor-pointer text-sm text-neutral-dark-medium dark:text-gray-400">
                      Закрытые ({closed.length})
                    </summary>
                    <div className="flex flex-col gap-2 mt-2 opacity-60">
                      {closed.map((t) => <SupportTicketCard key={t.id} ticket={t} />)}
                    </div>
                  </details>
                )}
              </div>
            )}
          </div>
        </div>
      </div>
    </div>
  );
}
```

- [ ] **Step 2: Manual test — создать тикет всех 3 категорий, убедиться что редиректит в чат**

- [ ] **Step 3: Commit**

`git add packages/frontend/src/app/support/page.tsx && git commit -m "feat(frontend): /support landing page"`

---

### Task 28: Page /chat/support/[id] — чат тикета

**Files:**
- Create: `packages/frontend/src/app/chat/support/[id]/page.tsx`

- [ ] **Step 1: Implementation**

Использовать существующий `Chat` компонент (как делает `/chat/task/[id]/page.tsx` — посмотреть как он передаёт `chatId`/`recipientId`).

```tsx
// packages/frontend/src/app/chat/support/[id]/page.tsx
'use client';
import { useEffect, useState } from 'react';
import { useParams, useRouter } from 'next/navigation';
import { Sidebar } from '../../../components/Sidebar';
import { ChatNavigation } from '../../../components/ChatNavigation';
import { Chat } from '../../../components/Chat';
import { SupportSoftHandoffBlock } from '../../../components/SupportSoftHandoffBlock';
import { useUser } from '../../../hooks/useUser';
import { apiClient } from '../../../../transport/apiClient';
import type { SupportTicket } from '@webchat/common';

const CATEGORY_LABELS = { how_to: 'Вопрос', bug: 'Баг', other: 'Другое' };
const STATUS_LABELS = { open: 'Открыт', in_progress: 'В работе', closed: 'Закрыт' };
const REOPEN_DAYS = 7;

export default function SupportChatPage() {
  const { id: ticketId } = useParams<{ id: string }>();
  const router = useRouter();
  const { user } = useUser();
  const [ticket, setTicket] = useState<SupportTicket | null>(null);
  const [chatId, setChatId] = useState<string | null>(null);

  const reload = async () => {
    const t = await apiClient.signedGet<SupportTicket>(`/api/support/tickets/${ticketId}`);
    const { chat_id } = await apiClient.signedGet<{ chat_id: string }>(`/api/support/tickets/${ticketId}/chat`);
    setTicket(t);
    setChatId(chat_id);
  };
  useEffect(() => { void reload(); }, [ticketId]);

  if (!ticket || !chatId || !user) return null;

  const isClosed = ticket.status === 'closed';
  const reopenDeadline = ticket.closedAt
    ? new Date(new Date(ticket.closedAt).getTime() + REOPEN_DAYS * 86400000)
    : null;
  const isInReopenWindow = !!reopenDeadline && reopenDeadline > new Date();
  const isArchived = isClosed && !isInReopenWindow;

  const handleEscalate = async () => {
    await apiClient.signedPost(`/api/support/tickets/${ticketId}/escalate`, {});
    await reload();
  };
  const handleClose = async () => {
    if (!confirm('Закрыть тикет?')) return;
    await apiClient.signedPost(`/api/support/tickets/${ticketId}/close`, {});
    await reload();
  };
  const handleContinueWithAi = () => {
    // просто визуально скрываем блок — состояние локальное
    setSoftHandoffSeen(true);
  };
  const [softHandoffSeen, setSoftHandoffSeen] = useState(false);

  const showSoftHandoff =
    ticket.category === 'how_to' &&
    !!ticket.aiFirstResponseAt &&
    !ticket.aiHandoffAt &&
    !softHandoffSeen;

  const headerSubtitle = `${CATEGORY_LABELS[ticket.category]} · ${STATUS_LABELS[ticket.status]} · ${new Date(ticket.createdAt).toLocaleDateString('ru')}`;

  return (
    <div className="h-dvh bg-neutral-light-lightest dark:bg-gray-900 pl-[70px]">
      <Sidebar chats={[]} />
      <div className="h-full flex flex-col">
        <ChatNavigation
          title="Тех. поддержка"
          subtitle={headerSubtitle}
          rightActions={
            <>
              {!ticket.aiHandoffAt && !isClosed && (
                <button onClick={handleEscalate} className="text-sm px-3 py-1 rounded-pill bg-neutral-light-medium dark:bg-gray-700 dark:text-white">
                  Позвать оператора
                </button>
              )}
              {!isClosed && (
                <button onClick={handleClose} className="text-sm px-3 py-1 rounded-pill bg-neutral-light-medium dark:bg-gray-700 dark:text-white">
                  Закрыть тикет
                </button>
              )}
            </>
          }
        />
        {isClosed && isInReopenWindow && (
          <div className="px-4 py-2 bg-highlight-lightest dark:bg-gray-700 text-sm text-center">
            Тикет закрыт. Если остался вопрос — напишите, тикет переоткроется (до {reopenDeadline!.toLocaleDateString('ru')}).
          </div>
        )}
        {isArchived && (
          <div className="px-4 py-2 bg-rose-100 dark:bg-rose-900 text-sm text-center">
            Тикет архивирован. <button onClick={() => router.push('/support')} className="underline">Создать новый?</button>
          </div>
        )}
        <Chat
          chatId={chatId}
          chatType="support"
          inputDisabled={isArchived}
          extraSlots={{
            beforeFirstAiMessage: showSoftHandoff
              ? <SupportSoftHandoffBlock onContinueWithAi={handleContinueWithAi} onCallOperator={handleEscalate} />
              : null
          }}
        />
      </div>
    </div>
  );
}
```

⚠️ Существующий компонент `Chat` может не поддерживать `chatType` props, `inputDisabled`, `extraSlots`. На имплементации:
1. Прочитать `Chat.tsx` (`packages/frontend/src/app/components/Chat.tsx`) и `MessageBubble.tsx`.
2. Добавить опциональные props: `chatType?: ChatType`, `inputDisabled?: boolean`, `extraSlots?: { beforeFirstAiMessage?: ReactNode }`.
3. Передавать `chatType` в `MessageBubble` (нужно для подмены имени, Task 31).
4. Если `extraSlots.beforeFirstAiMessage` задан — рендерить его перед первым сообщением `senderType === AI_ROLE`.

- [ ] **Step 2: Manual test — создать how_to тикет, дождаться AI ответа, увидеть soft-handoff блок**

- [ ] **Step 3: Commit**

`git add packages/frontend/src/app/chat/support/[id]/page.tsx packages/frontend/src/app/components/Chat.tsx && git commit -m "feat(frontend): support chat page with soft-handoff"`

---

### Task 29: Page /support/queue — очередь оператора

**Files:**
- Create: `packages/frontend/src/app/support/queue/page.tsx`

- [ ] **Step 1: Implementation**

```tsx
// packages/frontend/src/app/support/queue/page.tsx
'use client';
import { useRouter } from 'next/navigation';
import { Sidebar } from '../../components/Sidebar';
import { ChatNavigation } from '../../components/ChatNavigation';
import { useSupportQueue } from '../../hooks/useSupportQueue';
import { useUser } from '../../hooks/useUser';
import { RUGPT_SUPPORT_ORG_ID } from '@webchat/common';
import { apiClient } from '../../../transport/apiClient';

export default function SupportQueuePage() {
  const router = useRouter();
  const { user } = useUser();
  const { tickets, loading, reload } = useSupportQueue();

  if (user && user.orgId !== RUGPT_SUPPORT_ORG_ID) {
    return <div className="p-6">Доступ только для операторов тех. поддержки.</div>;
  }

  const handleTake = async (ticketId: string) => {
    try {
      await apiClient.signedPost(`/api/support/tickets/${ticketId}/take`, {});
      router.push(`/chat/support/${ticketId}`);
    } catch (e: any) {
      if (e.status === 409) alert('Тикет уже взят другим оператором');
      else throw e;
      await reload();
    }
  };

  return (
    <div className="h-dvh bg-neutral-light-lightest dark:bg-gray-900 pl-[70px]">
      <Sidebar chats={[]} />
      <div className="h-full flex flex-col">
        <ChatNavigation title="Очередь тех. поддержки" />
        <div className="flex-1 overflow-y-auto p-6">
          <div className="max-w-3xl mx-auto">
            {loading ? <div>Загрузка...</div> : tickets.length === 0 ? (
              <div className="text-center text-neutral-dark-medium dark:text-gray-400">Очередь пуста</div>
            ) : (
              <div className="flex flex-col gap-2">
                {tickets.map((t) => (
                  <div key={t.id} className="p-4 rounded-card bg-neutral-light-medium dark:bg-gray-700 flex justify-between items-center">
                    <div>
                      <div className="font-medium dark:text-white">{t.title}</div>
                      <div className="text-xs text-neutral-dark-medium dark:text-gray-400">
                        {t.category} · {new Date(t.createdAt).toLocaleString('ru')}
                      </div>
                    </div>
                    <button onClick={() => handleTake(t.id)} className="px-4 py-2 rounded-pill bg-primary text-white text-sm font-medium">
                      Взять
                    </button>
                  </div>
                ))}
              </div>
            )}
          </div>
        </div>
      </div>
    </div>
  );
}
```

- [ ] **Step 2: Commit**

`git add packages/frontend/src/app/support/queue/page.tsx && git commit -m "feat(frontend): /support/queue operator page"`

---

### Task 30: Расширение /tasks — табы для оператора

**Files:**
- Modify: `packages/frontend/src/app/tasks/page.tsx`

- [ ] **Step 1: Прочитать существующий tasks/page.tsx**

Run: `cat packages/frontend/src/app/tasks/page.tsx | head -80`

- [ ] **Step 2: Добавить условный рендеринг табов**

```tsx
// внутри Tasks page компонента
import { useUser } from '../hooks/useUser';
import { useSupportOperatorMy } from '../hooks/useSupportOperatorMy';
import { RUGPT_SUPPORT_ORG_ID } from '@webchat/common';

const { user } = useUser();
const isOperator = user?.orgId === RUGPT_SUPPORT_ORG_ID;
const [activeTab, setActiveTab] = useState<'tasks' | 'support'>('tasks');
const { tickets: supportTickets } = useSupportOperatorMy();

return (
  <div>
    {isOperator && (
      <div className="flex gap-2 border-b dark:border-gray-700">
        <button
          onClick={() => setActiveTab('tasks')}
          className={`px-4 py-2 ${activeTab === 'tasks' ? 'border-b-2 border-primary' : ''}`}
        >Задачи</button>
        <button
          onClick={() => setActiveTab('support')}
          className={`px-4 py-2 ${activeTab === 'support' ? 'border-b-2 border-primary' : ''}`}
        >Тикеты тех. поддержки ({supportTickets.length})</button>
      </div>
    )}
    {(!isOperator || activeTab === 'tasks') && (
      <ExistingTasksList />
    )}
    {isOperator && activeTab === 'support' && (
      <div className="flex flex-col gap-2">
        {supportTickets.map((t) => <SupportTicketCard key={t.id} ticket={t} />)}
      </div>
    )}
  </div>
);
```

- [ ] **Step 3: Commit**

`git add packages/frontend/src/app/tasks/page.tsx && git commit -m "feat(frontend): operator tabs on /tasks page"`

---

### Task 31: MessageBubble — подмена имени оператора на «Тех. поддержка»

**Files:**
- Modify: `packages/frontend/src/app/components/MessageBubble.tsx`

- [ ] **Step 1: Прочитать MessageBubble.tsx**

Run: `cat packages/frontend/src/app/components/MessageBubble.tsx | head -60`

- [ ] **Step 2: Добавить props chatType + senderOrgId, фильтр имени**

```tsx
// MessageBubble.tsx
import { ChatType, RUGPT_SUPPORT_ORG_ID } from '@webchat/common';

interface MessageBubbleProps {
  // ... существующие props
  chatType?: ChatType;
  senderOrgId?: string;
}

// внутри рендера:
const displayName = (
  chatType === 'support' &&
  senderOrgId === RUGPT_SUPPORT_ORG_ID &&
  !sender.isAI &&
  sender.id !== currentUserId
) ? 'Тех. поддержка' : sender.name;
```

⚠️ Сообщения должны нести `senderOrgId` в payload. Если сейчас не несут — расширить `MessageDto` в Engine `messages.py` route и `Message` model в frontend, либо подгружать на клиенте отдельно (хуже). Лучше — Engine при отдаче сообщений включает `sender_org_id` (это безопасно — orgship публичен внутри context чата, не leak).

- [ ] **Step 3: Передать в MessageBubble из Chat.tsx**

В местах использования `<MessageBubble ... />` передать `chatType={chatType}` и `senderOrgId={msg.senderOrgId}`.

- [ ] **Step 4: Commit**

`git add packages/frontend/src/app/components/MessageBubble.tsx packages/frontend/src/app/components/Chat.tsx && git commit -m "feat(frontend): hide operator name in support chats"`

---

## Phase 4 — End-to-End проверка

### Task 32: Локальная E2E проверка golden path

Не код — ручной/автоматический прогон сценариев. Зафиксировать в issue tracker / PR description.

- [ ] **Step 1: Подготовка тестовых аккаунтов**

```bash
# В psql:
# 1. Юзер в Acme орг (клиент): seed_user_acme@test.com / secret
# 2. Оператор в RuGPT Support орг: operator1@rugpt.support / secret
```

Создать через `./test-init.sh` или вручную INSERT'ами.

- [ ] **Step 2: Сценарий 1 — how_to с AI ответом**

Залогиниться клиентом → клик на иконку «Тех. поддержка» → выбрать «Что-то непонятно» → ввести «как создать чат?» → отправить → дождаться AI ответа (10-30 сек) → увидеть блок «Продолжить с ИИ / Позвать оператора» → клик «Продолжить» → блок исчез → написать ещё вопрос → AI ответил.

- [ ] **Step 3: Сценарий 2 — эскалация**

В том же чате → клик «Позвать оператора» в шапке → подтвердить (если есть confirm) → AI больше не отвечает на новые сообщения (тест: написать → 30 сек тишина) → проверить что у оператора пришло in-app уведомление + тикет появился в `/support/queue`.

- [ ] **Step 4: Сценарий 3 — bug сразу в очередь**

Клиент → новый тикет «Что-то не работает» → отправить → редирект в чат → проверить что AI не подключился (нет AI ответа) → у оператора уведомление + в очереди.

- [ ] **Step 5: Сценарий 4 — оператор берёт и закрывает**

Залогиниться оператором → `/support/queue` → клик «Взять» на одном из тикетов → редирект в чат → клиент видит системное сообщение «Оператор взял тикет в работу» (live через WS) → оператор пишет «починили, проверьте» → клиент видит «Тех. поддержка: починили, проверьте» (имя оператора скрыто) → оператор клик «Закрыть тикет» → системное сообщение «Оператор закрыл тикет» обоим.

- [ ] **Step 6: Сценарий 5 — reopen в окне**

Сразу после закрытия клиент пишет «у меня ещё вопрос» → сообщение проходит → системное сообщение «Тикет переоткрыт» → у оператора in-app уведомление о переоткрытии.

- [ ] **Step 7: Сценарий 6 — попытка cross-org лазейки**

Залогиниться оператором → попытаться открыть `/chat/<id>` где `<id>` — direct чат из Acme орг (взять ID из БД) → ожидать 403 либо редирект (НЕ открытый чат).

- [ ] **Step 8: Зафиксировать результаты**

В PR description: список 6 сценариев с пометкой PASS/FAIL/skipped и причинами.

---

## Self-Review Checklist (для автора плана)

- [ ] **Spec coverage:** прохожусь по разделам спеки 2026-04-26-support-chat-design.md, для каждого пункта указываю номер задачи. Раздел 2 (решения) → покрыт во всех Phase 1+2+3. Раздел 3 (схема) → Task 1. Раздел 4 (API) → Tasks 14-16, 21-22. Раздел 5 (cross-org) → Tasks 8-9, 18, 23. Раздел 6 (AI) → Tasks 12, 17. Раздел 7 (уведомления) → Task 11. Раздел 8 (frontend) → Tasks 24-31. Раздел 9 (UX flows) → Task 32. Раздел 10 (миграции) → Tasks 1, 19. Раздел 11 (тесты) → распределено по всем Tasks с TDD-блоком + Task 9 покрывает регрессии cross-org.
- [ ] **Placeholder scan:** прошёл — нет TBD/TODO в шагах. Везде где «нужно проверить как работает X» — чёткая инструкция (`grep -n` команда + что зафиксировать).
- [ ] **Type consistency:** имена полей `aiHandoffAt` (camelCase в TS) ↔ `ai_handoff_at` (snake_case в Python) — это правильное конвертирование между layers, не bug. `RUGPT_SUPPORT_ORG_ID` константа в обоих языках одинаковая по UUID. `closed_by_role` enum значения `requester`/`operator` совпадают.

---

## Execution Handoff

**Plan complete and saved to `/root/rugpt/docs/2026-04-26-support-chat-plan.md`. Two execution options:**

**1. Subagent-Driven (recommended)** — я диспатчу свежий subagent per task, ревью между задачами, быстрая итерация.

**2. Inline Execution** — выполняем задачи в этом же session через executing-plans, batch с checkpoint'ами.

**Какой вариант?**
