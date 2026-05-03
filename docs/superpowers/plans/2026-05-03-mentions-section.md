# Раздел «Упоминания» Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.
>
> **Note:** этот план **намеренно не содержит шагов `git commit`** — пользователь предпочитает контролировать коммиты вручную. После каждой задачи делается verify, но не commit.

**Goal:** Дать пользователю in-app уведомления на упоминания (`@user` и `@@user`) и возможность ответить из раздела «Упоминания» в исходный чат без вступления в его участники, ровно один раз на одно mentioning-сообщение.

**Architecture:** Engine создаёт `in_app_notifications` с типом `"mention"` при `@user`-упоминании в `routes/chats.py:send_message`. Новый endpoint `POST /chats/messages/{id}/reply` валидирует «sender упомянут в сообщении» и вставляет ответ в исходный чат через существующий `chat_service.send_message` без модификации `participants`. Single-use защищается SELECT-перед-INSERT через новый storage-метод `find_reply`. На фронте `/mentions` получает два таба: «Меня» (новый, in-app notifications с `type=mention`) и «Моя роль» (текущий `MentionReviewList`). Колокольчик-каунтер суммирует unread mentions + pending validations, polling 30 сек.

**Tech Stack:** Python 3.10 / FastAPI / asyncpg (engine); NestJS 11 / TypeScript / Axios (webclient backend); Next.js 15 / React 19 / Zustand (frontend); pytest (engine tests); jest (backend tests).

---

## File Structure

**Engine (`/root/rugpt`):**
- `src/engine/storage/message_storage.py` — modify: add `find_reply()`
- `src/engine/storage/in_app_notification_storage.py` — modify: add `type` filter to `list_by_user()`
- `src/engine/services/in_app_notification_service.py` — modify: pass `type` arg in `list_by_user()`
- `src/engine/routes/in_app_notifications.py` — modify: add `type` query param
- `src/engine/routes/chats.py` — modify: notification creation + new endpoint + helper

**Webclient backend (`/root/webclient_rugpt/packages/backend/src`):**
- `engine/adapters/rugpt.adapter.ts` — modify: extend `get_in_app_notifications`, new `reply_to_mention` case
- `in-app-notification/in-app-notification.controller.ts` — modify: `type` query
- `in-app-notification/in-app-notification.service.ts` — modify: pass `type`
- `chat/chat.controller.ts` — modify: new endpoint `POST messages/:id/reply`
- `chat/chat.service.ts` — modify: new method `replyToMention`

**Frontend (`/root/webclient_rugpt/packages/frontend/src/app`):**
- `hooks/useMyMentions.ts` — create
- `hooks/usePendingValidationCount.ts` — create
- `hooks/useNotifications.ts` — modify: bell counter sum
- `components/MyMentionsList.tsx` — create
- `components/NotificationDropdown.tsx` — modify: href for `mention` type
- `mentions/page.tsx` — modify: refactor with two tabs

---

## Task 1: Engine `MessageStorage.find_reply`

**Files:**
- Modify: `/root/rugpt/src/engine/storage/message_storage.py` (добавить метод)
- Test: `/root/rugpt/tests/test_message_storage_find_reply.py` (создать)

- [ ] **Step 1.1: Написать failing test**

Создай `/root/rugpt/tests/test_message_storage_find_reply.py`:

```python
"""Test MessageStorage.find_reply — used by reply-to-mention single-use guard."""
import pytest
from uuid import uuid4
from unittest.mock import AsyncMock

from src.engine.storage.message_storage import MessageStorage


@pytest.mark.asyncio
async def test_find_reply_returns_existing_reply():
    storage = MessageStorage(pool=None)
    storage.fetch_one = AsyncMock(return_value={
        "id": uuid4(), "chat_id": uuid4(), "sender_id": uuid4(),
        "sender_type": "user", "content": "ok", "mentions": "[]",
        "reply_to_id": uuid4(), "ai_is_valid": True, "ai_edited": False,
        "is_deleted": False, "created_at": None, "updated_at": None,
    })
    reply_to_id = uuid4()
    sender_id = uuid4()
    result = await storage.find_reply(reply_to_id, sender_id)
    assert result is not None
    storage.fetch_one.assert_awaited_once()


@pytest.mark.asyncio
async def test_find_reply_returns_none_when_no_match():
    storage = MessageStorage(pool=None)
    storage.fetch_one = AsyncMock(return_value=None)
    result = await storage.find_reply(uuid4(), uuid4())
    assert result is None
```

- [ ] **Step 1.2: Запустить тест — должен упасть**

Run: `cd /root/rugpt && venv/bin/pytest tests/test_message_storage_find_reply.py -v`
Expected: FAIL — `AttributeError: 'MessageStorage' object has no attribute 'find_reply'`

- [ ] **Step 1.3: Реализовать метод**

В `/root/rugpt/src/engine/storage/message_storage.py` добавить (после существующих list-методов):

```python
    async def find_reply(self, reply_to_id: UUID, sender_id: UUID) -> Optional[Message]:
        """Find an existing reply by sender to a specific message.

        Used by the reply-to-mention endpoint to enforce single-use semantics:
        a user may reply to a given mentioning message at most once via this path.
        """
        row = await self.fetch_one(
            "SELECT * FROM messages WHERE reply_to_id = $1 AND sender_id = $2 LIMIT 1",
            reply_to_id, sender_id,
        )
        return self._row_to_message(row) if row else None
```

- [ ] **Step 1.4: Запустить тест — должен пройти**

Run: `cd /root/rugpt && venv/bin/pytest tests/test_message_storage_find_reply.py -v`
Expected: 2 passed.

---

## Task 2: Engine — `type` фильтр в `InAppNotificationStorage.list_by_user`

**Files:**
- Modify: `/root/rugpt/src/engine/storage/in_app_notification_storage.py`
- Test: `/root/rugpt/tests/test_in_app_notification_storage_filter.py` (создать)

- [ ] **Step 2.1: Написать failing test**

Создай `/root/rugpt/tests/test_in_app_notification_storage_filter.py`:

```python
"""Test InAppNotificationStorage.list_by_user with optional type filter."""
import pytest
from uuid import uuid4
from unittest.mock import AsyncMock

from src.engine.storage.in_app_notification_storage import InAppNotificationStorage


@pytest.mark.asyncio
async def test_list_by_user_passes_type_to_sql():
    storage = InAppNotificationStorage(pool=None)
    storage.fetch = AsyncMock(return_value=[])
    user_id = uuid4()
    await storage.list_by_user(user_id, type="mention", limit=10, offset=0, unread_only=False)
    args = storage.fetch.call_args
    assert "mention" in args.args, f"type 'mention' not passed to fetch: {args}"


@pytest.mark.asyncio
async def test_list_by_user_without_type_skips_filter():
    storage = InAppNotificationStorage(pool=None)
    storage.fetch = AsyncMock(return_value=[])
    user_id = uuid4()
    await storage.list_by_user(user_id, type=None, limit=10, offset=0, unread_only=False)
    args = storage.fetch.call_args
    assert "mention" not in args.args
```

