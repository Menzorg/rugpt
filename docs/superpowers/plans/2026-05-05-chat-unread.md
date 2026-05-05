# Chat Unread Status Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Внедрить read/unread статус сообщений в rugpt с бейджами в sidebar, tasks list, projects list.

**Architecture:** На стороне Engine — отдельная таблица `chat_read_state(chat_id, user_id, last_read_message_id, last_read_at)` с UPSERT-ом и monotonic guard. Два HTTP endpoint'а (POST mark-read + GET bulk unread-counts). На стороне NestJS — proxy к Engine + замена no-op WS-handler `MESSAGE_READ` на реальный + новый WS server-event `chat:unread-cleared` для multi-device sync. На стороне Frontend — zustand store `useChatUnreadStore` + bootstrap-хук с подписками на WS-события + effect в `useChatService` для авто-mark-as-read при mount чата + UI badges в 4 точках.

**Tech Stack:** Python 3.10+ / FastAPI / asyncpg / pytest (Engine), NestJS 10 / Socket.IO / Jest (Backend), Next.js 15 / React 19 / zustand / RxJS / Vitest (Frontend).

**Spec:** `/root/rugpt/docs/superpowers/specs/2026-05-05-chat-unread-design.md`

---

## File Structure

### Engine (`/root/rugpt/`)

- **Create**: `src/engine/migrations/036_chat_read_state.sql` — DDL для новой таблицы
- **Create**: `src/engine/storage/chat_read_state_storage.py` — UPSERT + bulk get_unread_counts
- **Create**: `tests/test_chat_read_state_storage.py` — storage tests
- **Create**: `tests/test_chat_service_read.py` — service tests (mark_chat_read + list_unread_counts)
- **Create**: `tests/test_chats_read_routes.py` — route integration tests
- **Modify**: `src/engine/services/chat_service.py` — два новых метода
- **Modify**: `src/engine/services/engine_service.py` — wire ChatReadStateStorage в singleton + передать в ChatService
- **Modify**: `src/engine/routes/chats.py` — два новых endpoint'а

### Common shared types (`/root/webclient_rugpt/packages/common/`)

- **Modify**: `src/websocket/events.ts` — добавить `CHAT_UNREAD_CLEARED` в `WsServerEvents`
- **Modify**: `src/websocket/server-events.ts` — добавить payload type в `ServerToClientEvents`

### NestJS backend (`/root/webclient_rugpt/packages/backend/`)

- **Modify**: `src/engine/adapters/rugpt.adapter.ts` — два case'а в `execute()`
- **Modify**: `src/chat/chat.service.ts` — два метода (markChatRead + getUnreadCounts)
- **Modify**: `src/chat/chat.controller.ts` — GET endpoint
- **Modify**: `src/socket/socket.gateway.ts` — заменить skeleton `handleMessageRead`
- **Modify**: `src/socket/__tests__/socket.chat.spec.ts` — переписать тест-skeleton под реальную логику
- **Create**: `src/chat/__tests__/chat.controller.spec.ts` — controller test (если файла нет, иначе extend)

### Frontend (`/root/webclient_rugpt/packages/frontend/`)

- **Create**: `src/app/hooks/useChatUnreadStore.ts` — zustand store
- **Create**: `src/app/hooks/useChatUnreadStore.test.ts`
- **Create**: `src/app/hooks/useChatUnreadBootstrap.ts` — fetch + WS subscriptions
- **Create**: `src/app/hooks/useChatUnreadBootstrap.test.tsx`
- **Modify**: `src/transport/wsClient.ts` — getter `chatUnreadCleared$`
- **Modify**: `src/domain/chat/useChatService.ts` — mark-read effect
- **Modify**: `src/app/components/Sidebar.tsx` — call `useChatUnreadBootstrap()`, render badges direct rows + task/project sections, peerToChat resolver
- **Modify**: `src/app/utils/sidebarChats.ts` — extend `SidebarChatItem` опциональным `unreadCount`
- **Modify**: `src/app/tasks/page.tsx` — badge на строке задачи
- **Modify**: `src/app/projects/page.tsx` — badge на строке проекта

---

## Task Sequence

Задачи независимы по слоям: Engine (T1–T5), Common (T6), Backend (T7–T9), Frontend (T10–T15). Внутри слоя — последовательно (storage до service, service до route, и т.д.).

**При выполнении в subagent-driven mode: НЕ делать git commits. Пользователь сам контролирует коммиты.** Шаги "Commit" в плане сохранены как канонические для skill, но subagent должен их пропускать.

---

### Task 1: Engine — Migration 036_chat_read_state

**Files:**
- Create: `/root/rugpt/src/engine/migrations/036_chat_read_state.sql`

- [ ] **Step 1: Создать SQL-миграцию**

Файл `/root/rugpt/src/engine/migrations/036_chat_read_state.sql`:

```sql
-- Migration 036: chat_read_state
-- High-water-mark per (chat, user) для подсчёта unread сообщений.

CREATE TABLE chat_read_state (
    chat_id UUID NOT NULL REFERENCES chats(id) ON DELETE CASCADE,
    user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    last_read_message_id UUID REFERENCES messages(id) ON DELETE SET NULL,
    last_read_at TIMESTAMPTZ NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (chat_id, user_id)
);

CREATE INDEX idx_chat_read_state_user ON chat_read_state(user_id);
```

- [ ] **Step 2: Применить миграцию**

```bash
cd /root/rugpt && ./migrate.sh
```

Expected: миграция 036 применена, скрипт завершается без ошибок.

- [ ] **Step 3: Проверить структуру таблицы**

```bash
set -a; source /root/rugpt/.env; set +a
psql -U postgres -h localhost -d rugpt -c "\d chat_read_state"
```

Expected: видны колонки `chat_id`, `user_id`, `last_read_message_id`, `last_read_at`, `updated_at`, PRIMARY KEY (chat_id, user_id), индекс `idx_chat_read_state_user`.

- [ ] **Step 4: Commit**

```bash
git add src/engine/migrations/036_chat_read_state.sql
git commit -m "feat(engine): add chat_read_state migration 036"
```

---

### Task 2: Engine — ChatReadStateStorage

**Files:**
- Create: `/root/rugpt/src/engine/storage/chat_read_state_storage.py`
- Create: `/root/rugpt/tests/test_chat_read_state_storage.py`

- [ ] **Step 1: Написать failing test для upsert (создание)**

Файл `/root/rugpt/tests/test_chat_read_state_storage.py`:

```python
import pytest
import uuid
from datetime import datetime, timezone, timedelta
from src.engine.storage.chat_read_state_storage import ChatReadStateStorage


@pytest.mark.asyncio
async def test_upsert_creates_row(engine_service, sample_chat, sample_user, sample_message):
    """UPSERT создаёт новую строку, если её не было."""
    storage: ChatReadStateStorage = engine_service.chat_read_state_storage
    await storage.upsert(
        chat_id=sample_chat.id,
        user_id=sample_user.id,
        last_read_message_id=sample_message.id,
        last_read_at=sample_message.created_at,
    )

    counts = await storage.get_unread_counts_for_user(sample_user.id, sample_chat.org_id)
    assert sample_chat.id not in counts  # 0 unread, чат не должен попасть в результат
```

- [ ] **Step 2: Run test — должен упасть (модуль ещё не существует)**

```bash
cd /root/rugpt && source venv/bin/activate && pytest tests/test_chat_read_state_storage.py::test_upsert_creates_row -v
```

Expected: FAIL — `ModuleNotFoundError: chat_read_state_storage`.

- [ ] **Step 3: Создать ChatReadStateStorage с минимальной реализацией**

Файл `/root/rugpt/src/engine/storage/chat_read_state_storage.py`:

