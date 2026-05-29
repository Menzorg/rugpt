# Bell как единый центр нотификаций — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Удалить `TaskNotificationService` (PM-чат-уведомления о transitions). Bell — единственный канал нотификаций для всех task transitions, deadline-events и participant add/remove. PM-юзер сохраняется как AI-собеседник для руководителей.

**Architecture:** В `task_service.py` уже есть `_bell_to_recipients(task, actor_user_id, title, content)` (вызывается параллельно с `_notify(...)`). Inline'им `_resolve_recipients` чтобы убрать зависимость от `TaskNotificationService`. Добавляем `_bell_to_user(...)` для single-recipient (add/remove participant). Удаляем все `_notify(...)` вызовы и сам helper. Удаляем файл `task_notification_service.py`. Обновляем тесты. PM-юзер в БД и старые сообщения остаются.

**Tech Stack:** Python 3.10+, FastAPI, asyncpg, pytest, pytest-asyncio.

**Spec:** `/root/rugpt/docs/superpowers/specs/2026-05-03-bell-as-single-notification-center-design.md`

**No commits:** пользователь коммитит сам. В шагах нет `git commit`.

---

## File Structure

| File | Action | Responsibility |
|---|---|---|
| `src/engine/services/task_service.py` | Modify | Inline `_resolve_recipients`, добавить `_bell_to_user`, обновить titles/content в bell-вызовах, удалить `_notify`, удалить параметр `task_notification_service`. |
| `src/engine/services/engine_service.py` | Modify | Удалить импорт + конструирование `TaskNotificationService` + параметр в `TaskService(...)`. |
| `src/engine/services/task_notification_service.py` | Delete | Файл удаляется. |
| `src/engine/routes/chats.py` | Modify | Обновить комментарий в строке 540 (упоминание `task_notification_service` устаревает). |
| `tests/test_task_notifications.py` | Delete | Тесты удаляемого сервиса. |
| `tests/test_resolve_recipients.py` | Modify | Перевести с `engine.task_notification_service._resolve_recipients` на `engine.task_service._resolve_recipients`. |
| `tests/integration/test_kafka_end_to_end.py` | Modify | Удалить `test_pm_notification_published_to_chat_events` (тестирует удалённое поведение); обновить docstring. |

---

## Глоссарий имён актёров

`_format_actor(user)` возвращает строку для подстановки в content:
- `f"@{user.username}"` если `user.username` не пустое.
- иначе `user.name`.

Логика повторяет `_actor_name` из удаляемого `task_notification_service`.

Дата формата `dd.mm.yyyy hh:mm` через `dt.strftime("%d.%m.%Y %H:%M")`.

---

### Task 1: Добавить `_format_actor` helper и `_resolve_recipients` в `TaskService`

**Files:**
- Modify: `src/engine/services/task_service.py`
- Test: `tests/test_resolve_recipients.py`

- [ ] **Step 1: Прочесть текущий `_bell_to_recipients` чтобы знать точку вставки**

Run: `grep -n "_bell_to_recipients\|_notify\|_format_actor\|_resolve_recipients" /root/rugpt/src/engine/services/task_service.py`

Ожидание: видно текущий `_notify` (строка ~73) и `_bell_to_recipients` (строка ~83). `_format_actor` и `_resolve_recipients` отсутствуют.

- [ ] **Step 2: Вставить `_format_actor` и `_resolve_recipients` в `TaskService` сразу после `_bell_to_recipients`**

В `src/engine/services/task_service.py` найти конец метода `_bell_to_recipients` (заканчивается на `logger.error(f"Bell notify create failed for user {u.id} task {task.id}: {e}")`) и добавить ПОСЛЕ него (перед строкой `# --- Internal helpers ---`):