- [ ] **Step 2.2: Запустить — должны упасть**

Run: `cd /root/rugpt && venv/bin/pytest tests/test_in_app_notification_storage_filter.py -v`
Expected: FAIL — `TypeError: list_by_user() got an unexpected keyword argument 'type'` (или подобное).

- [ ] **Step 2.3: Прочитать текущий метод**

Run: `grep -n "list_by_user" /root/rugpt/src/engine/storage/in_app_notification_storage.py`

Открыть метод и понять текущую сигнатуру и SQL.

- [ ] **Step 2.4: Расширить метод**

Заменить `list_by_user` в `/root/rugpt/src/engine/storage/in_app_notification_storage.py` на:

```python
    async def list_by_user(
        self,
        user_id: UUID,
        type: Optional[str] = None,
        limit: int = 50,
        offset: int = 0,
        unread_only: bool = False,
    ) -> List[InAppNotification]:
        """List notifications for a user. `type` фильтрует по полю type (NULL = все)."""
        clauses = ["user_id = $1"]
        params: list = [user_id]
        if unread_only:
            clauses.append("is_read = FALSE")
        if type is not None:
            params.append(type)
            clauses.append(f"type = ${len(params)}")
        params.append(limit)
        params.append(offset)
        query = (
            f"SELECT * FROM in_app_notifications "
            f"WHERE {' AND '.join(clauses)} "
            f"ORDER BY created_at DESC "
            f"LIMIT ${len(params) - 1} OFFSET ${len(params)}"
        )
        rows = await self.fetch(query, *params)
        return [self._row_to_notification(r) for r in rows]
```

Импорт `Optional` уже должен быть в файле; если нет — добавить `from typing import Optional, List`.

- [ ] **Step 2.5: Запустить тест — должен пройти**

Run: `cd /root/rugpt && venv/bin/pytest tests/test_in_app_notification_storage_filter.py -v`
Expected: 2 passed.

---

## Task 3: Engine — `type` arg в `InAppNotificationService.list_by_user`

**Files:**
- Modify: `/root/rugpt/src/engine/services/in_app_notification_service.py`

- [ ] **Step 3.1: Прочитать текущий метод**

Run: `grep -n "list_by_user\|def list" /root/rugpt/src/engine/services/in_app_notification_service.py`

- [ ] **Step 3.2: Расширить сервис**

В `/root/rugpt/src/engine/services/in_app_notification_service.py` найти метод `list_by_user` и заменить на:

```python
    async def list_by_user(
        self,
        user_id: UUID,
        type: Optional[str] = None,
        limit: int = 50,
        offset: int = 0,
        unread_only: bool = False,
    ) -> List[InAppNotification]:
        """List notifications for a user. `type` опциональный фильтр."""
        return await self.storage.list_by_user(
            user_id=user_id,
            type=type,
            limit=limit,
            offset=offset,
            unread_only=unread_only,
        )
```

Проверить что `Optional` импортирован. Если в файле уже есть `from typing import ...`, добавить туда `Optional`.

- [ ] **Step 3.3: Verify — синтаксический прогон**

Run: `cd /root/rugpt && venv/bin/python -c "from src.engine.services.in_app_notification_service import InAppNotificationService; print('ok')"`
Expected: `ok`.

---

## Task 4: Engine — `type` query в `/in-app-notifications` route

**Files:**
- Modify: `/root/rugpt/src/engine/routes/in_app_notifications.py`

- [ ] **Step 4.1: Прочитать текущий endpoint**

Run: `sed -n '40,70p' /root/rugpt/src/engine/routes/in_app_notifications.py`

- [ ] **Step 4.2: Добавить query-параметр**

Заменить сигнатуру `list_notifications` в `/root/rugpt/src/engine/routes/in_app_notifications.py`. Найти `@router.get("", response_model=List[NotificationResponse])` — добавить параметр `type`:

```python
@router.get("", response_model=List[NotificationResponse])
async def list_notifications(
    user_id: UUID,
    type: Optional[str] = Query(None, description="Filter by notification type (e.g. 'mention')"),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    unread_only: bool = Query(False),
    engine: EngineService = Depends(get_engine),
):
    """List in-app notifications for a user, ordered newest first."""
    notifications = await engine.in_app_notification_service.list_by_user(
        user_id=user_id,
        type=type,
        limit=limit,
        offset=offset,
        unread_only=unread_only,
    )
    return [NotificationResponse(**n.to_dict()) for n in notifications]
```

Если `Optional` или `Query` не импортированы — добавить в импорты сверху файла.

- [ ] **Step 4.3: Verify import**

Run: `cd /root/rugpt && venv/bin/python -c "from src.engine.routes.in_app_notifications import router; print(router.routes)"`
Expected: список роутов без traceback.

---

## Task 5: Engine — нотификация при `@user`-mention в `send_message`

**Files:**
- Modify: `/root/rugpt/src/engine/routes/chats.py` (после `send_message`-вызова)
- Test: `/root/rugpt/tests/test_chat_route_mention_notification.py` (создать)

- [ ] **Step 5.1: Написать failing test (мок storage)**

Создай `/root/rugpt/tests/test_chat_route_mention_notification.py`:

```python
"""Test that @user mentions trigger in-app notifications via send_message route."""
import pytest
from uuid import uuid4
from unittest.mock import AsyncMock, MagicMock

from src.engine.models.message import Mention, MentionType


@pytest.mark.asyncio
async def test_user_mention_creates_notification():
    """When @anna is mentioned by Bob, notification with type='mention' is created for Anna."""
    from src.engine.routes import chats as chats_route

    bob_id = uuid4()
    anna_id = uuid4()
    org_id = uuid4()
    msg_id = uuid4()
    chat_id = uuid4()

    fake_engine = MagicMock()
    fake_engine.support_ticket_service = None
    fake_engine.chat_service.get_chat = AsyncMock(return_value=MagicMock(id=chat_id))
    fake_engine.mention_service.resolve_mentions = AsyncMock(
        return_value=[Mention(type=MentionType.USER, user_id=anna_id, username="anna", position=0)]
    )
    fake_engine.chat_service.send_message = AsyncMock(
        return_value=MagicMock(id=msg_id, to_dict=lambda: {"id": str(msg_id)})
    )
    fake_engine.user_storage.get_by_id = AsyncMock(
        return_value=MagicMock(username="bob", id=bob_id)
    )
    fake_engine.in_app_notification_service.create = AsyncMock()
    fake_engine.ai_service.process_ai_mentions = AsyncMock(return_value=[])
    fake_engine.ai_service._is_async_mode = lambda: False
    fake_engine.ai_service.try_auto_respond = AsyncMock(return_value=None)
    fake_engine.chat_storage.is_ai_direct_chat = AsyncMock(return_value=False)

    request = MagicMock(content="@anna привет", file_ids=None, reply_to_id=None)
    await chats_route.send_message(
        chat_id=chat_id,
        request=request,
        user_id=bob_id,
        org_id=org_id,
        engine=fake_engine,
    )

    fake_engine.in_app_notification_service.create.assert_awaited_once()
    kwargs = fake_engine.in_app_notification_service.create.call_args.kwargs
    assert kwargs["user_id"] == anna_id
    assert kwargs["type"] == "mention"
    assert kwargs["reference_type"] == "message"
    assert kwargs["reference_id"] == msg_id


@pytest.mark.asyncio
async def test_self_mention_skipped():
    """Bob's @bob in his own message does not notify Bob."""
    from src.engine.routes import chats as chats_route

    bob_id = uuid4()
    org_id = uuid4()
    chat_id = uuid4()

    fake_engine = MagicMock()
    fake_engine.support_ticket_service = None
    fake_engine.chat_service.get_chat = AsyncMock(return_value=MagicMock(id=chat_id))
    fake_engine.mention_service.resolve_mentions = AsyncMock(
        return_value=[Mention(type=MentionType.USER, user_id=bob_id, username="bob", position=0)]
    )
    fake_engine.chat_service.send_message = AsyncMock(
        return_value=MagicMock(id=uuid4(), to_dict=lambda: {})
    )
    fake_engine.user_storage.get_by_id = AsyncMock(return_value=MagicMock(username="bob", id=bob_id))
    fake_engine.in_app_notification_service.create = AsyncMock()
    fake_engine.ai_service.process_ai_mentions = AsyncMock(return_value=[])
    fake_engine.ai_service._is_async_mode = lambda: False
    fake_engine.ai_service.try_auto_respond = AsyncMock(return_value=None)
    fake_engine.chat_storage.is_ai_direct_chat = AsyncMock(return_value=False)

    request = MagicMock(content="@bob себе", file_ids=None, reply_to_id=None)
    await chats_route.send_message(
        chat_id=chat_id, request=request, user_id=bob_id, org_id=org_id, engine=fake_engine,
    )
    fake_engine.in_app_notification_service.create.assert_not_awaited()


@pytest.mark.asyncio
async def test_ai_role_mention_does_not_create_notification():
    """@@anna goes through AI flow, not in-app notification."""
    from src.engine.routes import chats as chats_route

    bob_id = uuid4()
    anna_id = uuid4()
    org_id = uuid4()
    chat_id = uuid4()

    fake_engine = MagicMock()
    fake_engine.support_ticket_service = None
    fake_engine.chat_service.get_chat = AsyncMock(return_value=MagicMock(id=chat_id))
    fake_engine.mention_service.resolve_mentions = AsyncMock(
        return_value=[Mention(type=MentionType.AI_ROLE, user_id=anna_id, username="anna", position=0)]
    )
    fake_engine.chat_service.send_message = AsyncMock(return_value=MagicMock(id=uuid4(), to_dict=lambda: {}))
    fake_engine.user_storage.get_by_id = AsyncMock(return_value=MagicMock(username="bob", id=bob_id))
    fake_engine.in_app_notification_service.create = AsyncMock()
    fake_engine.ai_service.process_ai_mentions = AsyncMock(return_value=[])
    fake_engine.ai_service._is_async_mode = lambda: False
    fake_engine.ai_service.try_auto_respond = AsyncMock(return_value=None)
    fake_engine.chat_storage.is_ai_direct_chat = AsyncMock(return_value=False)

    request = MagicMock(content="@@anna договор", file_ids=None, reply_to_id=None)
    await chats_route.send_message(
        chat_id=chat_id, request=request, user_id=bob_id, org_id=org_id, engine=fake_engine,
    )
    fake_engine.in_app_notification_service.create.assert_not_awaited()
```

- [ ] **Step 5.2: Запустить — должны упасть**

Run: `cd /root/rugpt && venv/bin/pytest tests/test_chat_route_mention_notification.py -v`
Expected: FAIL — нотификация не создаётся (или другие ошибки в зависимости от текущего состояния).

- [ ] **Step 5.3: Реализовать**

В `/root/rugpt/src/engine/routes/chats.py` найти `send_message` функцию (около строки 245). Сразу после строки `message = await engine.chat_service.send_message(...)` (≈ строка 275-282) и **до** блока `# Process @@ mentions -> AI responses` добавить:

```python
    # Создаём in-app notifications для @user (но не для self-mention).
    # @@ai_role идёт по другому пути ниже через process_ai_mentions.
    sender = await engine.user_storage.get_by_id(user_id)
    sender_label = f"@{sender.username}" if sender else "пользователь"
    for m in mentions:
        if m.type == MentionType.USER and m.user_id != user_id:
            await engine.in_app_notification_service.create(
                user_id=m.user_id,
                org_id=org_id,
                type="mention",
                title=f"Вас упомянул {sender_label}",
                content=request.content[:200],
                reference_type="message",
                reference_id=message.id,
            )
```

Если `MentionType` не импортирован в `chats.py` — добавить в импорты сверху:

```python
from ..models.message import Mention, MentionType
```

- [ ] **Step 5.4: Запустить тест — должен пройти**

Run: `cd /root/rugpt && venv/bin/pytest tests/test_chat_route_mention_notification.py -v`
Expected: 3 passed.

---

## Task 6: Engine — `_is_mentioned` helper + endpoint `POST /chats/messages/{id}/reply`

**Files:**
- Modify: `/root/rugpt/src/engine/routes/chats.py` (новый endpoint + helper + Pydantic-модель)
- Test: `/root/rugpt/tests/test_reply_to_mention_endpoint.py` (создать)

- [ ] **Step 6.1: Написать failing test**

Создай `/root/rugpt/tests/test_reply_to_mention_endpoint.py`:

```python
"""Test reply-to-mention endpoint and _is_mentioned helper."""
import pytest
from uuid import uuid4
from unittest.mock import AsyncMock, MagicMock

from fastapi import HTTPException

from src.engine.models.message import Mention, MentionType
from src.engine.routes.chats import _is_mentioned, ReplyToMentionRequest, reply_to_mention


def _msg_with_mentions(*mentions):
    m = MagicMock()
    m.mentions = list(mentions)
    return m


def test_is_mentioned_user_match():
    sender = MagicMock(id=uuid4(), role_id=None)
    msg = _msg_with_mentions(
        Mention(type=MentionType.USER, user_id=sender.id, username="x", position=0)
    )
    assert _is_mentioned(msg, sender) is True


def test_is_mentioned_ai_role_match():
    sender = MagicMock(id=uuid4(), role_id=uuid4())
    msg = _msg_with_mentions(
        Mention(type=MentionType.AI_ROLE, user_id=sender.id, username="x", position=0)
    )
    assert _is_mentioned(msg, sender) is True


def test_is_mentioned_no_match():
    sender = MagicMock(id=uuid4(), role_id=None)
    other = uuid4()
    msg = _msg_with_mentions(
        Mention(type=MentionType.USER, user_id=other, username="y", position=0)
    )
    assert _is_mentioned(msg, sender) is False


def test_is_mentioned_empty_list():
    sender = MagicMock(id=uuid4(), role_id=None)
    msg = MagicMock()
    msg.mentions = None
    assert _is_mentioned(msg, sender) is False


@pytest.mark.asyncio
async def test_reply_endpoint_403_when_not_mentioned():
    sender_id = uuid4()
    other_id = uuid4()
    chat_id = uuid4()
    msg_id = uuid4()

    fake_engine = MagicMock()
    fake_engine.chat_service.get_message = AsyncMock(return_value=_msg_with_mentions(
        Mention(type=MentionType.USER, user_id=other_id, username="x", position=0)
    ))
    fake_engine.user_storage.get_by_id = AsyncMock(
        return_value=MagicMock(id=sender_id, role_id=None)
    )

    with pytest.raises(HTTPException) as exc:
        await reply_to_mention(
            message_id=msg_id,
            request=ReplyToMentionRequest(content="ok"),
            user_id=sender_id,
            org_id=uuid4(),
            engine=fake_engine,
        )
    assert exc.value.status_code == 403


@pytest.mark.asyncio
async def test_reply_endpoint_409_on_double_reply():
    sender_id = uuid4()
    msg_id = uuid4()
    fake_engine = MagicMock()
    original = _msg_with_mentions(
        Mention(type=MentionType.USER, user_id=sender_id, username="x", position=0)
    )
    original.chat_id = uuid4()
    fake_engine.chat_service.get_message = AsyncMock(return_value=original)
    fake_engine.user_storage.get_by_id = AsyncMock(
        return_value=MagicMock(id=sender_id, role_id=None)
    )
    fake_engine.message_storage.find_reply = AsyncMock(return_value=MagicMock(id=uuid4()))

    with pytest.raises(HTTPException) as exc:
        await reply_to_mention(
            message_id=msg_id,
            request=ReplyToMentionRequest(content="ok"),
            user_id=sender_id,
            org_id=uuid4(),
            engine=fake_engine,
        )
    assert exc.value.status_code == 409


@pytest.mark.asyncio
async def test_reply_endpoint_success_calls_send_message():
    sender_id = uuid4()
    msg_id = uuid4()
    chat_id = uuid4()
    fake_engine = MagicMock()
    original = _msg_with_mentions(
        Mention(type=MentionType.USER, user_id=sender_id, username="x", position=0)
    )
    original.chat_id = chat_id
    fake_engine.chat_service.get_message = AsyncMock(return_value=original)
    fake_engine.user_storage.get_by_id = AsyncMock(
        return_value=MagicMock(id=sender_id, role_id=None)
    )
    fake_engine.message_storage.find_reply = AsyncMock(return_value=None)
    new_msg = MagicMock(id=uuid4(), to_dict=lambda: {"id": "..."})
    fake_engine.chat_service.send_message = AsyncMock(return_value=new_msg)

    await reply_to_mention(
        message_id=msg_id,
        request=ReplyToMentionRequest(content="ответ"),
        user_id=sender_id,
        org_id=uuid4(),
        engine=fake_engine,
    )

    fake_engine.chat_service.send_message.assert_awaited_once()
    kwargs = fake_engine.chat_service.send_message.call_args.kwargs
    assert kwargs["chat_id"] == chat_id
    assert kwargs["sender_id"] == sender_id
    assert kwargs["content"] == "ответ"
    assert kwargs["reply_to_id"] == msg_id
```

- [ ] **Step 6.2: Запустить — должны упасть**

Run: `cd /root/rugpt && venv/bin/pytest tests/test_reply_to_mention_endpoint.py -v`
Expected: ImportError или AttributeError — `_is_mentioned`, `ReplyToMentionRequest`, `reply_to_mention` не существуют.

- [ ] **Step 6.3: Реализовать helper + Pydantic-модель**

В `/root/rugpt/src/engine/routes/chats.py` добавить (рядом с другими `class ...Request(BaseModel)`):

```python
class ReplyToMentionRequest(BaseModel):
    content: str


def _is_mentioned(original, sender) -> bool:
    """True if sender appears in original.mentions, regardless of mention type.

    `mention_service.resolve_mentions` пишет реальный user_id владельца
    и для @user, и для @@user (когда @@ резолвится к человеку, а не к
    системнику). Поэтому хватает простого сравнения user_id, без role_id-логики
    и без обращений к user_storage за дополнительными данными.

    Системные AI-роли типа @@pm резолвятся к system user'ам — их user_id
    не совпадёт с sender.id (который всегда обычный человек), так что
    случай отсекается естественным образом.
    """
    return any(m.user_id == sender.id for m in (original.mentions or []))
```

- [ ] **Step 6.4: Реализовать endpoint**

Добавить в тот же файл (рядом с другими `@router.post`):

```python
@router.post("/messages/{message_id}/reply", response_model=MessageResponse)
async def reply_to_mention(
    message_id: UUID,
    request: ReplyToMentionRequest,
    user_id: UUID,
    org_id: UUID,
    engine: EngineService = Depends(get_engine),
):
    """Reply to a mentioning message without joining the chat as participant.

    Гейт прав: sender должен быть упомянут в `original.mentions`.
    Single-use: один реплай на одну (mentioning_message, sender) пару.
    """
    original = await engine.chat_service.get_message(message_id)
    if not original:
        raise HTTPException(status_code=404, detail="Message not found")

    sender = await engine.user_storage.get_by_id(user_id)
    if not sender:
        raise HTTPException(status_code=404, detail="User not found")

    if not _is_mentioned(original, sender):
        raise HTTPException(status_code=403, detail="Not mentioned in this message")

    if await engine.message_storage.find_reply(message_id, sender.id):
        raise HTTPException(status_code=409, detail="Already replied to this mention")

    reply = await engine.chat_service.send_message(
        chat_id=original.chat_id,
        sender_id=sender.id,
        content=request.content,
        reply_to_id=message_id,
    )
    return MessageResponse(**reply.to_dict())
```

- [ ] **Step 6.5: Запустить тест — должен пройти**

Run: `cd /root/rugpt && venv/bin/pytest tests/test_reply_to_mention_endpoint.py -v`
Expected: 7 passed.

- [ ] **Step 6.6: Проверить что весь движок не сломался**

Run: `cd /root/rugpt && venv/bin/pytest tests/ -x --ignore=tests/integration -q 2>&1 | tail -30`
Expected: All passed (или известные пропуски).

---

## Task 7: Webclient backend — `type` фильтр в адаптере `get_in_app_notifications`