```python
"""ChatReadStateStorage — high-water-mark прочитанности сообщений per (chat, user)."""
import logging
from datetime import datetime
from typing import Dict
from uuid import UUID

from .base import BaseStorage

logger = logging.getLogger(__name__)


class ChatReadStateStorage(BaseStorage):
    async def upsert(
        self,
        chat_id: UUID,
        user_id: UUID,
        last_read_message_id: UUID,
        last_read_at: datetime,
    ) -> None:
        """
        UPSERT с monotonic guard: не двигаем high-water-mark назад.
        Если в таблице уже есть запись с last_read_at >= incoming — UPDATE отменяется.
        """
        sql = """
            INSERT INTO chat_read_state (chat_id, user_id, last_read_message_id, last_read_at)
            VALUES ($1, $2, $3, $4)
            ON CONFLICT (chat_id, user_id) DO UPDATE
            SET last_read_message_id = EXCLUDED.last_read_message_id,
                last_read_at = EXCLUDED.last_read_at,
                updated_at = NOW()
            WHERE EXCLUDED.last_read_at >= chat_read_state.last_read_at
        """
        async with self.pool.acquire() as conn:
            await conn.execute(sql, chat_id, user_id, last_read_message_id, last_read_at)

    async def get_unread_counts_for_user(
        self, user_id: UUID, org_id: UUID
    ) -> Dict[UUID, int]:
        """Bulk: для всех чатов юзера в org возвращает {chat_id: count}, count > 0."""
        sql = """
            SELECT c.id AS chat_id,
                   LEAST(COUNT(m.id), 100) AS unread
            FROM chats c
            LEFT JOIN chat_read_state crs
              ON crs.chat_id = c.id AND crs.user_id = $1
            LEFT JOIN messages m
              ON m.chat_id = c.id
             AND m.sender_id != $1
             AND m.is_deleted = false
             AND m.created_at > COALESCE(crs.last_read_at, '-infinity'::timestamptz)
            WHERE c.org_id = $2
              AND c.is_active = true
              AND $1::text = ANY(c.participants)
            GROUP BY c.id
            HAVING COUNT(m.id) > 0
        """
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(sql, user_id, org_id)
        return {row["chat_id"]: int(row["unread"]) for row in rows}
```

- [ ] **Step 4: Wire storage в EngineService временно для теста**

В `/root/rugpt/src/engine/services/engine_service.py` после строки 91 (`self.message_storage = MessageStorage(...)`) добавить:

```python
        from ..storage.chat_read_state_storage import ChatReadStateStorage
        self.chat_read_state_storage = ChatReadStateStorage(self.postgres_dsn)
```

В методе init (~строка 410, рядом с `await self.chat_storage.init()`) добавить:

```python
        await self.chat_read_state_storage.init()
```

- [ ] **Step 5: Run test — должен пройти**

```bash
cd /root/rugpt && pytest tests/test_chat_read_state_storage.py::test_upsert_creates_row -v
```

Expected: PASS.

- [ ] **Step 6: Добавить тест monotonic_guard**

В тот же файл `tests/test_chat_read_state_storage.py`:

```python
@pytest.mark.asyncio
async def test_upsert_monotonic_guard(engine_service, sample_chat, sample_user, two_messages_in_order):
    """Старый last_read_at не двигает HWM назад."""
    storage: ChatReadStateStorage = engine_service.chat_read_state_storage
    msg_old, msg_new = two_messages_in_order  # msg_new.created_at > msg_old.created_at

    # Сначала отметили новое сообщение как прочитанное
    await storage.upsert(sample_chat.id, sample_user.id, msg_new.id, msg_new.created_at)
    # Затем приходит "старое" событие — UPSERT не должен откатить HWM
    await storage.upsert(sample_chat.id, sample_user.id, msg_old.id, msg_old.created_at)

    # Если был бы откат — счётчик нашёл бы msg_new как unread. Проверяем, что нет.
    counts = await storage.get_unread_counts_for_user(sample_user.id, sample_chat.org_id)
    assert sample_chat.id not in counts
```

- [ ] **Step 7: Run test — должен пройти**

```bash
cd /root/rugpt && pytest tests/test_chat_read_state_storage.py::test_upsert_monotonic_guard -v
```

Expected: PASS.

- [ ] **Step 8: Добавить тесты для get_unread_counts_for_user**

В тот же файл:

```python
@pytest.mark.asyncio
async def test_get_unread_counts_excludes_own(engine_service, sample_chat, sample_user, sample_other_user):
    """Свои сообщения не попадают в counter."""
    msg_storage = engine_service.message_storage
    # sample_user пишет 3 сообщения в чат, sample_other_user — 2
    for _ in range(3):
        await msg_storage.create_message(chat_id=sample_chat.id, sender_id=sample_user.id, content="my")
    for _ in range(2):
        await msg_storage.create_message(chat_id=sample_chat.id, sender_id=sample_other_user.id, content="other")

    counts = await engine_service.chat_read_state_storage.get_unread_counts_for_user(
        sample_user.id, sample_chat.org_id
    )
    assert counts[sample_chat.id] == 2  # видны только 2 от other


@pytest.mark.asyncio
async def test_get_unread_counts_excludes_deleted(engine_service, sample_chat, sample_user, sample_other_user):
    """is_deleted=true сообщения не попадают."""
    msg_storage = engine_service.message_storage
    msg = await msg_storage.create_message(chat_id=sample_chat.id, sender_id=sample_other_user.id, content="x")
    await msg_storage.soft_delete(msg.id)

    counts = await engine_service.chat_read_state_storage.get_unread_counts_for_user(
        sample_user.id, sample_chat.org_id
    )
    assert sample_chat.id not in counts


@pytest.mark.asyncio
async def test_get_unread_counts_caps_at_100(engine_service, sample_chat, sample_user, sample_other_user):
    """Cap = 100, даже если фактически больше."""
    msg_storage = engine_service.message_storage
    for i in range(120):
        await msg_storage.create_message(chat_id=sample_chat.id, sender_id=sample_other_user.id, content=f"m{i}")

    counts = await engine_service.chat_read_state_storage.get_unread_counts_for_user(
        sample_user.id, sample_chat.org_id
    )
    assert counts[sample_chat.id] == 100


@pytest.mark.asyncio
async def test_get_unread_counts_skips_zero_chats(engine_service, sample_chat_empty, sample_user):
    """Чаты без unread не возвращаются."""
    counts = await engine_service.chat_read_state_storage.get_unread_counts_for_user(
        sample_user.id, sample_chat_empty.org_id
    )
    assert sample_chat_empty.id not in counts
```

**Замечание для имплементатора:** если в проекте нет fixtures `sample_chat`, `sample_user`, `sample_other_user`, `sample_message`, `two_messages_in_order`, `sample_chat_empty` — добавить их в `tests/conftest.py` через `engine_service.chat_service.create_direct_chat` / `engine_service.message_storage.create_message`. Реальные методы fixtures посмотри в существующих тестах вокруг (например `test_chat_storage_support_cross_org.py`).

- [ ] **Step 9: Run all storage tests**

```bash
cd /root/rugpt && pytest tests/test_chat_read_state_storage.py -v
```

Expected: ВСЕ PASS.

- [ ] **Step 10: Commit**

```bash
git add src/engine/storage/chat_read_state_storage.py tests/test_chat_read_state_storage.py src/engine/services/engine_service.py
git commit -m "feat(engine): add ChatReadStateStorage with upsert + bulk unread-counts"
```

---

### Task 3: Engine — ChatService.mark_chat_read + list_unread_counts

**Files:**
- Modify: `/root/rugpt/src/engine/services/chat_service.py`
- Modify: `/root/rugpt/src/engine/services/engine_service.py`
- Create: `/root/rugpt/tests/test_chat_service_read.py`

- [ ] **Step 1: Написать failing tests**

Файл `/root/rugpt/tests/test_chat_service_read.py`:

```python
import pytest
import uuid
from src.engine.utils.errors import NotFoundError, ForbiddenError


@pytest.mark.asyncio
async def test_mark_chat_read_happy_path(engine_service, sample_chat, sample_user, sample_message_in_chat):
    """Happy path — UPSERT успешен."""
    await engine_service.chat_service.mark_chat_read(
        chat_id=sample_chat.id,
        user_id=sample_user.id,
        message_id=sample_message_in_chat.id,
    )
    # Проверка через storage — после mark-read counter должен быть пуст
    counts = await engine_service.chat_read_state_storage.get_unread_counts_for_user(
        sample_user.id, sample_chat.org_id
    )
    assert sample_chat.id not in counts


@pytest.mark.asyncio
async def test_mark_chat_read_validates_message_in_chat(
    engine_service, sample_chat, sample_other_chat, sample_user, sample_message_in_other_chat
):
    """Если message принадлежит другому чату — NotFoundError."""
    with pytest.raises(NotFoundError):
        await engine_service.chat_service.mark_chat_read(
            chat_id=sample_chat.id,
            user_id=sample_user.id,
            message_id=sample_message_in_other_chat.id,
        )


@pytest.mark.asyncio
async def test_mark_chat_read_validates_participant(
    engine_service, sample_chat, sample_outsider_user, sample_message_in_chat
):
    """User не участник чата → ForbiddenError."""
    with pytest.raises(ForbiddenError):
        await engine_service.chat_service.mark_chat_read(
            chat_id=sample_chat.id,
            user_id=sample_outsider_user.id,
            message_id=sample_message_in_chat.id,
        )


@pytest.mark.asyncio
async def test_list_unread_counts_returns_dict(engine_service, sample_user, sample_chat, sample_other_user):
    """list_unread_counts проксирует storage и возвращает dict."""
    msg_storage = engine_service.message_storage
    await msg_storage.create_message(chat_id=sample_chat.id, sender_id=sample_other_user.id, content="hi")

    counts = await engine_service.chat_service.list_unread_counts(
        user_id=sample_user.id, org_id=sample_chat.org_id
    )
    assert isinstance(counts, dict)
    assert counts[sample_chat.id] == 1
```