```python
    @staticmethod
    def _format_actor(user: "User") -> str:
        """Display name for actor: '@username' if username present, else plain name."""
        return f"@{user.username}" if getattr(user, "username", None) else user.name

    async def _resolve_recipients(
        self, task: "Task", exclude_user_id: Optional[UUID] = None,
    ) -> List["User"]:
        """Active users involved in the task: creator + assignee + participants.
        Deduplicates and excludes `exclude_user_id`. Filters out users with is_active=false.
        Returns [] if user_storage is unavailable (test/no-db mode).
        """
        if self.user_storage is None:
            return []
        candidate_ids: List[UUID] = []
        if task.created_by_user_id:
            candidate_ids.append(task.created_by_user_id)
        if task.assignee_user_id:
            candidate_ids.append(task.assignee_user_id)
        if self.task_participant_storage is not None:
            participant_ids = await self.task_participant_storage.list_user_ids(task.id)
            candidate_ids.extend(participant_ids)

        seen: set = set()
        unique_ids: List[UUID] = []
        for uid in candidate_ids:
            if uid is None or uid == exclude_user_id or uid in seen:
                continue
            seen.add(uid)
            unique_ids.append(uid)

        result: List["User"] = []
        for uid in unique_ids:
            user = await self.user_storage.get_by_id(uid)
            if user is None:
                continue
            if not getattr(user, "is_active", False):
                continue
            result.append(user)
        return result
```

- [ ] **Step 3: Переключить `_bell_to_recipients` на собственный `_resolve_recipients`**

В `src/engine/services/task_service.py` изменить тело `_bell_to_recipients`:

Найти:
```python
        if self.task_notification_service is None or self.notification_service is None:
            return
        try:
            recipients = await self.task_notification_service._resolve_recipients(
                task, exclude_user_id=actor_user_id,
            )
```

Заменить на:
```python
        if self.notification_service is None:
            return
        try:
            recipients = await self._resolve_recipients(
                task, exclude_user_id=actor_user_id,
            )
```

- [ ] **Step 4: Адаптировать `tests/test_resolve_recipients.py` к новому источнику**

Заменить три обращения `tns = env["engine"].task_notification_service` на `tns = env["engine"].task_service`.

Прочитать файл:
```bash
cat /root/rugpt/tests/test_resolve_recipients.py
```

Найти три строки вида:
```python
    tns = env["engine"].task_notification_service
```
и заменить КАЖДУЮ на:
```python
    tns = env["engine"].task_service
```

Обращения `tns._resolve_recipients(...)` дальше в тестах остаются — метод теперь висит на `task_service` с тем же именем и сигнатурой.

- [ ] **Step 5: Запустить тест и убедиться что проходит**

Run: `cd /root/rugpt && venv/bin/pytest tests/test_resolve_recipients.py -v`