**Files:**
- Modify: `/root/webclient_rugpt/packages/backend/src/engine/adapters/rugpt.adapter.ts`

- [ ] **Step 7.1: Найти case**

Run: `grep -n "get_in_app_notifications" /root/webclient_rugpt/packages/backend/src/engine/adapters/rugpt.adapter.ts`

- [ ] **Step 7.2: Расширить case**

Открыть найденный case и добавить чтение `payload.type`. Конкретно — заменить блок (типичный вид):

```typescript
      case 'get_in_app_notifications': {
        const params = new URLSearchParams();
        if (payload.limit) params.append('limit', String(payload.limit));
        if (payload.offset) params.append('offset', String(payload.offset));
        if (payload.unread_only) params.append('unread_only', String(payload.unread_only));
        const qs = params.toString();
        return this.request('GET', `/api/v1/in-app-notifications${qs ? '?' + qs : ''}`, null, headers);
      }
```

на:

```typescript
      case 'get_in_app_notifications': {
        const params = new URLSearchParams();
        if (payload.limit) params.append('limit', String(payload.limit));
        if (payload.offset) params.append('offset', String(payload.offset));
        if (payload.unread_only) params.append('unread_only', String(payload.unread_only));
        if (payload.type) params.append('type', String(payload.type));
        const qs = params.toString();
        return this.request('GET', `/api/v1/in-app-notifications${qs ? '?' + qs : ''}`, null, headers);
      }
```

(Скопировать существующий блок и добавить ровно одну строку про `type`. Если структура слегка иная — адаптировать сохраняя стиль.)

- [ ] **Step 7.3: Verify TypeScript build**

Run: `cd /root/webclient_rugpt/packages/backend && npx tsc --noEmit`
Expected: 0 errors.

---

## Task 8: Webclient backend — `type` query в `InAppNotificationController` и сервисе

**Files:**
- Modify: `/root/webclient_rugpt/packages/backend/src/in-app-notification/in-app-notification.controller.ts`
- Modify: `/root/webclient_rugpt/packages/backend/src/in-app-notification/in-app-notification.service.ts`

- [ ] **Step 8.1: Прочитать контроллер**

Run: `cat /root/webclient_rugpt/packages/backend/src/in-app-notification/in-app-notification.controller.ts`

- [ ] **Step 8.2: Расширить контроллер**

В `findAll` добавить `@Query('type') type: string`. Замена сигнатуры (типично):

```typescript
  @Get()
  async findAll(
    @Query('limit') limit: string,
    @Query('offset') offset: string,
    @Query('unread_only') unreadOnly: string,
    @Query('type') type: string,
    @Request() req: any,
  ) {
    return this.service.findAll(
      {
        limit: limit ? parseInt(limit, 10) : undefined,
        offset: offset ? parseInt(offset, 10) : undefined,
        unread_only: unreadOnly === 'true',
        type: type || undefined,
      },
      req.user,
    );
  }
```

- [ ] **Step 8.3: Прочитать сервис и обновить тип query + передачу**

Run: `cat /root/webclient_rugpt/packages/backend/src/in-app-notification/in-app-notification.service.ts`

В методе `findAll(query, currentUser)` тип `query` расширить полем `type?: string`, и в `engineAdapter.execute(...)` payload пробросить `type`. Типично:

```typescript
  async findAll(
    query: { limit?: number; offset?: number; unread_only?: boolean; type?: string },
    currentUser: CurrentUser,
  ) {
    const [success, data] = await this.engineAdapter.execute('get_in_app_notifications', {
      ...query,
      user_id: currentUser.id,
      token: currentUser.engineToken,
    });
    if (!success) return [];
    return data || [];
  }
```

- [ ] **Step 8.4: Verify build**

Run: `cd /root/webclient_rugpt/packages/backend && npx tsc --noEmit`
Expected: 0 errors.

---

## Task 9: Webclient backend — `reply_to_mention` case в адаптере

**Files:**
- Modify: `/root/webclient_rugpt/packages/backend/src/engine/adapters/rugpt.adapter.ts`

- [ ] **Step 9.1: Добавить case**

В `/root/webclient_rugpt/packages/backend/src/engine/adapters/rugpt.adapter.ts`, в switch-блоке `execute(action, payload)` рядом с другими `case 'get_files':` добавить:

```typescript
      case 'reply_to_mention':
        return this.request(
          'POST',
          `/api/v1/chats/messages/${encodeURIComponent(payload.message_id)}/reply`,
          { content: payload.content },
          headers,
        );
```

- [ ] **Step 9.2: Verify build**

Run: `cd /root/webclient_rugpt/packages/backend && npx tsc --noEmit`
Expected: 0 errors.

---

## Task 10: Webclient backend — endpoint `POST /chats/messages/:id/reply`

**Files:**
- Modify: `/root/webclient_rugpt/packages/backend/src/chat/chat.controller.ts`
- Modify: `/root/webclient_rugpt/packages/backend/src/chat/chat.service.ts`

- [ ] **Step 10.1: Прочитать контроллер чата**

Run: `grep -n "@Post\|@Get\|@Controller" /root/webclient_rugpt/packages/backend/src/chat/chat.controller.ts | head -10`

- [ ] **Step 10.2: Добавить endpoint в контроллер**

В `chat.controller.ts`, рядом с другими `@Post`, добавить:

```typescript
  @Post('messages/:id/reply')
  async replyToMention(
    @Param('id') messageId: string,
    @Body() body: { content: string },
    @Request() req: any,
  ) {
    return this.chatService.replyToMention(messageId, body.content, req.user);
  }
```

Если `Param` или `Body` не импортированы — добавить в импорты сверху файла из `@nestjs/common`.

- [ ] **Step 10.3: Добавить метод в сервис**

В `chat.service.ts` добавить метод:

```typescript
  async replyToMention(messageId: string, content: string, currentUser: CurrentUser) {
    const [success, data] = await this.engineAdapter.execute('reply_to_mention', {
      message_id: messageId,
      content,
      token: currentUser.engineToken,
    });
    if (!success) {
      throw new Error(typeof data === 'string' ? data : 'Failed to reply to mention');
    }
    return data;
  }
```

- [ ] **Step 10.4: Verify build**

Run: `cd /root/webclient_rugpt/packages/backend && npx tsc --noEmit`
Expected: 0 errors.

---

## Task 11: Frontend — хук `useMyMentions`

**Files:**
- Create: `/root/webclient_rugpt/packages/frontend/src/app/hooks/useMyMentions.ts`

- [ ] **Step 11.1: Создать файл**