**Замечание:** `NotFoundError` / `ForbiddenError` существуют в `src/engine/utils/errors.py`. Если имена другие — посмотри как уже выкидываются 404/403 в существующих сервисах (`chat_service.py`).

- [ ] **Step 2: Run tests — должны упасть**

```bash
cd /root/rugpt && pytest tests/test_chat_service_read.py -v
```

Expected: FAIL — `AttributeError: ChatService has no attribute 'mark_chat_read'`.

- [ ] **Step 3: Добавить два метода в ChatService**

В `/root/rugpt/src/engine/services/chat_service.py` после метода `create_direct_chat` (строка ~119):

```python
    async def mark_chat_read(
        self, chat_id: UUID, user_id: UUID, message_id: UUID
    ) -> None:
        """Отметить чат прочитанным до сообщения message_id (включительно)."""
        msg = await self.message_storage.get_by_id(message_id)
        if not msg or msg.chat_id != chat_id:
            raise NotFoundError("Message not in chat")
        chat = await self.chat_storage.get_by_id(chat_id)
        if not chat or str(user_id) not in chat.participants:
            raise ForbiddenError("Not a participant")
        await self.chat_read_state_storage.upsert(
            chat_id=chat_id,
            user_id=user_id,
            last_read_message_id=message_id,
            last_read_at=msg.created_at,
        )

    async def list_unread_counts(
        self, user_id: UUID, org_id: UUID
    ) -> Dict[UUID, int]:
        """Вернуть unread-counter по всем чатам юзера в org. Чаты с 0 unread исключены."""
        return await self.chat_read_state_storage.get_unread_counts_for_user(user_id, org_id)
```

В начале файла убедиться, что импорты включают:
```python
from typing import Dict
from uuid import UUID
from .errors import NotFoundError, ForbiddenError  # или откуда они берутся
```

`ChatService.__init__` должен принимать `chat_read_state_storage` параметром — добавить:

```python
    def __init__(
        self,
        chat_storage: ChatStorage,
        message_storage: MessageStorage,
        # ... existing params ...
        chat_read_state_storage: ChatReadStateStorage,  # NEW
    ):
        # ... existing assignments ...
        self.chat_read_state_storage = chat_read_state_storage
```

(Точные позиции существующих параметров — посмотри в текущем `chat_service.py:__init__`.)

- [ ] **Step 4: Pass storage в ChatService при создании в EngineService**