Expected: 3 теста проходят (если БД доступна; иначе — пропускаются по `pytest_asyncio` fixture'у — это OK).

---

### Task 2: Привести bell-вызовы в transitions к спеке (titles + content)

**Files:**
- Modify: `src/engine/services/task_service.py`

Currently 9 transitions имеют generic `_bell_to_recipients(updated, user.id, "...")`. Нужно довести title/content до спеки.

- [ ] **Step 1: `take_task` — content = имя assignee**

В `src/engine/services/task_service.py` найти:
```python
        await self._notify("notify_take", updated, user)
        await self._bell_to_recipients(
            updated, user.id, f"Задача «{updated.title}» взята в работу",
        )
        return updated
```

Заменить на:
```python
        await self._notify("notify_take", updated, user)
        await self._bell_to_recipients(
            updated, user.id,
            title=f"Задача «{updated.title}» взята в работу",
            content=self._format_actor(user),
        )
        return updated
```

- [ ] **Step 2: `mark_done` — content = имя assignee**

Найти и заменить:
```python
        await self._notify("notify_mark_done", updated, user)
        await self._bell_to_recipients(
            updated, user.id, f"Задача «{updated.title}» отмечена как выполненная",
        )
```

на:
```python
        await self._notify("notify_mark_done", updated, user)
        await self._bell_to_recipients(
            updated, user.id,
            title=f"Задача «{updated.title}» отмечена выполненной",
            content=self._format_actor(user),
        )
```

(Title скорректирован под спеку: «отмечена выполненной» без «как».)

- [ ] **Step 3: `accept_task` — content пуст**

Найти:
```python
        await self._notify("notify_accept", updated, user)
        await self._bell_to_recipients(
            updated, user.id, f"Задача «{updated.title}» принята",
        )
```

Заменить на:
```python
        await self._notify("notify_accept", updated, user)
        await self._bell_to_recipients(
            updated, user.id,
            title=f"Задача «{updated.title}» принята",
        )
```

(Без `content=` — оставить дефолт `None`.)

- [ ] **Step 4: `reject_task` — content = комментарий руководителя**

Найти:
```python
        await self._notify("notify_reject", updated, user, comment)
        await self._bell_to_recipients(
            updated, user.id,
            f"Задача «{updated.title}» возвращена в работу",
            content=comment,
        )
```

(Уже корректно — `comment` уходит в content, может быть None.) Изменения нет.

- [ ] **Step 5: `set_deadline` — content = «Новый срок: dd.mm.yyyy hh:mm»**

Найти:
```python
        await self._notify("notify_set_deadline", updated, user)
        await self._bell_to_recipients(
            updated, user.id, f"Срок задачи «{updated.title}» изменён",
        )
```

Заменить на:
```python
        await self._notify("notify_set_deadline", updated, user)
        deadline_str = updated.deadline.strftime("%d.%m.%Y %H:%M") if updated.deadline else "—"
        await self._bell_to_recipients(
            updated, user.id,
            title=f"Срок задачи «{updated.title}» изменён",
            content=f"Новый срок: {deadline_str}",
        )
```

- [ ] **Step 6: `propose_deadline` — content = «{actor}: dd.mm.yyyy hh:mm»**

Найти:
```python
        await self._notify("notify_propose_deadline", updated, user)
        await self._bell_to_recipients(
            updated, user.id, f"Предложен новый срок для «{updated.title}»",
        )
```

Заменить на:
```python
        await self._notify("notify_propose_deadline", updated, user)
        proposed_str = (
            updated.proposed_deadline.strftime("%d.%m.%Y %H:%M")
            if updated.proposed_deadline else "—"
        )
        await self._bell_to_recipients(
            updated, user.id,
            title=f"Предложен новый срок для «{updated.title}»",
            content=f"{self._format_actor(user)}: {proposed_str}",
        )
```

- [ ] **Step 7: `accept_proposed_deadline` — content = новая дата**

Найти:
```python
        await self._notify("notify_accept_proposed_deadline", updated, user)
        await self._bell_to_recipients(
            updated, user.id, f"Новый срок для «{updated.title}» принят",
        )
```

Заменить на:
```python
        await self._notify("notify_accept_proposed_deadline", updated, user)
        deadline_str = updated.deadline.strftime("%d.%m.%Y %H:%M") if updated.deadline else "—"
        await self._bell_to_recipients(
            updated, user.id,
            title=f"Новый срок для «{updated.title}» принят",
            content=deadline_str,
        )
```

- [ ] **Step 8: `reject_proposed_deadline` — content пуст**

Найти:
```python
        await self._notify("notify_reject_proposed_deadline", updated, user)
        await self._bell_to_recipients(
            updated, user.id, f"Новый срок для «{updated.title}» отклонён",
        )
```

Заменить на:
```python
        await self._notify("notify_reject_proposed_deadline", updated, user)
        await self._bell_to_recipients(
            updated, user.id,
            title=f"Новый срок для «{updated.title}» отклонён",
        )
```

- [ ] **Step 9: `check_overdue` — title уже корректен, content пуст**

В `check_overdue` найти:
```python
                await self._notify("notify_overdue", task)

                # Bell всем involved (creator + assignee + participants).
                # actor_user_id=None — overdue scheduler-driven, исключать некого.
                await self._bell_to_recipients(
                    task, actor_user_id=None,
                    title=f"Задача просрочена: {task.title}",
                )
```

(Уже корректно — content=None по дефолту.) Изменений нет.

- [ ] **Step 10: Sanity-парсер**

Run: `/root/rugpt/venv/bin/python -c "import ast; ast.parse(open('/root/rugpt/src/engine/services/task_service.py').read()); print('parse ok')"`

Expected: `parse ok`

---

### Task 3: Добавить `_bell_to_user` helper в `TaskService`

**Files:**
- Modify: `src/engine/services/task_service.py`

- [ ] **Step 1: Вставить метод `_bell_to_user` сразу после `_bell_to_recipients`**

В `src/engine/services/task_service.py` найти конец `_bell_to_recipients` (строка с `logger.error(f"Bell notify create failed for user {u.id} task {task.id}: {e}")`) и добавить ПОСЛЕ него (перед `_format_actor`):

```python
    async def _bell_to_user(
        self,
        user_id: UUID,
        org_id: UUID,
        task_id: UUID,
        title: str,
        content: Optional[str] = None,
    ) -> None:
        """Best-effort single-recipient bell. Used for participant add/remove
        where notification has one specific addressee, not 'all involved'."""
        if self.notification_service is None:
            return
        try:
            await self.notification_service.create(
                user_id=user_id,
                org_id=org_id,
                type="task_status_change",
                title=title,
                content=content,
                reference_type="task",
                reference_id=task_id,
            )
        except Exception as e:
            logger.error(f"Bell single notify failed for user {user_id} task {task_id}: {e}")
```

- [ ] **Step 2: Проверить парсер**

Run: `/root/rugpt/venv/bin/python -c "import ast; ast.parse(open('/root/rugpt/src/engine/services/task_service.py').read()); print('parse ok')"`

Expected: `parse ok`

---

### Task 4: Заменить 3 `_notify("notify_added/removed_as_participant", ...)` на `_bell_to_user`

**Files:**
- Modify: `src/engine/services/task_service.py`

- [ ] **Step 1: Replace в `create()` (создание задачи + добавление участника)**

В `src/engine/services/task_service.py` найти:
```python
        # 5. Notify each new participant.
        for pid in filtered_participants:
            await self._notify(
                "notify_added_as_participant", created, pid,
                by_user_id=created_by_user_id,
            )
```

Заменить на:
```python
        # 5. Notify each new participant via bell (skip self-add).
        actor_user = None
        if self.user_storage is not None and created_by_user_id is not None:
            actor_user = await self.user_storage.get_by_id(created_by_user_id)
        actor_label = self._format_actor(actor_user) if actor_user else ""
        for pid in filtered_participants:
            if pid == created_by_user_id:
                continue
            await self._bell_to_user(
                user_id=pid,
                org_id=org_id,
                task_id=created.id,
                title=f"Вас добавили в задачу «{created.title}»",
                content=f"Добавил: {actor_label}" if actor_label else None,
            )
```

- [ ] **Step 2: Replace в `add_participant()`**

Найти:
```python
        # Notify
        await self._notify(
            "notify_added_as_participant", task, user_id, by_user_id=actor.id,
        )
```

Заменить на:
```python
        # Notify added user via bell (skip self-add).
        if user_id != actor.id:
            await self._bell_to_user(
                user_id=user_id,
                org_id=task.org_id,
                task_id=task.id,
                title=f"Вас добавили в задачу «{task.title}»",
                content=f"Добавил: {self._format_actor(actor)}",
            )
```

- [ ] **Step 3: Replace в `remove_participant()`**

Найти:
```python
        # Notify
        await self._notify(
            "notify_removed_as_participant", task, user_id, by_user_id=actor.id,
        )
```

Заменить на:
```python
        # Notify removed user via bell (skip self-remove).
        if user_id != actor.id:
            await self._bell_to_user(
                user_id=user_id,
                org_id=task.org_id,
                task_id=task.id,
                title=f"Вас исключили из задачи «{task.title}»",
                content=f"Исключил: {self._format_actor(actor)}",
            )
```

- [ ] **Step 4: Sanity-парсер**

Run: `/root/rugpt/venv/bin/python -c "import ast; ast.parse(open('/root/rugpt/src/engine/services/task_service.py').read()); print('parse ok')"`

Expected: `parse ok`

---

### Task 5: Удалить все 9 оставшихся `_notify(...)` вызовов и сам helper

**Files:**
- Modify: `src/engine/services/task_service.py`

- [ ] **Step 1: Удалить 8 `_notify` в transitions**

Открыть файл и удалить ровно строки вида (одна на каждое — оставить только следующий за ними `_bell_to_recipients(...)`):

```python
        await self._notify("notify_take", updated, user)
```
```python
        await self._notify("notify_mark_done", updated, user)
```
```python
        await self._notify("notify_accept", updated, user)
```
```python
        await self._notify("notify_reject", updated, user, comment)
```
```python
        await self._notify("notify_set_deadline", updated, user)
```
```python
        await self._notify("notify_propose_deadline", updated, user)
```
```python
        await self._notify("notify_accept_proposed_deadline", updated, user)
```
```python
        await self._notify("notify_reject_proposed_deadline", updated, user)
```

- [ ] **Step 2: Удалить `_notify` в `check_overdue`**

Найти:
```python
                await self._notify("notify_overdue", task)

                # Bell всем involved (creator + assignee + participants).
```

Заменить на:
```python
                # Bell всем involved (creator + assignee + participants).
```

(Удалили строку `_notify` и пустую строку после неё.)

- [ ] **Step 3: Удалить сам метод `_notify` в `TaskService`**

Найти и удалить блок целиком (включая docstring):
```python
    async def _notify(self, method_name: str, *args, **kwargs) -> None:
        """Best-effort PM notification via TaskNotificationService. Silent no-op if absent."""
        if self.task_notification_service is None:
            return
        try:
            method = getattr(self.task_notification_service, method_name)
            await method(*args, **kwargs)
        except Exception as e:
            logger.error(f"PM notify {method_name} failed: {e}")
```

- [ ] **Step 4: Проверить, что больше нет вызовов `self._notify(`**

Run: `grep -n "self._notify(" /root/rugpt/src/engine/services/task_service.py || echo "clean"`

Expected: `clean` (выход без совпадений).

- [ ] **Step 5: Sanity-парсер**

Run: `/root/rugpt/venv/bin/python -c "import ast; ast.parse(open('/root/rugpt/src/engine/services/task_service.py').read()); print('parse ok')"`

Expected: `parse ok`

---

### Task 6: Удалить параметр `task_notification_service` из `TaskService.__init__`

**Files:**
- Modify: `src/engine/services/task_service.py`

- [ ] **Step 1: Убрать импорт TYPE_CHECKING**

В `src/engine/services/task_service.py` найти блок:
```python
if TYPE_CHECKING:
    from .task_notification_service import TaskNotificationService
```

Удалить строку с `from .task_notification_service import TaskNotificationService`. Если в TYPE_CHECKING остаётся пустой блок — оставить `if TYPE_CHECKING: pass` либо весь блок удалить, в зависимости от контекста (см. что ещё внутри).

Проверить, нужен ли вообще TYPE_CHECKING — если только для TaskNotificationService, удалить весь блок и импорт `TYPE_CHECKING` из typing.

Run: `grep -n "TYPE_CHECKING\|from typing import" /root/rugpt/src/engine/services/task_service.py`

Если `TYPE_CHECKING` теперь не используется — удалить из строки `from typing import ...`.

- [ ] **Step 2: Убрать параметр конструктора и атрибут**

В `__init__` `TaskService` найти и удалить строку:
```python
        task_notification_service: Optional["TaskNotificationService"] = None,
```

Также удалить присваивание:
```python
        self.task_notification_service = task_notification_service
```

- [ ] **Step 3: Убедиться, что других ссылок на `task_notification_service` в файле нет**

Run: `grep -n "task_notification_service" /root/rugpt/src/engine/services/task_service.py || echo "clean"`

Expected: `clean`.

- [ ] **Step 4: Sanity-парсер**

Run: `/root/rugpt/venv/bin/python -c "import ast; ast.parse(open('/root/rugpt/src/engine/services/task_service.py').read()); print('parse ok')"`

Expected: `parse ok`

---

### Task 7: Убрать wiring `TaskNotificationService` в `engine_service.py`

**Files:**
- Modify: `src/engine/services/engine_service.py`

- [ ] **Step 1: Убрать импорт**

В `src/engine/services/engine_service.py` найти и удалить строку:
```python
from .task_notification_service import TaskNotificationService
```

- [ ] **Step 2: Убрать конструирование `TaskNotificationService`**

Найти и удалить блок (около строк 162-170):
```python
        # TaskNotificationService — PM agent posts notifications to direct chats
        # via chat_service + message_storage, publishes to chat.events for live WS delivery.
        self.task_notification_service = TaskNotificationService(
            chat_service=self.chat_service,
            message_storage=self.message_storage,
            user_storage=self.user_storage,
            kafka_producer=self.kafka_producer,
            task_participant_storage=self.task_participant_storage,
        )

```

(Включая trailing пустую строку после блока.)

- [ ] **Step 3: Убрать передачу `task_notification_service` в `TaskService(...)`**

Найти конструирование `TaskService` (около строк 173-182). Удалить строку:
```python
            task_notification_service=self.task_notification_service,
```

- [ ] **Step 4: Sanity-парсер**

Run: `/root/rugpt/venv/bin/python -c "import ast; ast.parse(open('/root/rugpt/src/engine/services/engine_service.py').read()); print('parse ok')"`

Expected: `parse ok`

- [ ] **Step 5: Проверить, что атрибут `task_notification_service` больше нигде не используется в src/engine**

Run: `grep -rn "task_notification_service\|TaskNotificationService" /root/rugpt/src/engine 2>/dev/null | grep -v task_notification_service.py`

Expected: только результат из `routes/chats.py:540` (комментарий, обновим в Task 9).

---

### Task 8: Удалить файл `task_notification_service.py`

**Files:**
- Delete: `src/engine/services/task_notification_service.py`

- [ ] **Step 1: Удалить файл**

Run: `rm /root/rugpt/src/engine/services/task_notification_service.py`

- [ ] **Step 2: Подтвердить отсутствие**

Run: `test ! -f /root/rugpt/src/engine/services/task_notification_service.py && echo "removed"`

Expected: `removed`

- [ ] **Step 3: Проверить, что engine стартует без него**

Run: `/root/rugpt/venv/bin/python -c "from src.engine.services.engine_service import EngineService; print('import ok')"`

Expected: `import ok` (без ImportError).

---

### Task 9: Обновить комментарий в `routes/chats.py`

**Files:**
- Modify: `src/engine/routes/chats.py`

- [ ] **Step 1: Прочитать комментарий**

Run: `sed -n '535,545p' /root/rugpt/src/engine/routes/chats.py`

- [ ] **Step 2: Заменить упоминание `task_notification_service`**

В строке 540 (или близкой) найти:
```
    # Same pattern as task_notification_service / agent_handler — engine
```

Заменить на:
```
    # Same pattern as agent_handler — engine
```

(Удалили устаревшее упоминание `task_notification_service`. Остальной текст комментария не трогаем.)

- [ ] **Step 3: Подтвердить**

Run: `grep -n "task_notification_service" /root/rugpt/src/engine/routes/chats.py || echo "clean"`

Expected: `clean`

---

### Task 10: Удалить тест `test_task_notifications.py`

**Files:**
- Delete: `tests/test_task_notifications.py`

- [ ] **Step 1: Удалить файл**

Run: `rm /root/rugpt/tests/test_task_notifications.py`

- [ ] **Step 2: Подтвердить**

Run: `test ! -f /root/rugpt/tests/test_task_notifications.py && echo "removed"`

Expected: `removed`

---

### Task 11: Удалить устаревший integration-тест

**Files:**
- Modify: `tests/integration/test_kafka_end_to_end.py`

- [ ] **Step 1: Прочитать файл, найти границы `test_pm_notification_published_to_chat_events`**

Run: `grep -n "^def test_\|^async def test_\|TaskNotificationService\|PM notification" /root/rugpt/tests/integration/test_kafka_end_to_end.py`

Ожидание: видны def'ы тестов (`test_pm_notification_published_to_chat_events` и второй тест про AI mention).

- [ ] **Step 2: Удалить блок `test_pm_notification_published_to_chat_events`**

В `tests/integration/test_kafka_end_to_end.py` удалить весь функциональный блок от `def test_pm_notification_published_to_chat_events():` (строка 76) до `await consumer.stop()` (строка 168) включительно с trailing пустой строкой.

Тест проверял удалённое поведение (PM-сообщение → chat.events) — после удаления `task_notification_service` оно больше не воспроизводится.

- [ ] **Step 3: Обновить module docstring**

Найти в начале файла (строки 1-13) docstring и удалить пункт 1:
```
1. PM notifications travel from TaskService.take_task through TaskNotificationService,
   get persisted in messages table, and are published to chat.events in a form that
   a fresh aiokafka consumer can read.
```

Перенумеровать оставшийся пункт (был 2, станет 1):
```
2. AI mention @@role triggers a Kafka publish to agent.requests with correct payload
```
→
```
1. AI mention @@role triggers a Kafka publish to agent.requests with correct payload
```

(Пункт 3 «Fixtures cleanup» становится пунктом 2.)

- [ ] **Step 4: Sanity-парсер**

Run: `/root/rugpt/venv/bin/python -c "import ast; ast.parse(open('/root/rugpt/tests/integration/test_kafka_end_to_end.py').read()); print('parse ok')"`

Expected: `parse ok`

- [ ] **Step 5: Подтвердить отсутствие упоминаний**

Run: `grep -n "TaskNotificationService\|task_notification_service\|test_pm_notification" /root/rugpt/tests/integration/test_kafka_end_to_end.py || echo "clean"`

Expected: `clean`

---

### Task 12: Smoke-тест на bell-нотификации (TDD: один интеграционный)

**Files:**
- Create: `tests/test_task_service_bell.py`

Один интеграционный тест убеждается, что после `take_task` в `in_app_notifications` появляется row для creator с правильными `type`/`title`/`content`/`reference_*`. Покрывает E2E без mock'ов.

- [ ] **Step 1: Написать тест**

Создать `tests/test_task_service_bell.py`:

```python
"""Bell-нотификация после take_task для creator (smoke).

Реальная БД (как test_resolve_recipients.py). Скипается если нет DSN.
"""
import os
import pytest
import pytest_asyncio
import asyncpg
from uuid import uuid4

from src.engine.services.engine_service import EngineService

DSN = os.environ.get("DATABASE_URL", "postgresql://postgres@localhost/rugpt")


@pytest_asyncio.fixture
async def env():
    e = EngineService()
    await e.initialize()
    pool = await asyncpg.create_pool(DSN)
    async with pool.acquire() as conn:
        org = await conn.fetchval(
            "INSERT INTO organizations (id, name, slug) "
            "VALUES (gen_random_uuid(), 'bell', $1) RETURNING id",
            f"bell_{uuid4().hex[:8]}",
        )
        users = {}
        for tag in ("creator", "assignee"):
            uid = await conn.fetchval(
                "INSERT INTO users (id, org_id, username, name, password_hash, email, is_active) "
                "VALUES (gen_random_uuid(), $1, $2, $2, 'x', $3, true) RETURNING id",
                org, f"bell_{tag}", f"bell_{tag}_{uuid4()}@test.local",
            )
            users[tag] = uid
        yield {
            "engine": e, "org_id": org, "creator": users["creator"],
            "assignee": users["assignee"], "pool": pool,
        }
        async with pool.acquire() as conn2:
            await conn2.execute(
                "DELETE FROM in_app_notifications WHERE user_id = ANY($1::uuid[])",
                list(users.values()),
            )
            await conn2.execute("DELETE FROM tasks WHERE org_id = $1", org)
            await conn2.execute(
                "DELETE FROM users WHERE id = ANY($1::uuid[])", list(users.values()),
            )
            await conn2.execute("DELETE FROM organizations WHERE id = $1", org)
    await pool.close()
    await e.close()


@pytest.mark.asyncio
async def test_take_task_creates_bell_for_creator(env):
    engine = env["engine"]
    task = await engine.task_service.create(
        org_id=env["org_id"],
        title="bell-test",
        assignee_user_id=env["assignee"],
        created_by_user_id=env["creator"],
    )
    assignee_user = await engine.user_storage.get_by_id(env["assignee"])
    await engine.task_service.take_task(task.id, assignee_user)

    async with env["pool"].acquire() as conn:
        rows = await conn.fetch(
            "SELECT type, title, content, reference_type, reference_id "
            "FROM in_app_notifications "
            "WHERE user_id = $1 AND reference_id = $2 "
            "ORDER BY created_at DESC",
            env["creator"], task.id,
        )

    bell_take = [r for r in rows if r["title"].endswith("взята в работу")]
    assert len(bell_take) == 1, f"expected 1 bell row, got {len(bell_take)}: {rows}"
    r = bell_take[0]
    assert r["type"] == "task_status_change"
    assert r["reference_type"] == "task"
    assert str(r["reference_id"]) == str(task.id)
    assert r["title"] == f"Задача «bell-test» взята в работу"
    assert r["content"]  # non-empty actor name
```

- [ ] **Step 2: Запустить тест**

Run: `cd /root/rugpt && venv/bin/pytest tests/test_task_service_bell.py -v`

Expected: 1 PASS (если DSN доступен; иначе — skip).

---

### Task 13: Прогон полного тест-сьюта

**Files:** none

- [ ] **Step 1: Запустить все юнит-тесты, исключая integration**

Run: `cd /root/rugpt && venv/bin/pytest tests/ -v --ignore=tests/integration -x 2>&1 | tail -50`

Expected: green либо предсказуемые skip'ы (БД-зависимые если DSN не задан). Никаких новых failures.

- [ ] **Step 2: Если есть failures — починить**

Если упал тест, который ссылался на удалённый `task_notification_service` или `_notify` — исправить точечно. Большинство тестов не должны быть затронуты, потому что:
- `_resolve_recipients` живёт под тем же именем в `task_service`.
- `_bell_to_recipients` уже работал параллельно с `_notify`.

Если упало что-то в `test_task_service_participants.py` или похожем — посмотреть assertion и обновить.

Run: `cd /root/rugpt && venv/bin/pytest tests/ -v --ignore=tests/integration 2>&1 | grep -E "FAIL|ERROR" | head -20`

- [ ] **Step 3: Финальный grep на устаревшие имена в src/**

Run: `grep -rn "TaskNotificationService\|task_notification_service\|notify_take\|notify_mark_done\|notify_accept\|notify_reject\|notify_set_deadline\|notify_propose_deadline\|notify_overdue\|notify_added_as_participant\|notify_removed_as_participant" /root/rugpt/src 2>/dev/null || echo "clean"`

Expected: `clean`.

---

## Self-Review

**Spec coverage:**
- ✅ Удалить файл `task_notification_service.py` → Task 8.
- ✅ Удалить параметр `task_notification_service` из TaskService — Task 6.
- ✅ Удалить атрибут — Task 6 step 2.
- ✅ Удалить helper `_notify` — Task 5 step 3.
- ✅ Удалить 9 transition `_notify` вызовов — Task 5 steps 1-2.
- ✅ Удалить 3 participant `_notify` вызова — Task 4 (заменены на `_bell_to_user`).
- ✅ Engine_service удалить импорт + конструктор + параметр — Task 7.
- ✅ Inline `_resolve_recipients` — Task 1.
- ✅ Helper `_bell_to_user` — Task 3.
- ✅ Замена 3 participant вызовов — Task 4.
- ✅ Спека-таблица titles+content — Task 2.
- ✅ Покрытие overdue → all involved — уже в текущем коде; Task 2 step 9 подтверждает.
- ✅ Frontend без изменений — план их не трогает.
- ✅ Тесты `test_task_notifications.py` удалить — Task 10.
- ✅ Миграция `test_resolve_recipients.py` — Task 1 step 4.
- ✅ Integration `test_kafka_end_to_end.py` — Task 11.
- ✅ Комментарий `routes/chats.py:540` — Task 9.
- ✅ Smoke-тест bell — Task 12.

**Placeholder scan:** нет TBD/TODO/«similar to». Все шаги содержат конкретные `grep`/`sed`/код.

**Type consistency:**
- `_resolve_recipients(task, exclude_user_id)` — определён Task 1, использован в `_bell_to_recipients` Task 1 step 3.
- `_bell_to_user(user_id, org_id, task_id, title, content)` — определён Task 3, используется Task 4 steps 1-3.
- `_format_actor(user)` — определён Task 1 step 2, используется Task 2 steps 1-7 и Task 4.
- `_bell_to_recipients` (уже в коде) — сигнатура `(task, actor_user_id, title, content=None)` — кейс параметров согласован с вызовами Task 2.

Готово.