```typescript
'use client';

import { useState, useEffect, useCallback, useRef } from 'react';
import { useAuthStore } from './useAuth';
import { getApiClient } from '../../transport/apiClient';

const POLL_INTERVAL = 30000;

export interface MentionNotification {
  id: string;
  user_id: string;
  org_id: string;
  type: string;          // всегда 'mention'
  title: string;         // "Вас упомянул @bob"
  content: string | null;
  reference_type: string | null;   // 'message'
  reference_id: string | null;     // message UUID
  is_read: boolean;
  created_at: string;
}

export function useMyMentions() {
  const [mentions, setMentions] = useState<MentionNotification[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const token = useAuthStore((s) => s.token);
  const user = useAuthStore((s) => s.user);
  const intervalRef = useRef<ReturnType<typeof setInterval> | null>(null);

  const fetchMentions = useCallback(async () => {
    if (!token) {
      setLoading(false);
      return;
    }
    try {
      setError(null);
      const api = getApiClient();
      const data = await api.signedGet<MentionNotification[]>(
        '/api/in-app-notifications?type=mention&limit=50',
        user?.id,
      );
      setMentions(data || []);
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to fetch mentions');
    } finally {
      setLoading(false);
    }
  }, [token, user?.id]);

  const replyToMention = useCallback(
    async (messageId: string, content: string) => {
      if (!token) throw new Error('Not authenticated');
      const api = getApiClient();
      return api.signedPost(`/api/messages/${messageId}/reply`, { content }, user?.id);
    },
    [token, user?.id],
  );

  useEffect(() => {
    fetchMentions();
  }, [fetchMentions]);

  useEffect(() => {
    if (!token) return;
    intervalRef.current = setInterval(fetchMentions, POLL_INTERVAL);
    return () => {
      if (intervalRef.current) clearInterval(intervalRef.current);
    };
  }, [token, fetchMentions]);

  const unreadCount = mentions.filter((m) => !m.is_read).length;

  return { mentions, loading, error, unreadCount, refetch: fetchMentions, replyToMention };
}
```

- [ ] **Step 11.2: Verify build**

Run: `cd /root/webclient_rugpt/packages/frontend && npx tsc --noEmit 2>&1 | head -10`
Expected: 0 errors касательно нового файла.

---

## Task 12: Frontend — хук `usePendingValidationCount`

**Files:**
- Create: `/root/webclient_rugpt/packages/frontend/src/app/hooks/usePendingValidationCount.ts`

- [ ] **Step 12.1: Создать файл**

```typescript
'use client';

import { useState, useEffect } from 'react';
import { useApiClient } from '../../transport';
import { ChatMessage } from '@webchat/common';

interface WsResponse {
  status: string;
  messages?: ChatMessage[];
}

/**
 * Lightweight count-only variant of useMentionReview. Тянет тот же WS-эндпоинт
 * messages:pending-review, но возвращает только число — для бейджа колокольчика.
 * Refresh при validate/reject событиях.
 */
export function usePendingValidationCount() {
  const api = useApiClient();
  const [count, setCount] = useState(0);

  useEffect(() => {
    if (!api.isConnected) return;
    const fetch = () => {
      api.emit('messages:pending-review' as any, {}, (response: unknown) => {
        const res = response as WsResponse;
        setCount(res?.status === 'ok' && res.messages ? res.messages.length : 0);
      });
    };
    fetch();
    const onChange = () => fetch();
    const unsub1 = api.on('message:validated' as any, onChange);
    const unsub2 = api.on('message:rejected' as any, onChange);
    return () => {
      unsub1();
      unsub2();
    };
  }, [api]);

  return { count };
}
```

- [ ] **Step 12.2: Verify build**

Run: `cd /root/webclient_rugpt/packages/frontend && npx tsc --noEmit 2>&1 | head -10`
Expected: 0 errors.

---

## Task 13: Frontend — расширить `useNotifications` суммой колокольчика

**Files:**
- Modify: `/root/webclient_rugpt/packages/frontend/src/app/hooks/useNotifications.ts`

- [ ] **Step 13.1: Прочитать текущий return**

Run: `tail -25 /root/webclient_rugpt/packages/frontend/src/app/hooks/useNotifications.ts`

- [ ] **Step 13.2: Импортировать новый хук и сложить**

В `/root/webclient_rugpt/packages/frontend/src/app/hooks/useNotifications.ts`:

1. Добавить импорт сверху:
```typescript
import { usePendingValidationCount } from './usePendingValidationCount';
```

2. Внутри `useNotifications()` перед `return`:
```typescript
  const { count: pendingValidationCount } = usePendingValidationCount();
  const totalUnread = unreadCount + pendingValidationCount;
```

3. В объекте `return` заменить поле `unreadCount: unreadCount` на `unreadCount: totalUnread` (либо добавить отдельное `bellCount: totalUnread` если хочется не ломать прежнюю семантику; рекомендую оставить под именем `unreadCount` для backward-compat в UI).

- [ ] **Step 13.3: Verify build**

Run: `cd /root/webclient_rugpt/packages/frontend && npx tsc --noEmit 2>&1 | head -10`
Expected: 0 errors.

---

## Task 14: Frontend — компонент `MyMentionsList`

**Files:**
- Create: `/root/webclient_rugpt/packages/frontend/src/app/components/MyMentionsList.tsx`

- [ ] **Step 14.1: Создать файл**