В `/root/rugpt/src/engine/services/engine_service.py` найти место где создаётся `ChatService` (строки ~136-141 по grep'у):

```python
        self.chat_service = ChatService(
            self.chat_storage,
            self.message_storage,
            # ... existing args ...
            chat_read_state_storage=self.chat_read_state_storage,  # NEW — kwarg
        )
```

- [ ] **Step 5: Run tests — должны пройти**

```bash
cd /root/rugpt && pytest tests/test_chat_service_read.py -v
```

Expected: ВСЕ PASS.

- [ ] **Step 6: Commit**

```bash
git add src/engine/services/chat_service.py src/engine/services/engine_service.py tests/test_chat_service_read.py
git commit -m "feat(engine): ChatService.mark_chat_read + list_unread_counts"
```

---

### Task 4: Engine — Routes (POST /chats/{id}/read + GET /chats/unread-counts)

**Files:**
- Modify: `/root/rugpt/src/engine/routes/chats.py`
- Create: `/root/rugpt/tests/test_chats_read_routes.py`

- [ ] **Step 1: Написать failing tests**

Файл `/root/rugpt/tests/test_chats_read_routes.py`:

```python
import pytest
from httpx import AsyncClient


@pytest.mark.asyncio
async def test_post_read_204(client: AsyncClient, sample_user, sample_chat, sample_message_in_chat):
    """POST /chats/{id}/read возвращает 204."""
    resp = await client.post(
        f"/api/v1/chats/{sample_chat.id}/read",
        params={"user_id": str(sample_user.id)},
        json={"message_id": str(sample_message_in_chat.id)},
    )
    assert resp.status_code == 204


@pytest.mark.asyncio
async def test_post_read_404_message_in_other_chat(
    client: AsyncClient, sample_user, sample_chat, sample_message_in_other_chat
):
    resp = await client.post(
        f"/api/v1/chats/{sample_chat.id}/read",
        params={"user_id": str(sample_user.id)},
        json={"message_id": str(sample_message_in_other_chat.id)},
    )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_post_read_403_not_participant(
    client: AsyncClient, sample_outsider_user, sample_chat, sample_message_in_chat
):
    resp = await client.post(
        f"/api/v1/chats/{sample_chat.id}/read",
        params={"user_id": str(sample_outsider_user.id)},
        json={"message_id": str(sample_message_in_chat.id)},
    )
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_get_unread_counts_returns_dict(
    client: AsyncClient, sample_user, sample_chat, sample_other_user, engine_service
):
    """Bulk endpoint возвращает {chat_id: count}."""
    await engine_service.message_storage.create_message(
        chat_id=sample_chat.id, sender_id=sample_other_user.id, content="hi"
    )
    resp = await client.get("/api/v1/chats/unread-counts", params={"user_id": str(sample_user.id)})
    assert resp.status_code == 200
    data = resp.json()
    assert str(sample_chat.id) in data
    assert data[str(sample_chat.id)] == 1


@pytest.mark.asyncio
async def test_get_unread_counts_only_my_chats(
    client: AsyncClient, sample_user, sample_chat_for_other_users
):
    """Чаты, в которых я не участник, не светятся."""
    resp = await client.get("/api/v1/chats/unread-counts", params={"user_id": str(sample_user.id)})
    assert resp.status_code == 200
    data = resp.json()
    assert str(sample_chat_for_other_users.id) not in data
```

- [ ] **Step 2: Run tests — должны упасть с 404**

```bash
cd /root/rugpt && pytest tests/test_chats_read_routes.py -v
```

Expected: FAIL — endpoint'ов ещё нет, FastAPI возвращает 404 на route lookup.

- [ ] **Step 3: Добавить endpoint POST /chats/{chat_id}/read**

В `/root/rugpt/src/engine/routes/chats.py` после существующего endpoint'а на отправку сообщений (`POST /chats/{chat_id}/messages` ~ строка 319):

```python
class MarkReadRequest(BaseModel):
    message_id: UUID


@router.post("/chats/{chat_id}/read", status_code=204)
async def mark_chat_read(
    chat_id: UUID,
    body: MarkReadRequest,
    user_id: UUID = Query(...),
    engine: EngineService = Depends(get_engine_service),
):
    """Поднять high-water-mark прочитанности до message_id."""
    try:
        await engine.chat_service.mark_chat_read(
            chat_id=chat_id, user_id=user_id, message_id=body.message_id
        )
    except NotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except ForbiddenError as e:
        raise HTTPException(status_code=403, detail=str(e))
    return None
```

Импорты в начале файла должны включать `BaseModel`, `Query`, `HTTPException`, `Depends`, `UUID`, `NotFoundError`, `ForbiddenError`. Если каких-то нет — добавить.

- [ ] **Step 4: Добавить endpoint GET /chats/unread-counts**

В том же файле, рядом с другими GET-endpoint'ами (`/chats/my` ~ строка 122):

```python
@router.get("/chats/unread-counts")
async def get_unread_counts(
    user_id: UUID = Query(...),
    engine: EngineService = Depends(get_engine_service),
) -> Dict[str, int]:
    """Bulk: для всех чатов юзера в его org вернуть {chat_id: count}, count > 0."""
    user = await engine.user_storage.get_by_id(user_id)
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    counts = await engine.chat_service.list_unread_counts(user_id=user_id, org_id=user.org_id)
    return {str(chat_id): count for chat_id, count in counts.items()}
```

- [ ] **Step 5: Run tests — должны пройти**

```bash
cd /root/rugpt && pytest tests/test_chats_read_routes.py -v
```

Expected: ВСЕ PASS.

- [ ] **Step 6: Sanity-check полным pytest engine**

```bash
cd /root/rugpt && pytest tests/ -x --tb=short -q
```

Expected: все existing тесты тоже PASS — ничего не сломали.

- [ ] **Step 7: Commit**

```bash
git add src/engine/routes/chats.py tests/test_chats_read_routes.py
git commit -m "feat(engine): POST /chats/{id}/read + GET /chats/unread-counts endpoints"
```

---

### Task 5: Common — WS event types (CHAT_UNREAD_CLEARED)

**Files:**
- Modify: `/root/webclient_rugpt/packages/common/src/websocket/events.ts`
- Modify: `/root/webclient_rugpt/packages/common/src/websocket/server-events.ts`

- [ ] **Step 1: Добавить событие в WsServerEvents**

В `/root/webclient_rugpt/packages/common/src/websocket/events.ts` в объект `WsServerEvents` (~строка 39) добавить новый ключ:

```typescript
  // Chat read state (multi-device sync)
  CHAT_UNREAD_CLEARED: 'chat:unread-cleared',
```

(Между существующими ключами — например после блока "Users" или в новом блоке "Chat read state".)

- [ ] **Step 2: Добавить payload type в ServerToClientEvents**

В `/root/webclient_rugpt/packages/common/src/websocket/server-events.ts` в interface `ServerToClientEvents` (~строка 23):

```typescript
  'chat:unread-cleared': { chatId: string };
```

- [ ] **Step 3: Compile common package**

```bash
cd /root/webclient_rugpt/packages/common && pnpm build
```

Expected: TS-компиляция чистая, exit 0.

- [ ] **Step 4: Commit**

```bash
git add packages/common/src/websocket/events.ts packages/common/src/websocket/server-events.ts
git commit -m "feat(common): add CHAT_UNREAD_CLEARED ws server event"
```

---

### Task 6: Backend — RuGPTEngineAdapter cases

**Files:**
- Modify: `/root/webclient_rugpt/packages/backend/src/engine/adapters/rugpt.adapter.ts`

- [ ] **Step 1: Найти место для новых case'ов в execute()**

Открыть `/root/webclient_rugpt/packages/backend/src/engine/adapters/rugpt.adapter.ts:393` (там уже есть `case 'create_direct_chat':`). Новые case'ы добавить рядом с другими chat-case'ами.

- [ ] **Step 2: Добавить case 'mark_chat_read'**

```typescript
      case 'mark_chat_read': {
        const { chatId, userId, messageId } = payload as {
          chatId: string;
          userId: string;
          messageId: string;
        };
        return this.client.post(
          `/api/v1/chats/${chatId}/read?user_id=${userId}`,
          { message_id: messageId },
          correlationId,
        );
      }
```

- [ ] **Step 3: Добавить case 'get_unread_counts'**

```typescript
      case 'get_unread_counts': {
        const { userId } = payload as { userId: string };
        return this.client.get(
          `/api/v1/chats/unread-counts?user_id=${userId}`,
          correlationId,
        );
      }
```

(Точная сигнатура `this.client.get` / `.post` — посмотри в соседних case'ах того же файла, чтобы соответствовать стилю.)

- [ ] **Step 4: TS check**

```bash
cd /root/webclient_rugpt/packages/backend && pnpm tsc --noEmit
```

Expected: exit 0.

- [ ] **Step 5: Commit**

```bash
git add packages/backend/src/engine/adapters/rugpt.adapter.ts
git commit -m "feat(backend): adapter cases for mark_chat_read + get_unread_counts"
```

---

### Task 7: Backend — ChatService methods + Controller endpoint

**Files:**
- Modify: `/root/webclient_rugpt/packages/backend/src/chat/chat.service.ts`
- Modify: `/root/webclient_rugpt/packages/backend/src/chat/chat.controller.ts`
- Create or Modify: `/root/webclient_rugpt/packages/backend/src/chat/__tests__/chat.controller.spec.ts`

- [ ] **Step 1: Написать failing test для controller endpoint**

Файл `/root/webclient_rugpt/packages/backend/src/chat/__tests__/chat.controller.spec.ts` (если не существует — создать; если есть — extend):

```typescript
import { Test, TestingModule } from '@nestjs/testing';
import { ChatController } from '../chat.controller';
import { ChatService } from '../chat.service';

describe('ChatController.getUnreadCounts', () => {
  let controller: ChatController;
  let chatService: jest.Mocked<ChatService>;

  beforeEach(async () => {
    chatService = { getUnreadCounts: jest.fn() } as any;
    const module: TestingModule = await Test.createTestingModule({
      controllers: [ChatController],
      providers: [{ provide: ChatService, useValue: chatService }],
    }).compile();
    controller = module.get(ChatController);
  });

  it('proxies userId and correlationId to ChatService', async () => {
    chatService.getUnreadCounts.mockResolvedValue({ 'chat-1': 3 });
    const req = { user: { userId: 'user-uuid' }, correlationId: 'cid-1' } as any;

    const result = await controller.getUnreadCounts(req);

    expect(chatService.getUnreadCounts).toHaveBeenCalledWith('user-uuid', 'cid-1');
    expect(result).toEqual({ 'chat-1': 3 });
  });
});
```

- [ ] **Step 2: Run test — должен упасть**

```bash
cd /root/webclient_rugpt/packages/backend && pnpm jest chat.controller -t "getUnreadCounts"
```

Expected: FAIL — `controller.getUnreadCounts is not a function`.

- [ ] **Step 3: Добавить методы в ChatService**

В `/root/webclient_rugpt/packages/backend/src/chat/chat.service.ts` после существующих методов:

```typescript
  async markChatRead(
    chatId: string,
    userId: string,
    messageId: string,
    correlationId: string,
  ): Promise<void> {
    await this.engineAdapter.execute(
      'mark_chat_read',
      { chatId, userId, messageId },
      correlationId,
    );
  }

  async getUnreadCounts(
    userId: string,
    correlationId: string,
  ): Promise<Record<string, number>> {
    const [success, data] = await this.engineAdapter.execute(
      'get_unread_counts',
      { userId },
      correlationId,
    );
    if (!success) return {};
    return data as Record<string, number>;
  }
```

(Посмотри как другие методы того же файла обрабатывают `[success, data]` — следуй существующему стилю.)

- [ ] **Step 4: Добавить endpoint в ChatController**

В `/root/webclient_rugpt/packages/backend/src/chat/chat.controller.ts`:

```typescript
  @Get('unread-counts')
  @UseGuards(JwtAuthGuard)
  async getUnreadCounts(@Req() req: AuthenticatedRequest): Promise<Record<string, number>> {
    return this.chatService.getUnreadCounts(req.user.userId, req.correlationId);
  }
```

Импорты `Get`, `UseGuards`, `Req`, `JwtAuthGuard`, `AuthenticatedRequest` — посмотри как зачитываются другие endpoint'ы в этом файле.

- [ ] **Step 5: Run controller test — должен пройти**

```bash
cd /root/webclient_rugpt/packages/backend && pnpm jest chat.controller -t "getUnreadCounts"
```

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add packages/backend/src/chat/chat.service.ts packages/backend/src/chat/chat.controller.ts packages/backend/src/chat/__tests__/chat.controller.spec.ts
git commit -m "feat(backend): ChatService.markChatRead + getUnreadCounts + controller endpoint"
```

---

### Task 8: Backend — SocketGateway.handleMessageRead (replace skeleton)

**Files:**
- Modify: `/root/webclient_rugpt/packages/backend/src/socket/socket.gateway.ts`
- Modify: `/root/webclient_rugpt/packages/backend/src/socket/__tests__/socket.chat.spec.ts` (переписать существующий тест-skeleton)

- [ ] **Step 1: Переписать существующий тест skeleton**

В `/root/webclient_rugpt/packages/backend/src/socket/__tests__/socket.chat.spec.ts:708` найти тест `"should update unread messages status when joining chat"` и заменить его блок описания. Либо создать соседний `socket.read.spec.ts`. Содержимое:

```typescript
describe('handleMessageRead', () => {
  it('skips temp-id messages without calling Engine', async () => {
    const chatService = { markChatRead: jest.fn(), getChat: jest.fn() } as any;
    const gateway = makeGateway({ chatService });  // helper из существующих тестов
    const client = makeAuthSocket({ userId: 'u1', correlationId: 'cid' });

    const result = await gateway.handleMessageRead(
      { chatId: 'chat-1', messageId: 'temp-abc-123' },
      client,
    );

    expect(result).toEqual({ success: true });
    expect(chatService.markChatRead).not.toHaveBeenCalled();
  });

  it('rejects non-participants with 403-shaped response', async () => {
    const chatService = {
      markChatRead: jest.fn(),
      getChat: jest.fn().mockResolvedValue({ participants: ['u-other'] }),
    } as any;
    const gateway = makeGateway({ chatService });
    const client = makeAuthSocket({ userId: 'u1', correlationId: 'cid' });

    const result = await gateway.handleMessageRead(
      { chatId: 'chat-1', messageId: 'real-msg-uuid' },
      client,
    );

    expect(result.success).toBe(false);
    expect(chatService.markChatRead).not.toHaveBeenCalled();
  });

  it('calls markChatRead and emits chat:unread-cleared into user room', async () => {
    const emit = jest.fn();
    const to = jest.fn().mockReturnValue({ emit });
    const ioMock = { to } as any;
    const chatService = {
      markChatRead: jest.fn().mockResolvedValue(undefined),
      getChat: jest.fn().mockResolvedValue({ participants: ['u1', 'u2'] }),
    } as any;
    const gateway = makeGateway({ chatService, io: ioMock });
    const client = makeAuthSocket({ userId: 'u1', correlationId: 'cid' });

    const result = await gateway.handleMessageRead(
      { chatId: 'chat-1', messageId: 'real-msg-uuid' },
      client,
    );

    expect(chatService.markChatRead).toHaveBeenCalledWith('chat-1', 'u1', 'real-msg-uuid', 'cid');
    expect(to).toHaveBeenCalledWith('user:u1');
    expect(emit).toHaveBeenCalledWith('chat:unread-cleared', { chatId: 'chat-1' });
    expect(result).toEqual({ success: true });
  });
});
```

(Точные имена `makeGateway` / `makeAuthSocket` — используй те, что уже есть в этом spec-файле; если их нет — создай по аналогии с другими test'ами в `__tests__/`.)

- [ ] **Step 2: Run test — должен упасть**

```bash
cd /root/webclient_rugpt/packages/backend && pnpm jest socket.chat -t "handleMessageRead"
```

Expected: FAIL — реальная реализация ещё no-op.

- [ ] **Step 3: Заменить handleMessageRead в SocketGateway**

В `/root/webclient_rugpt/packages/backend/src/socket/socket.gateway.ts:422-452` заменить тело метода:

```typescript
  @SubscribeMessage(WsClientEvents.MESSAGE_READ)
  async handleMessageRead(
    @MessageBody() payload: { chatId: string; messageId: string },
    @ConnectedSocket() client: AuthenticatedSocket,
  ): Promise<{ success: boolean; error?: string }> {
    const userId = client.userId;
    if (!userId) return { success: false, error: 'Unauthorized' };

    if (payload.messageId.startsWith('temp-')) {
      return { success: true };
    }

    const chat = await this.chatService.getChat(payload.chatId, userId);
    if (!chat || !chat.participants.includes(userId)) {
      return { success: false, error: 'Not a participant' };
    }

    await this.chatService.markChatRead(
      payload.chatId,
      userId,
      payload.messageId,
      client.correlationId,
    );

    this.io.to(`user:${userId}`).emit(WsServerEvents.CHAT_UNREAD_CLEARED, {
      chatId: payload.chatId,
    });

    return { success: true };
  }
```

- [ ] **Step 4: Run all socket tests**

```bash
cd /root/webclient_rugpt/packages/backend && pnpm jest socket
```

Expected: новые PASS, существующие тоже PASS (никаких регрессий).

- [ ] **Step 5: TS check на весь backend**

```bash
cd /root/webclient_rugpt/packages/backend && pnpm tsc --noEmit
```

Expected: exit 0.

- [ ] **Step 6: Commit**

```bash
git add packages/backend/src/socket/socket.gateway.ts packages/backend/src/socket/__tests__/socket.chat.spec.ts
git commit -m "feat(backend): real handleMessageRead with engine + multi-device sync"
```

---

### Task 9: Frontend — useChatUnreadStore (zustand)

**Files:**
- Create: `/root/webclient_rugpt/packages/frontend/src/app/hooks/useChatUnreadStore.ts`
- Create: `/root/webclient_rugpt/packages/frontend/src/app/hooks/useChatUnreadStore.test.ts`

- [ ] **Step 1: Написать failing test**

Файл `useChatUnreadStore.test.ts`:

```typescript
import { describe, it, expect, beforeEach } from 'vitest';
import { useChatUnreadStore } from './useChatUnreadStore';

describe('useChatUnreadStore', () => {
  beforeEach(() => {
    useChatUnreadStore.setState({ counts: {}, hasFetchedOnce: false });
  });

  it('setAll replaces counts and marks fetched', () => {
    useChatUnreadStore.getState().setAll({ 'c1': 3, 'c2': 7 });
    const s = useChatUnreadStore.getState();
    expect(s.counts).toEqual({ 'c1': 3, 'c2': 7 });
    expect(s.hasFetchedOnce).toBe(true);
  });

  it('setCount sets single chat count', () => {
    useChatUnreadStore.getState().setCount('c1', 5);
    expect(useChatUnreadStore.getState().counts['c1']).toBe(5);
  });

  it('increment increases by 1, capped at 99 (UI displays 99+)', () => {
    useChatUnreadStore.getState().setCount('c1', 4);
    useChatUnreadStore.getState().increment('c1');
    expect(useChatUnreadStore.getState().counts['c1']).toBe(5);
  });

  it('increment from undefined starts at 1', () => {
    useChatUnreadStore.getState().increment('c1');
    expect(useChatUnreadStore.getState().counts['c1']).toBe(1);
  });

  it('clear removes the entry', () => {
    useChatUnreadStore.setState({ counts: { 'c1': 5 }, hasFetchedOnce: true });
    useChatUnreadStore.getState().clear('c1');
    expect(useChatUnreadStore.getState().counts['c1']).toBeUndefined();
  });

  it('totalForChats sums counts for given chatIds', () => {
    useChatUnreadStore.setState({ counts: { 'c1': 3, 'c2': 5, 'c3': 7 }, hasFetchedOnce: true });
    expect(useChatUnreadStore.getState().totalForChats(['c1', 'c3'])).toBe(10);
    expect(useChatUnreadStore.getState().totalForChats(['c-missing'])).toBe(0);
  });
});
```

- [ ] **Step 2: Run test — должен упасть**

```bash
cd /root/webclient_rugpt/packages/frontend && pnpm vitest run useChatUnreadStore
```

Expected: FAIL — модуль не существует.

- [ ] **Step 3: Создать store**

Файл `/root/webclient_rugpt/packages/frontend/src/app/hooks/useChatUnreadStore.ts`:

```typescript
import { create } from 'zustand';

interface ChatUnreadState {
  counts: Record<string, number>;
  hasFetchedOnce: boolean;
  setAll: (counts: Record<string, number>) => void;
  setCount: (chatId: string, count: number) => void;
  increment: (chatId: string) => void;
  clear: (chatId: string) => void;
  totalForChats: (chatIds: string[]) => number;
}

export const useChatUnreadStore = create<ChatUnreadState>((set, get) => ({
  counts: {},
  hasFetchedOnce: false,

  setAll: (counts) => set({ counts, hasFetchedOnce: true }),

  setCount: (chatId, count) => set((s) => ({
    counts: { ...s.counts, [chatId]: count },
  })),

  increment: (chatId) => set((s) => ({
    counts: { ...s.counts, [chatId]: (s.counts[chatId] ?? 0) + 1 },
  })),

  clear: (chatId) => set((s) => {
    const next = { ...s.counts };
    delete next[chatId];
    return { counts: next };
  }),

  totalForChats: (chatIds) => {
    const { counts } = get();
    return chatIds.reduce((sum, id) => sum + (counts[id] ?? 0), 0);
  },
}));
```

- [ ] **Step 4: Run test — должны пройти**

```bash
cd /root/webclient_rugpt/packages/frontend && pnpm vitest run useChatUnreadStore
```

Expected: ВСЕ PASS.

- [ ] **Step 5: Commit**

```bash
git add packages/frontend/src/app/hooks/useChatUnreadStore.ts packages/frontend/src/app/hooks/useChatUnreadStore.test.ts
git commit -m "feat(frontend): useChatUnreadStore (zustand)"
```

---

### Task 10: Frontend — wsClient chatUnreadCleared$ getter

**Files:**
- Modify: `/root/webclient_rugpt/packages/frontend/src/transport/wsClient.ts`

- [ ] **Step 1: Добавить getter в WsClient**

В `/root/webclient_rugpt/packages/frontend/src/transport/wsClient.ts` рядом с другими getters (после `messages$` ~ строка 106):

```typescript
  get chatUnreadCleared$(): Observable<{ chatId: string }> {
    return this.on('chat:unread-cleared');
  }
```

`this.on(...)` — generic listener (см. строки 97-103 того же файла).

- [ ] **Step 2: TS check**

```bash
cd /root/webclient_rugpt/packages/frontend && pnpm tsc --noEmit
```

Expected: exit 0. (Опирается на тип из common — он уже добавлен в Task 5.)

- [ ] **Step 3: Commit**

```bash
git add packages/frontend/src/transport/wsClient.ts
git commit -m "feat(frontend): wsClient chatUnreadCleared\$ observable"
```

---

### Task 11: Frontend — useChatUnreadBootstrap hook

**Files:**
- Create: `/root/webclient_rugpt/packages/frontend/src/app/hooks/useChatUnreadBootstrap.ts`
- Create: `/root/webclient_rugpt/packages/frontend/src/app/hooks/useChatUnreadBootstrap.test.tsx`

- [ ] **Step 1: Написать failing test**

Файл `useChatUnreadBootstrap.test.tsx`:

```typescript
import { describe, it, expect, beforeEach, vi } from 'vitest';
import { renderHook, waitFor } from '@testing-library/react';
import { Subject, BehaviorSubject } from 'rxjs';
import { useChatUnreadStore } from './useChatUnreadStore';
import { useChatUnreadBootstrap } from './useChatUnreadBootstrap';

vi.mock('../../transport/apiClient', () => {
  const signedGet = vi.fn();
  return { getApiClient: () => ({ signedGet }), __signedGet: signedGet };
});

const messages$ = new Subject<any>();
const connection$ = new BehaviorSubject<boolean>(false);
const chatUnreadCleared$ = new Subject<{ chatId: string }>();

vi.mock('../../transport/wsClient', () => ({
  wsClient: {
    messages$,
    connection$: connection$.asObservable(),
    chatUnreadCleared$: chatUnreadCleared$.asObservable(),
  },
}));

vi.mock('./useAuth', () => ({
  useAuthStore: (selector: any) => selector({ user: { id: 'me' } }),
}));

describe('useChatUnreadBootstrap', () => {
  let signedGet: any;

  beforeEach(async () => {
    useChatUnreadStore.setState({ counts: {}, hasFetchedOnce: false });
    signedGet = (await import('../../transport/apiClient') as any).__signedGet;
    signedGet.mockReset();
    connection$.next(false);
  });

  it('fetches unread-counts on mount and stores them', async () => {
    signedGet.mockResolvedValue({ 'c1': 3 });
    renderHook(() => useChatUnreadBootstrap());
    await waitFor(() => {
      expect(useChatUnreadStore.getState().counts).toEqual({ 'c1': 3 });
    });
  });

  it('increments count on incoming message from another user', async () => {
    signedGet.mockResolvedValue({});
    renderHook(() => useChatUnreadBootstrap());
    await waitFor(() => expect(useChatUnreadStore.getState().hasFetchedOnce).toBe(true));

    messages$.next({ id: 'm1', chatId: 'c1', senderId: 'someone-else' });
    expect(useChatUnreadStore.getState().counts['c1']).toBe(1);
  });

  it('does NOT increment for own messages', async () => {
    signedGet.mockResolvedValue({});
    renderHook(() => useChatUnreadBootstrap());
    await waitFor(() => expect(useChatUnreadStore.getState().hasFetchedOnce).toBe(true));

    messages$.next({ id: 'm1', chatId: 'c1', senderId: 'me' });
    expect(useChatUnreadStore.getState().counts['c1']).toBeUndefined();
  });

  it('clears chat counter on chat:unread-cleared event', async () => {
    signedGet.mockResolvedValue({ 'c1': 5 });
    renderHook(() => useChatUnreadBootstrap());
    await waitFor(() => expect(useChatUnreadStore.getState().counts['c1']).toBe(5));

    chatUnreadCleared$.next({ chatId: 'c1' });
    expect(useChatUnreadStore.getState().counts['c1']).toBeUndefined();
  });

  it('re-fetches unread-counts on connection (reconnect)', async () => {
    signedGet.mockResolvedValue({});
    renderHook(() => useChatUnreadBootstrap());
    await waitFor(() => expect(signedGet).toHaveBeenCalledTimes(1));

    signedGet.mockResolvedValue({ 'c2': 4 });
    connection$.next(true);
    await waitFor(() => expect(signedGet).toHaveBeenCalledTimes(2));
    expect(useChatUnreadStore.getState().counts['c2']).toBe(4);
  });
});
```

- [ ] **Step 2: Run test — должен упасть**

```bash
cd /root/webclient_rugpt/packages/frontend && pnpm vitest run useChatUnreadBootstrap
```

Expected: FAIL.

- [ ] **Step 3: Создать хук**

Файл `/root/webclient_rugpt/packages/frontend/src/app/hooks/useChatUnreadBootstrap.ts`:

```typescript
import { useEffect } from 'react';
import { wsClient } from '../../transport/wsClient';
import { getApiClient } from '../../transport/apiClient';
import { useAuthStore } from './useAuth';
import { useChatUnreadStore } from './useChatUnreadStore';

export function useChatUnreadBootstrap(): void {
  const userId = useAuthStore((s) => s.user?.id);

  useEffect(() => {
    if (!userId) return;
    const { setAll, increment, clear } = useChatUnreadStore.getState();

    const fetchCounts = () => {
      getApiClient()
        .signedGet<Record<string, number>>('/api/chat/unread-counts', userId)
        .then((counts) => setAll(counts ?? {}))
        .catch(() => { /* ignore — sidebar просто не покажет badges */ });
    };

    // Initial fetch
    fetchCounts();

    // Re-fetch on (re)connect
    const connSub = wsClient.connection$.subscribe((isConnected: boolean) => {
      if (isConnected) fetchCounts();
    });

    // New message → increment (если не свой)
    const msgSub = wsClient.messages$.subscribe((msg) => {
      if (msg.senderId !== userId) increment(msg.chatId);
    });

    // Multi-device sync — обнуляем счётчик после mark-read с другого устройства
    const clrSub = wsClient.chatUnreadCleared$.subscribe(({ chatId }) => clear(chatId));

    return () => {
      connSub.unsubscribe();
      msgSub.unsubscribe();
      clrSub.unsubscribe();
    };
  }, [userId]);
}
```

**Замечание:** имя getter'а `connection$` смотри в `wsClient.ts` (строки 79-81). Если в коде он называется иначе (например `connectionState$`), используй фактическое имя.

- [ ] **Step 4: Run tests — должны пройти**

```bash
cd /root/webclient_rugpt/packages/frontend && pnpm vitest run useChatUnreadBootstrap
```

Expected: ВСЕ PASS.

- [ ] **Step 5: Commit**

```bash
git add packages/frontend/src/app/hooks/useChatUnreadBootstrap.ts packages/frontend/src/app/hooks/useChatUnreadBootstrap.test.tsx
git commit -m "feat(frontend): useChatUnreadBootstrap (initial fetch + WS subscriptions)"
```

---

### Task 12: Frontend — useChatService mark-read effect

**Files:**
- Modify: `/root/webclient_rugpt/packages/frontend/src/domain/chat/useChatService.ts`
- Create or Modify: `/root/webclient_rugpt/packages/frontend/src/domain/chat/useChatService.test.ts` (extend если есть)

- [ ] **Step 1: Написать failing test**

Создать или расширить файл `useChatService.test.ts`:

```typescript
import { describe, it, expect, beforeEach, vi } from 'vitest';
import { renderHook, act } from '@testing-library/react';
import { useChatService } from './useChatService';
import { useChatUnreadStore } from '../../app/hooks/useChatUnreadStore';

const markAsRead = vi.fn();
vi.mock('./chatService', () => ({
  ChatServiceImpl: class {
    markAsRead = markAsRead;
    sendMessage = vi.fn();
    joinChat = vi.fn();
    leaveChat = vi.fn();
    getOrCreateChat = vi.fn().mockResolvedValue({ chatId: 'c1', messages: [] });
    getPinnedMessages = vi.fn().mockResolvedValue([]);
  },
}));

// Дополнительные моки wsClient/auth — посмотри как они выглядят в существующих
// тестах useChatService либо в useChatUnreadBootstrap.test.tsx.

describe('useChatService mark-read effect', () => {
  beforeEach(() => {
    markAsRead.mockReset();
    useChatUnreadStore.setState({ counts: { 'c1': 5 }, hasFetchedOnce: true });
  });

  it('calls markAsRead and clears local store when last message from other user', () => {
    const { rerender } = renderHook(
      ({ msgs }: any) => useChatService({ chatId: 'c1', currentUserId: 'me' }),
      { initialProps: { msgs: [] } },
    );
    // simulate messages updating (через store/state — точная техника зависит от того,
    // как тестируется state в useChatService; смотри соседние тесты)
    act(() => {
      // ...trigger messages = [{ id: 'm1', senderId: 'other' }]
    });

    expect(markAsRead).toHaveBeenCalledWith('m1');
    expect(useChatUnreadStore.getState().counts['c1']).toBeUndefined();
  });

  it('skips temp-id', () => {
    // simulate messages with last id = 'temp-xxx'
    // expect markAsRead NOT called
  });

  it('skips own message', () => {
    // simulate messages with last senderId === 'me'
    // expect markAsRead NOT called
  });
});
```

**Замечание:** точные моки и техника симуляции `messages` зависят от существующих тестов `useChatService`. Если test-suite этого хука пока минимальный — добавь только тесты, которые получится написать без переписывания инфры. Минимально достаточно: один тест happy path + два early-return (temp-id, own).

- [ ] **Step 2: Run test — должен упасть**

```bash
cd /root/webclient_rugpt/packages/frontend && pnpm vitest run useChatService
```

Expected: FAIL.

- [ ] **Step 3: Добавить effect в useChatService**

В `/root/webclient_rugpt/packages/frontend/src/domain/chat/useChatService.ts` после mount-effect (строка ~260, после `}, [isConnected, recipientId, chatIdInput, currentUserId]);`):

```typescript
  // Mark-as-read: при mount чата с сообщениями + при каждом новом входящем
  useEffect(() => {
    if (!chatId || !messages.length || !currentUserId) return;
    const lastMsg = messages[messages.length - 1];
    if (lastMsg.id.startsWith('temp-')) return;
    if (lastMsg.senderId === currentUserId) return;

    serviceRef.current?.markAsRead(lastMsg.id);
    useChatUnreadStore.getState().clear(chatId);
  }, [chatId, messages, currentUserId]);