```tsx
'use client';

import { useState, useEffect } from 'react';
import { useMyMentions, MentionNotification } from '../hooks/useMyMentions';
import { getApiClient } from '../../transport/apiClient';
import { User } from '@webchat/common';

interface MyMentionsListProps {
  currentUserId: string;
  users: User[];
}

interface MessagePreview {
  id: string;
  content: string;
  sender_id: string;
  chat_id: string;
}

function formatTime(iso: string): string {
  const d = new Date(iso);
  const diffMin = Math.floor((Date.now() - d.getTime()) / 60000);
  if (diffMin < 1) return 'только что';
  if (diffMin < 60) return `${diffMin} мин. назад`;
  const diffHours = Math.floor(diffMin / 60);
  if (diffHours < 24) return `${diffHours} ч. назад`;
  return d.toLocaleDateString('ru-RU', { day: 'numeric', month: 'short', hour: '2-digit', minute: '2-digit' });
}

export function MyMentionsList({ currentUserId, users }: MyMentionsListProps) {
  const { mentions, loading, replyToMention, refetch } = useMyMentions();
  const userMap = new Map(users.map((u) => [u.id, u.name]));

  if (loading) return <div className="text-center py-8 text-neutral-dark-medium dark:text-gray-400">Загрузка…</div>;
  if (mentions.length === 0) return <div className="text-center py-8 text-neutral-dark-medium dark:text-gray-400">Упоминаний пока нет</div>;

  return (
    <div className="space-y-3">
      {mentions.map((m) => (
        <MentionCard
          key={m.id}
          notification={m}
          userMap={userMap}
          replyToMention={replyToMention}
          onReplied={refetch}
        />
      ))}
    </div>
  );
}

function MentionCard({
  notification,
  userMap,
  replyToMention,
  onReplied,
}: {
  notification: MentionNotification;
  userMap: Map<string, string>;
  replyToMention: (messageId: string, content: string) => Promise<unknown>;
  onReplied: () => void;
}) {
  const [original, setOriginal] = useState<MessagePreview | null>(null);
  const [loadingMsg, setLoadingMsg] = useState(true);
  const [text, setText] = useState('');
  const [sending, setSending] = useState(false);
  const [doneText, setDoneText] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!notification.reference_id) {
      setLoadingMsg(false);
      return;
    }
    let cancelled = false;
    (async () => {
      try {
        const api = getApiClient();
        const msg = await api.signedGet<MessagePreview>(
          `/api/messages/${notification.reference_id}`,
          notification.user_id,
        );
        if (!cancelled) setOriginal(msg);
      } catch {
        if (!cancelled) setError('Не удалось загрузить сообщение');
      } finally {
        if (!cancelled) setLoadingMsg(false);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [notification.reference_id, notification.user_id]);

  const handleSubmit = async () => {
    if (!text.trim() || !notification.reference_id) return;
    setSending(true);
    setError(null);
    try {
      await replyToMention(notification.reference_id, text.trim());
      setDoneText(text.trim());
      onReplied();
    } catch (e: any) {
      const status = e?.status;
      if (status === 409) setError('Вы уже отвечали на это упоминание');
      else if (status === 403) setError('Нет доступа');
      else setError('Не удалось отправить ответ');
    } finally {
      setSending(false);
    }
  };

  const senderName = original ? (userMap.get(original.sender_id) || 'Кто-то') : '…';

  return (
    <div id={`mention-${notification.id}`} className="bg-neutral-light-medium dark:bg-gray-700 rounded-card p-4 space-y-2">
      <div className="flex items-center justify-between">
        <span className="text-body-sm font-medium text-neutral-dark-darkest dark:text-white">{senderName}</span>
        <span className="text-[12px] text-neutral-dark-medium dark:text-gray-400">{formatTime(notification.created_at)}</span>
      </div>
      <div className="text-body-sm text-neutral-dark-darkest dark:text-gray-200 whitespace-pre-wrap">
        {loadingMsg ? '…' : original?.content || notification.content || '(сообщение недоступно)'}
      </div>
      {doneText ? (
        <div className="text-body-sm text-green-700 dark:text-green-400">✓ Отвечено: {doneText}</div>
      ) : (
        <div className="space-y-2">
          <textarea
            value={text}
            onChange={(e) => setText(e.target.value)}
            placeholder="Ваш ответ…"
            rows={2}
            disabled={sending}
            className="w-full px-3 py-2 rounded-button border border-neutral-light-darkest dark:border-gray-600 bg-white dark:bg-gray-800 text-body-sm text-neutral-dark-darkest dark:text-white focus:outline-none focus:ring-2 focus:ring-primary disabled:opacity-50"
          />
          <div className="flex items-center gap-3">
            <button
              type="button"
              onClick={handleSubmit}
              disabled={sending || !text.trim()}
              className="px-3 py-1.5 rounded-button text-body-sm bg-primary text-white hover:bg-primary-dark transition-colors disabled:opacity-50"
            >
              {sending ? 'Отправка…' : 'Ответить'}
            </button>
            {error && <span className="text-body-sm text-rose-600 dark:text-rose-400">{error}</span>}
          </div>
        </div>
      )}
    </div>
  );
}
```

- [ ] **Step 14.2: Verify build**

Run: `cd /root/webclient_rugpt/packages/frontend && npx tsc --noEmit 2>&1 | head -10`
Expected: 0 errors.

---

## Task 15: Frontend — рефактор `mentions/page.tsx` с двумя табами

**Files:**
- Modify: `/root/webclient_rugpt/packages/frontend/src/app/mentions/page.tsx`

- [ ] **Step 15.1: Полностью заменить содержимое файла**

Записать `/root/webclient_rugpt/packages/frontend/src/app/mentions/page.tsx`:

```tsx
'use client';

import { Suspense, useEffect, useState } from 'react';
import { useAuth } from '../hooks/useAuth';
import { useHasHydrated } from '../hooks/useHasHydrated';
import { useUsers } from '../hooks/useUsers';
import { useDepartments } from '../hooks/useDepartments';
import { useRouter, useSearchParams } from 'next/navigation';
import { Sidebar } from '../components/Sidebar';
import { ChatNavigation } from '../components/ChatNavigation';
import { MentionReviewList } from '../components/MentionReviewList';
import { MyMentionsList } from '../components/MyMentionsList';
import { useMyMentions } from '../hooks/useMyMentions';
import { useMentionReview } from '../hooks/useMentionReview';

export default function MentionsPage() {
  return (
    <Suspense fallback={null}>
      <MentionsPageInner />
    </Suspense>
  );
}

function MentionsPageInner() {
  const { user, isAuthenticated } = useAuth();
  const hasHydrated = useHasHydrated();
  const { users } = useUsers();
  const { departments } = useDepartments();
  const deptMap = new Map(departments.map((d) => [d.id, d.name]));
  const router = useRouter();
  const searchParams = useSearchParams();
  const initialTab = searchParams.get('tab') === 'role' ? 'role' : 'me';
  const highlight = searchParams.get('highlight');
  const [tab, setTab] = useState<'me' | 'role'>(initialTab);

  const { unreadCount: myUnread } = useMyMentions();
  const { count: validationsCount } = useMentionReview();

  useEffect(() => {
    if (hasHydrated && !isAuthenticated) router.push('/login');
  }, [hasHydrated, isAuthenticated, router]);

  useEffect(() => {
    if (!highlight) return;
    let cancelled = false;
    let timer: ReturnType<typeof setTimeout> | null = null;
    const tryHighlight = (attempt: number) => {
      if (cancelled) return;
      const el = document.getElementById(`mention-${highlight}`);
      if (!el) {
        if (attempt < 20) timer = setTimeout(() => tryHighlight(attempt + 1), 150);
        return;
      }
      el.scrollIntoView({ behavior: 'smooth', block: 'center' });
      el.classList.add('ring-2', 'ring-primary', 'shadow-lg');
      timer = setTimeout(() => el.classList.remove('ring-2', 'ring-primary', 'shadow-lg'), 2500);
    };
    tryHighlight(0);
    return () => {
      cancelled = true;
      if (timer) clearTimeout(timer);
    };
  }, [highlight, tab]);

  if (!hasHydrated || !isAuthenticated || !user) return null;

  return (
    <div className="h-dvh bg-neutral-light-lightest dark:bg-gray-900 transition-colors pl-[70px]">
      <Sidebar
        chats={users.filter((u) => u.id !== user.id).map((u) => ({
          id: u.id,
          username: u.username,
          name: u.name,
          avatar: u.avatarUrl,
          roleName: u.roleName,
          departmentId: u.departmentId,
          departmentName: u.departmentId ? deptMap.get(u.departmentId) || null : null,
          isHead: u.isHead,
          isSystem: u.isSystem,
        }))}
      />

      <div className="h-full flex flex-col">
        <ChatNavigation title="Упоминания" />

        <div className="px-4 pt-4 max-w-3xl mx-auto w-full">
          <div className="flex border-b border-neutral-light-dark dark:border-gray-700">
            <button
              type="button"
              onClick={() => setTab('me')}
              className={`px-4 py-2 text-body-sm font-medium border-b-2 transition-colors ${
                tab === 'me'
                  ? 'border-primary text-primary dark:text-primary-light'
                  : 'border-transparent text-neutral-dark-medium dark:text-gray-400 hover:text-neutral-dark-darkest dark:hover:text-gray-200'
              }`}
            >
              Меня
              {myUnread > 0 && (
                <span className="ml-2 inline-flex items-center px-1.5 py-0.5 rounded-full text-[11px] bg-primary text-white">
                  {myUnread}
                </span>
              )}
            </button>
            <button
              type="button"
              onClick={() => setTab('role')}
              className={`px-4 py-2 text-body-sm font-medium border-b-2 transition-colors ${
                tab === 'role'
                  ? 'border-primary text-primary dark:text-primary-light'
                  : 'border-transparent text-neutral-dark-medium dark:text-gray-400 hover:text-neutral-dark-darkest dark:hover:text-gray-200'
              }`}
            >
              Моя роль
              {validationsCount > 0 && (
                <span className="ml-2 inline-flex items-center px-1.5 py-0.5 rounded-full text-[11px] bg-primary text-white">
                  {validationsCount}
                </span>
              )}
            </button>
          </div>
        </div>

        <div className="flex-1 min-h-0 overflow-y-auto">
          <div className="max-w-3xl mx-auto px-4 py-6">
            {tab === 'me' ? (
              <MyMentionsList currentUserId={user.id} users={users} />
            ) : (
              <MentionReviewList currentUserId={user.id} users={users} />
            )}
          </div>
        </div>
      </div>
    </div>
  );
}
```

- [ ] **Step 15.2: Verify build**

Run: `cd /root/webclient_rugpt/packages/frontend && npx tsc --noEmit 2>&1 | head -10`
Expected: 0 errors.

---

## Task 16: Frontend — `NotificationDropdown` href для `mention`

**Files:**
- Modify: `/root/webclient_rugpt/packages/frontend/src/app/components/NotificationDropdown.tsx`

- [ ] **Step 16.1: Найти строку**

Run: `grep -n "type === 'mention'" /root/webclient_rugpt/packages/frontend/src/app/components/NotificationDropdown.tsx`

Должна найтись строка вида `if (n.type === 'mention') return '/mentions';`.

- [ ] **Step 16.2: Заменить на параметризованный URL**

Заменить:
```typescript
if (n.type === 'mention') return '/mentions';
```
на:
```typescript
if (n.type === 'mention') return `/mentions?tab=me&highlight=${n.id}`;
```

- [ ] **Step 16.3: Verify build**

Run: `cd /root/webclient_rugpt/packages/frontend && npx tsc --noEmit 2>&1 | head -10`
Expected: 0 errors.

---

## Task 17: End-to-end ручная проверка по критериям приёмки

**Files:** деплой, не код.

- [ ] **Step 17.1: Деплой engine**

С Mac: `./deploy.sh` (engine рестартанётся, новые миграции отсутствуют — ничего нового в БД).

- [ ] **Step 17.2: Деплой webclient (frontend + backend)**

С webclient-машины: rsync `packages/` + rebuild контейнеров `webclient_rugpt-backend-1` и `webclient_rugpt-frontend-1`.

- [ ] **Step 17.3: Сценарий 1 — `@user`-нотификация**

В браузере: пользователь B открывает чат C (где Anna **не** участник), пишет «`@anna` привет», отправляет.
Ожидание: Anna в течение 30 сек видит +1 на колокольчике.

- [ ] **Step 17.4: Сценарий 2 — Просмотр в табе «Меня»**

Anna кликает колокольчик → строка «Вас упомянул @bob» → переход на `/mentions?tab=me&highlight=...`.
Ожидание: открыт таб «Меня», в списке карточка с текстом «привет», именем «Bob», временем; карточка подсвечена rings.

- [ ] **Step 17.5: Сценарий 3 — Reply**

Anna пишет «ок, посмотрю» в textarea и жмёт «Ответить».
Ожидание: карточка превращается в «✓ Отвечено: ок, посмотрю». В исходном чате C появляется новое сообщение от Anna с reply-связью на «привет».

- [ ] **Step 17.6: Сценарий 4 — Single-use**

Anna обновляет страницу `/mentions` — карточка снова в исходном виде (server-state). Anna пишет второй ответ и жмёт «Ответить».
Ожидание: красный текст «Вы уже отвечали на это упоминание», кнопка disabled.

- [ ] **Step 17.7: Сценарий 5 — 403 без mention**

Через DevTools/curl напрямую: `POST /api/messages/{любой_msg_id_без_меня_в_mentions}/reply`.
Ожидание: 403 Forbidden.

- [ ] **Step 17.8: Сценарий 6 — `@@anna` валидация**

B пишет «`@@anna` посмотри». AI генерирует ответ.
Ожидание: Anna видит +1 на колокольчике (от pending-validation), таб «Моя роль» содержит карточку валидации с возможностью apprive/reject (текущее поведение `MentionReviewList` без изменений).

- [ ] **Step 17.9: Сценарий 7 — Сумма колокольчика**

Anna имеет 2 непрочитанных `@user`-нотификации + 3 pending-validations.
Ожидание: цифра на колокольчике = 5.

---

## Self-Review (выполнено автором)

**Spec coverage:**
- ✅ Поток A (in-app на @user): Task 5
- ✅ Поток B (validation queue без изменений): не трогаем
- ✅ Поток C (reply без участников): Task 6
- ✅ Безопасность carve-out (sender в mentions, single-use): Task 1, Task 6
- ✅ Engine: storage + service + route — Task 1-6
- ✅ Webclient backend: адаптер + контроллеры — Task 7-10
- ✅ Frontend: хуки + компоненты + страница — Task 11-16
- ✅ Acceptance criteria: Task 17 (все 7 сценариев)

**Placeholder scan:** проверены все шаги — no TBD/TODO/«fill in», все блоки кода полные.

**Type consistency:**
- `find_reply(reply_to_id, sender_id)` — одинаково в Task 1, 6
- `_is_mentioned(original, sender)` синхронный — одинаково в Task 6 helper и call site
- `type` опциональный аргумент — одинаково в Task 2 (storage), 3 (service), 4 (route), 7-8 (webclient)
- `replyToMention(messageId, content, currentUser)` — одинаково в Task 10, 11
- `MentionNotification` interface — одинаково в Task 11, 14

Готово к исполнению.