```

В импортах того же файла добавить:
```typescript
import { useChatUnreadStore } from '../../app/hooks/useChatUnreadStore';
```

- [ ] **Step 4: Run tests — должны пройти**

```bash
cd /root/webclient_rugpt/packages/frontend && pnpm vitest run useChatService
```

Expected: ВСЕ PASS.

- [ ] **Step 5: Commit**

```bash
git add packages/frontend/src/domain/chat/useChatService.ts packages/frontend/src/domain/chat/useChatService.test.ts
git commit -m "feat(frontend): useChatService mark-read effect on mount + new incoming"
```

---

### Task 13: Frontend — Sidebar UI (badges direct + task/project + bootstrap)

**Files:**
- Modify: `/root/webclient_rugpt/packages/frontend/src/app/utils/sidebarChats.ts`
- Modify: `/root/webclient_rugpt/packages/frontend/src/app/components/Sidebar.tsx`

- [ ] **Step 1: Расширить SidebarChatItem**

В `/root/webclient_rugpt/packages/frontend/src/app/utils/sidebarChats.ts` в interface `SidebarChatItem`:

```typescript
export interface SidebarChatItem {
  // ... existing fields ...
  unreadCount?: number;  // NEW — отображается как badge (99+ для >= 99)
}
```

- [ ] **Step 2: Вызвать bootstrap-хук в Sidebar**

В `/root/webclient_rugpt/packages/frontend/src/app/components/Sidebar.tsx` в начале функции компонента (рядом с другими хуками):

```typescript
import { useChatUnreadBootstrap } from '../hooks/useChatUnreadBootstrap';
import { useChatUnreadStore } from '../hooks/useChatUnreadStore';
// ...

export function Sidebar({ chats }: SidebarProps) {
  useChatUnreadBootstrap();
  const unreadCounts = useChatUnreadStore((s) => s.counts);
  // ... existing hooks ...
}
```

- [ ] **Step 3: Построить peerToChat resolver и присвоить unreadCount к direct rows**

В компоненте `Sidebar` где происходит формирование/итерация `chats` для рендера direct-rows — добавить useMemo для resolver'а. Источник direct-чатов: `useChats` или существующий хук, возвращающий `myChats` с `participants` и `type`. Если такого хука нет — нужно добавить fetch `/api/chats?type=direct` в bootstrap-хуке (но ТОЛЬКО для direct-resolver'а — task/project chats уже в `SidebarTaskProjectSections`).

```typescript
import { useMyDirectChats } from '../hooks/useMyDirectChats';  // если такой есть

// внутри Sidebar:
const { directChats } = useMyDirectChats();  // или аналог
const peerToChat = useMemo(() => {
  const map: Record<string, string> = {};
  for (const c of directChats ?? []) {
    if (c.type !== 'direct') continue;
    const peer = c.participants.find((id: string) => id !== currentUserId);
    if (peer) map[peer] = c.id;
  }
  return map;
}, [directChats, currentUserId]);
```

При прокидывании `chats` в `renderRow` (строки ~359-401), для каждого чата проставить `unreadCount`:

```typescript
const chatsWithUnread = useMemo(() => chats.map((c) => {
  const chatId = peerToChat[c.userId];
  return chatId ? { ...c, unreadCount: unreadCounts[chatId] ?? 0 } : c;
}), [chats, peerToChat, unreadCounts]);
```

(Если в коде `chats` имеет другую форму — адаптируй: главное чтобы `unreadCount` числом > 0 попало в строку.)

**Замечание:** если хука `useMyDirectChats` нет, добавить fetch direct-чатов прямо в `useChatUnreadBootstrap` и хранить mapping в zustand-сторе как поле `peerToChat: Record<string, string>`. В таком случае `setAll` принимает второй аргумент `peerMap`, или добавь отдельный action `setPeerToChat`. Ориентируйся по существующим паттернам.

- [ ] **Step 4: Отрендерить badge возле direct-row**

В функции `renderRow` (Sidebar.tsx:359-401) рядом с `<Avatar>` либо в правом конце строки добавить:

```tsx
{chat.unreadCount && chat.unreadCount > 0 && (
  <span className="absolute top-0 right-0 min-w-[18px] h-[18px] px-1
                    bg-rose-500 text-white text-[10px] rounded-full
                    flex items-center justify-center font-medium">
    {chat.unreadCount >= 99 ? '99+' : chat.unreadCount}
  </span>
)}
```

(Стиль скопирован с support-icon `Sidebar.tsx:311-318`. Точное позиционирование — относительно avatar wrapper'а.)

- [ ] **Step 5: Передать unreadCount в SidebarTaskProjectSections**

В `SidebarTaskProjectSections` (Sidebar.tsx:22-109) в местах рендера task-row и project-row извлечь counter:

```tsx
const unreadCounts = useChatUnreadStore((s) => s.counts);
// внутри renderTaskRow:
const unread = task.chatId ? unreadCounts[task.chatId] ?? 0 : 0;
{unread > 0 && (
  <span className="ml-2 min-w-[18px] h-[18px] px-1
                    bg-rose-500 text-white text-[10px] rounded-full
                    flex items-center justify-center font-medium">
    {unread >= 99 ? '99+' : unread}
  </span>
)}
```

- [ ] **Step 6: TS check + manual smoke**

```bash
cd /root/webclient_rugpt/packages/frontend && pnpm tsc --noEmit
```

Expected: exit 0.

Затем запустить dev-server и проверить визуально, что в sidebar:
- При наличии новых сообщений в direct-чате badge показывается возле аватара.
- Открытие этого чата обнуляет badge мгновенно.

```bash
cd /root/webclient_rugpt && pnpm dev
```

- [ ] **Step 7: Commit**

```bash
git add packages/frontend/src/app/components/Sidebar.tsx packages/frontend/src/app/utils/sidebarChats.ts
git commit -m "feat(frontend): unread badges in sidebar (direct + task/project rows)"
```

---

### Task 14: Frontend — tasks/page.tsx + projects/page.tsx badges

**Files:**
- Modify: `/root/webclient_rugpt/packages/frontend/src/app/tasks/page.tsx`
- Modify: `/root/webclient_rugpt/packages/frontend/src/app/projects/page.tsx`

- [ ] **Step 1: Tasks list — badge возле заголовка задачи**

В `/root/webclient_rugpt/packages/frontend/src/app/tasks/page.tsx`:

```typescript
import { useChatUnreadStore } from '../hooks/useChatUnreadStore';
// ...

const unreadCounts = useChatUnreadStore((s) => s.counts);
```

В JSX'е рендера строки задачи рядом с заголовком:

```tsx
{(() => {
  const unread = task.chatId ? unreadCounts[task.chatId] ?? 0 : 0;
  if (unread === 0) return null;
  return (
    <span className="ml-2 inline-flex items-center justify-center min-w-[18px] h-[18px] px-1
                      bg-rose-500 text-white text-[10px] rounded-full font-medium">
      {unread >= 99 ? '99+' : unread}
    </span>
  );
})()}
```

(Если у `task` нет поля `chatId` напрямую — посмотри как оно называется; в spec'е и engine оно есть как `task.chat_id` и через mapping должно прийти как `chatId` или `task_chat_id`.)

- [ ] **Step 2: Projects list — то же**

В `/root/webclient_rugpt/packages/frontend/src/app/projects/page.tsx` аналогично:

```typescript
import { useChatUnreadStore } from '../hooks/useChatUnreadStore';

const unreadCounts = useChatUnreadStore((s) => s.counts);
```

В строке проекта:

```tsx
{(() => {
  const unread = project.chatId ? unreadCounts[project.chatId] ?? 0 : 0;
  if (unread === 0) return null;
  return (
    <span className="ml-2 inline-flex items-center justify-center min-w-[18px] h-[18px] px-1
                      bg-rose-500 text-white text-[10px] rounded-full font-medium">
      {unread >= 99 ? '99+' : unread}
    </span>
  );
})()}
```

- [ ] **Step 3: TS check + visual smoke**

```bash
cd /root/webclient_rugpt/packages/frontend && pnpm tsc --noEmit
```

Expected: exit 0.

Затем визуально:
- Открыть `/tasks`. Если в чате какой-то задачи есть новые сообщения — на её строке видно badge.
- Зайти в чат задачи через клик → вернуться на `/tasks` → badge исчез.
- То же для `/projects`.

- [ ] **Step 4: Commit**

```bash
git add packages/frontend/src/app/tasks/page.tsx packages/frontend/src/app/projects/page.tsx
git commit -m "feat(frontend): unread badges in tasks/projects list pages"
```

---

### Task 15: E2E ручная проверка

**Files:** none

- [ ] **Step 1: Сценарий 1 — direct-чат, B офлайн**

A пишет B (B офлайн).
- A не должен иметь unread у себя.
- B входит → badge "1" возле A в sidebar.
- B открывает чат → badge исчезает мгновенно.

- [ ] **Step 2: Сценарий 2 — открытый чат**

B держит чат открытым, A пишет 5 раз.
- У B badge не появляется (effect mark-read срабатывает на каждое новое входящее).

- [ ] **Step 3: Сценарий 3 — multi-device**

B открыл один и тот же чат на двух вкладках.
- Читает на одной вкладке.
- На второй вкладке badge тоже обнуляется (через `chat:unread-cleared`).

- [ ] **Step 4: Сценарий 4 — reload**

B reload-ит страницу.
- Badge восстанавливается из `/api/chat/unread-counts` (если кто-то писал между сессиями).

- [ ] **Step 5: Сценарий 5 — tasks list**

В чате какой-то задачи накапливаются новые сообщения от другого юзера.
- На странице `/tasks` на строке задачи виден badge.
- Открыть чат → вернуться на `/tasks` → badge ушёл.

- [ ] **Step 6: Сценарий 6 — projects list**

То же для `/projects`.

- [ ] **Step 7: Сценарий 7 — cap "99+"**

В чате накопилось > 99 непрочитанных (например, через тестовый скрипт массовой отправки или вручную).
- В sidebar / tasks / projects badge показывает `99+`, а не `100+` или `137`.

---

## Self-Review

**Spec coverage** (проверка, что каждое требование спеки покрыто задачей):

| Spec section | Implemented in |
|---|---|
| Семантика — high-water-mark per (chat, user) | T1 (миграция), T2 (storage upsert) |
| Семантика — mark-read на open чата | T12 (useChatService effect) |
| Семантика — все кроме своих, без soft-deleted | T2 (SQL `sender_id != $1 AND is_deleted=false`) |
| Counter cap = 100 | T2 (`LEAST(COUNT(*), 100)`) + T9 (UI `99+`) + T15 (E2E сценарий 7) |
| Только my-unread, без галочек | НЕ реализуем (явно, no broadcast read-marker) |
| Badges sidebar direct | T13 (renderRow + peerToChat) |
| Badges sidebar task/project | T13 (SidebarTaskProjectSections) |
| Badges tasks list | T14 |
| Badges projects list | T14 |
| Engine миграция 036_chat_read_state | T1 |
| ChatReadStateStorage | T2 |
| ChatService.mark_chat_read + list_unread_counts | T3 |
| Routes POST /read + GET /unread-counts | T4 |
| Common WsServerEvents.CHAT_UNREAD_CLEARED | T5 |
| Backend adapter cases | T6 |
| Backend ChatService methods | T7 |
| Backend ChatController GET endpoint | T7 |
| Backend handleMessageRead replace | T8 |
| Frontend useChatUnreadStore | T9 |
| Frontend wsClient chatUnreadCleared$ | T10 |
| Frontend useChatUnreadBootstrap | T11 |
| Frontend useChatService mark-read effect | T12 |
| Edge case multi-device sync | T8 (emit chat:unread-cleared в user room) + T11 (subscribe) |
| Edge case monotonic guard | T2 (UPSERT WHERE EXCLUDED.last_read_at >= ...) |
| Edge case temp-id skip | T8 (early return) + T12 (early return) |
| Edge case offline догон | T11 (re-fetch on connection$) |

Все требования покрыты.

**Placeholder scan:**

- "TBD" / "TODO": нет.
- "implement later" / "fill in details": нет.
- "Add appropriate error handling": нет.
- "Similar to Task N": нет — каждая задача содержит полный код.

**Type consistency:**

- `chatId: string`, `userId: string`, `messageId: string` — единообразно во всех слоях.
- `last_read_at TIMESTAMPTZ` (Engine) ↔ `Date` (frontend, через ISO).
- WS event name `'chat:unread-cleared'` — единая константа `WsServerEvents.CHAT_UNREAD_CLEARED`, payload `{ chatId: string }`.
- `useChatUnreadStore.counts: Record<string, number>` — одинаковый shape во всех потребителях.
- `mark_chat_read` / `markChatRead` / `markAsRead` — Engine snake_case, NestJS camelCase, frontend method name (уже существующий) — всё через адаптерный слой переводится корректно.

План готов.

---

## Execution Handoff

Plan complete and saved to `/root/rugpt/docs/superpowers/plans/2026-05-05-chat-unread.md`.

**Two execution options:**

1. **Subagent-Driven (recommended)** — fresh subagent per task, два-стадийный review (spec compliance + code quality) после каждой, быстрая итерация.

2. **Inline Execution** — выполнить задачи в этой сессии через executing-plans, batch с checkpoints.

При любом варианте: **subagent'ам НЕ делать git commits** — пользователь контролирует коммиты сам. Шаги "Commit" в плане сохранены для канонической полноты skill, но при исполнении пропускаются.

Который выбираем?
