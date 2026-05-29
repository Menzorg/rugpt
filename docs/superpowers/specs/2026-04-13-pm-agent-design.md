# PM-агент и уведомления о задачах — дизайн

> Дата: 2026-04-13
> Статус: утверждено к имплементации
> Пункт роадмапа: 10

## Контекст

Пункт 9 (приоритизация и владение задачами) реализован: задачи имеют создателя, статусы переходят через семантические эндпоинты (`take_task` / `mark_done` / `accept_task` / `reject_task`), есть негоциация дедлайна. Но **уведомлений о изменениях нет** — пользователь должен сам зайти в `/tasks` и посмотреть.

Этот пункт добавляет PM-агент: персональный AI-ассистент по задачам, который:
1. Доступен как 4-й агент на главной странице (для будущих диалогов в п.12)
2. Автоматически пишет в личный direct-чат каждому пользователю когда что-то меняется в его задачах

## Цели

1. **Seed PM роли** — добавить `pm` как обычную AI-роль (`agent_type='simple'`), чтобы автоматически появилась на главной странице WebClient
2. **TaskNotificationService** — реагирует на изменения задач, отправляет plain-text сообщения в direct-чат PM↔пользователь
3. **Lazy создание чата** — direct-чат PM↔пользователь создаётся при первом уведомлении этому пользователю
4. **Правило адресации** — уведомляем того, кто **не инициировал** изменение (создатель видит действия исполнителя, исполнитель видит действия создателя)

## Объём

### Входит
- Миграция: PM роль + system user `pm` в RuGPT системной организации
- Файл `src/engine/prompts/pm.md` — system prompt для PM
- Новый сервис `TaskNotificationService` в `src/engine/services/task_notification_service.py`
- Интеграция в `TaskService` — каждый метод перехода вызывает соответствующий метод нотификации
- Интеграция в `EngineService` (DI)
- Тесты для `TaskNotificationService` (mocked storages)

### НЕ входит
- Интерактивные кнопки в сообщениях PM (намеренно отброшено — пользователь идёт в `/tasks` для действий)
- `acting_on_behalf_of_user_id` поле (отброшено — не нужно, "по команде кого" живёт в тексте сообщения и в `correlation_id` логов)
- Conversational tools для PM (`chat_post`, `summary` и т.д.) — это пункт 12
- WebClient изменения — никакого нового UI, сообщения отрисовываются существующим механизмом чатов
- Удаление существующих in-app notifications (пусть остаются как параллельный канал колокольчика)

## Архитектура

### PM роль (seed)

Миграция `017_pm_role.sql` создаёт:

1. **PM роль** в RuGPT org (`org_id = 00000000-0000-0000-0000-000000000000`):
   - `code = 'pm'`
   - `name = 'Проджект-менеджер'`
   - `description = 'AI-ассистент для управления задачами и уведомлений'`
   - `agent_type = 'simple'`
   - `model_name = 'qwen3:14b'` (или текущий по умолчанию — соответствует другим ролям)
   - `tools = ["task_create", "task_query", "task_update"]` (только существующие)
   - `prompt_file = 'pm.md'`
   - `is_active = true`

2. **System user `pm`** в той же org:
   - `username = 'pm'`
   - `name = 'PM-агент'`
   - `email = 'pm@rugpt.system'`
   - `role_id = <pm role id>`
   - `is_admin = false`, `is_system = true`, `is_active = true`

Использовать `ON CONFLICT DO NOTHING` для идемпотентности, как в миграции 003.

После миграции PM **автоматически** появляется на главной странице WebClient — фронт уже отображает 4-й агент потому что список ролей берётся из `/api/v1/roles`, никакого фронт-кода менять не надо.

### System prompt (`src/engine/prompts/pm.md`)

```markdown
Вы — PM-агент, проджект-менеджер. Вы помогаете руководителям и сотрудникам управлять задачами.

Ваши обязанности:
- Помочь поставить задачу: уточнить срок, исполнителя, описание
- Проверить статус задач сотрудника или подразделения
- Отслеживать просроченные задачи и обращать внимание руководителя
- Объяснить как работает система задач если кто-то не понимает

Тон: деловой, краткий, помогающий. Отвечайте по-русски.

Вам доступны инструменты:
- task_create — создать задачу для сотрудника
- task_query — посмотреть список задач (по исполнителю, по статусу)
- task_update — обновить название или описание задачи

Когда пользователь просит создать задачу — уточните название, исполнителя и срок если они не указаны явно. После создания подтвердите.

Когда пользователь просит "покажи мои задачи" — используйте task_query с фильтром по нему.

Если вас просят сделать что-то, для чего у вас нет инструмента (например, написать в чат другой задачи), вежливо объясните что вы пока не умеете это, но скоро научитесь.
```

### TaskNotificationService

Новый файл `src/engine/services/task_notification_service.py`:

```python
"""
TaskNotificationService

Sends plain-text notifications to user's direct chat with PM agent
when task state changes. Item 10 from roadmap.
"""
import logging
from typing import Optional
from uuid import UUID

from ..models.task import Task
from ..models.user import User
from .chat_service import ChatService
from ..storage.user_storage import UserStorage
from ..storage.message_storage import MessageStorage

logger = logging.getLogger("rugpt.services.task_notification")


class TaskNotificationService:
    """
    Posts plain-text notifications about task events to PM↔user direct chats.

    Lazily creates the direct chat on first notification.
    Notifies the OPPOSITE party of who initiated the change:
    - Assignee transitions → notify creator
    - Creator transitions → notify assignee
    - Scheduler/system → notify both
    """

    def __init__(
        self,
        chat_service: ChatService,
        message_storage: MessageStorage,
        user_storage: UserStorage,
        pm_user_id: Optional[UUID] = None,
    ):
        self.chat_service = chat_service
        self.message_storage = message_storage
        self.user_storage = user_storage
        self._pm_user_id = pm_user_id  # cached lookup, populated at first call if None

    async def _get_pm_user_id(self) -> Optional[UUID]:
        if self._pm_user_id is not None:
            return self._pm_user_id
        # Look up the system user with username 'pm'
        pm = await self.user_storage.get_by_username('pm')
        if pm is None:
            logger.error("PM system user not found — migration 017 not applied?")
            return None
        self._pm_user_id = pm.id
        return self._pm_user_id

    async def _post(self, recipient_user_id: UUID, recipient_org_id: UUID, text: str, task_id: UUID):
        """Lazy-create direct chat PM↔recipient and post a message from PM."""
        pm_id = await self._get_pm_user_id()
        if pm_id is None:
            return

        chat = await self.chat_service.create_direct_chat(pm_id, recipient_user_id, recipient_org_id)

        # Persist message via message_storage directly (or chat_service.send_message if it exists)
        from ..models.message import Message
        from datetime import datetime
        msg = Message(
            chat_id=chat.id,
            sender_id=pm_id,
            content=text,
            created_at=datetime.utcnow(),
        )
        await self.message_storage.create(msg)
        logger.info(
            f"PM notified user={recipient_user_id} task={task_id}: {text[:80]}"
        )

    # ============================================
    # Notifications by transition
    # ============================================

    async def notify_take(self, task: Task, actor: User, creator: Optional[User]):
        """Assignee took the task. Notify creator."""
        if creator is None:
            return  # legacy task without creator
        text = (
            f"@{actor.username or actor.name} взял задачу «{task.title}» в работу.\n"
            f"→ Открыть задачу: /tasks#{task.id}"
        )
        await self._post(creator.id, task.org_id, text, task.id)

    async def notify_mark_done(self, task: Task, actor: User, creator: Optional[User]):
        """Assignee marked task done (awaiting review). Notify creator."""
        if creator is None:
            return
        text = (
            f"@{actor.username or actor.name} отметил задачу «{task.title}» готовой. Ожидает вашей приёмки.\n"
            f"→ Открыть: /tasks#{task.id}"
        )
        await self._post(creator.id, task.org_id, text, task.id)

    async def notify_accept(self, task: Task, actor: User, assignee: Optional[User]):
        """Creator accepted task. Notify assignee."""
        if assignee is None:
            return
        text = f"Ваша работа «{task.title}» принята. Спасибо."
        await self._post(assignee.id, task.org_id, text, task.id)

    async def notify_reject(self, task: Task, actor: User, assignee: Optional[User], comment: Optional[str]):
        """Creator rejected task back to in_progress. Notify assignee, include comment if any."""
        if assignee is None:
            return
        text = f"Задача «{task.title}» возвращена в работу."
        if comment:
            text += f"\nПричина: {comment}"
        text += f"\n→ Открыть: /tasks#{task.id}"
        await self._post(assignee.id, task.org_id, text, task.id)

    async def notify_set_deadline(self, task: Task, actor: User, assignee: Optional[User]):
        """Creator changed deadline directly. Notify assignee."""
        if assignee is None:
            return
        deadline_str = task.deadline.strftime("%d.%m.%Y %H:%M") if task.deadline else "—"
        text = (
            f"Срок задачи «{task.title}» изменён: {deadline_str}\n"
            f"→ Открыть: /tasks#{task.id}"
        )
        await self._post(assignee.id, task.org_id, text, task.id)

    async def notify_propose_deadline(self, task: Task, actor: User, creator: Optional[User]):
        """Assignee proposed a new deadline. Notify creator."""
        if creator is None:
            return
        proposed_str = task.proposed_deadline.strftime("%d.%m.%Y %H:%M") if task.proposed_deadline else "—"
        text = (
            f"@{actor.username or actor.name} предложил перенести срок задачи «{task.title}» на {proposed_str}.\n"
            f"→ Принять или отклонить: /tasks#{task.id}"
        )
        await self._post(creator.id, task.org_id, text, task.id)

    async def notify_accept_proposed_deadline(self, task: Task, actor: User, assignee: Optional[User]):
        """Creator accepted assignee's proposed deadline. Notify assignee."""
        if assignee is None:
            return
        deadline_str = task.deadline.strftime("%d.%m.%Y %H:%M") if task.deadline else "—"
        text = f"Ваше предложение нового срока для «{task.title}» принято: {deadline_str}"
        await self._post(assignee.id, task.org_id, text, task.id)

    async def notify_reject_proposed_deadline(self, task: Task, actor: User, assignee: Optional[User]):
        """Creator rejected assignee's proposed deadline. Notify assignee."""
        if assignee is None:
            return
        text = f"Ваше предложение нового срока для «{task.title}» отклонено."
        await self._post(assignee.id, task.org_id, text, task.id)

    async def notify_overdue(self, task: Task, assignee: Optional[User], creator: Optional[User]):
        """Task became overdue. Notify both assignee and creator."""
        text = f"Задача «{task.title}» просрочена.\n→ Открыть: /tasks#{task.id}"
        if assignee is not None:
            await self._post(assignee.id, task.org_id, text, task.id)
        if creator is not None and (assignee is None or creator.id != assignee.id):
            await self._post(creator.id, task.org_id, text, task.id)
```

### Интеграция в TaskService

`TaskService.__init__` принимает дополнительный параметр `task_notification_service: Optional[TaskNotificationService] = None` (опциональный для тестов).

Каждый метод перехода после успешного вызова `storage.update(task)` вызывает соответствующий метод нотификации:

```python
    async def take_task(self, task_id: UUID, user: User) -> Task:
        task = await self.storage.get_by_id(task_id)
        if not task: raise ValueError(...)
        self._check_assignee(task, user)
        if task.status != "created": raise ValueError(...)
        task.status = "in_progress"
        updated = await self.storage.update(task)

        # Notify creator
        if self.task_notification_service:
            creator = None
            if updated.created_by_user_id:
                creator = await self.user_storage.get_by_id(updated.created_by_user_id)
            await self.task_notification_service.notify_take(updated, user, creator)

        return updated
```

Аналогично для остальных методов перехода. Загрузка `creator` или `assignee` объекта User делается через `user_storage.get_by_id(...)` — в TaskService нужно добавить зависимость на `user_storage`. Альтернатива: загружать в TaskNotificationService (прячет детали, но требует двух дополнительных запросов).

**Решение:** загружать в TaskNotificationService — это его дело знать кому что отправить. TaskService просто передаёт `task` и `actor`. Сервис нотификаций сам делает lookup creator/assignee.

Тогда сигнатуры упрощаются:

```python
    async def notify_take(self, task: Task, actor: User):
        creator_id = task.created_by_user_id
        if creator_id is None:
            return
        creator = await self.user_storage.get_by_id(creator_id)
        if creator is None:
            return
        text = ...
        await self._post(creator.id, task.org_id, text, task.id)
```

И TaskService остаётся не зависящим от user_storage:

```python
    async def take_task(self, task_id: UUID, user: User) -> Task:
        ...
        updated = await self.storage.update(task)
        if self.task_notification_service:
            await self.task_notification_service.notify_take(updated, user)
        return updated
```

Чище.

### Интеграция в EngineService

В `engine_service.py` после создания TaskService и ChatService:

```python
self.task_notification_service = TaskNotificationService(
    chat_service=self.chat_service,
    message_storage=self.message_storage,
    user_storage=self.user_storage,
)

# Inject into TaskService
self.task_service.task_notification_service = self.task_notification_service
```

Альтернатива — передать в конструктор TaskService. Решить на имплементации; обе ОК.

### check_overdue (scheduler)

Существующий метод `TaskService.check_overdue()` тоже должен использовать новый сервис. После пометки задачи overdue:

```python
if self.task_notification_service:
    await self.task_notification_service.notify_overdue(task)
```

Существующий вызов `notification_service.create(...)` (in-app для колокольчика) **остаётся как есть** — это параллельный канал.

## Идемпотентность direct-чата

`chat_service.create_direct_chat` уже идемпотентен — он сначала проверяет существующий чат через `chat_storage.get_direct_chat(user1, user2)` и возвращает его если есть. Никаких дубликатов.

## org_id для PM-чата

PM-агент находится в **системной RuGPT org** (`00000000-0000-0000-0000-000000000000`). Юзер находится в своей org. При создании direct-чата используется `task.org_id` (= org юзера). Это корректно, потому что:
- Direct-чаты с системными пользователями уже работают через `recipient_org_id` (см. как реализовано для существующих AI ролей)
- Все участники чата корректно резолвятся, если хранение участников в `chat.participants` JSONB не привязано к одной org

**Проверить на имплементации:** работает ли `chat_storage.get_direct_chat(user1_id, user2_id)` для пары "юзер из org A + system user из системной org". Если есть проблема — fallback использовать org_id системы вместо org юзера. Скорее всего работает, потому что существующие AI-роли (GPT-OSS 20B, Qwen3, GLM) уже так общаются.

## Тестирование

Новые тесты в `tests/test_task_notifications.py`:

1. `test_notify_take_notifies_creator` — мок storages, вызов `notify_take`, проверить что чат создан и сообщение постится
2. `test_notify_take_skips_legacy_task_no_creator` — задача без `created_by_user_id` → no-op
3. `test_notify_accept_notifies_assignee` — после `accept_task` сообщение уходит исполнителю
4. `test_notify_reject_includes_comment` — текст содержит `comment` если задан
5. `test_notify_overdue_notifies_both` — оба получают сообщение
6. `test_notify_overdue_skips_duplicate_when_creator_is_assignee` — если creator == assignee, отправляется один раз
7. `test_lazy_chat_creation` — несколько уведомлений тому же юзеру создают чат один раз (через идемпотентность `create_direct_chat`)
8. `test_pm_user_lookup_cached` — `_get_pm_user_id` вызывается один раз, дальше из кеша

Существующие тесты `test_task_ownership.py` должны продолжать работать — TaskService с `task_notification_service=None` должен пропускать нотификации без ошибок.

## Метрики успеха

После имплементации п.10:
1. PM-агент виден на главной странице как 4-й агент (рядом с GPT/Qwen/Claude/PM)
2. Пользователь может открыть чат с PM (как с любой другой ролью) — там пусто или есть AI-сообщения
3. Когда исполнитель берёт задачу → создатель получает сообщение от PM в своём direct-чате
4. Когда исполнитель отметил готово → создатель видит "Ожидает приёмки"
5. Когда создатель принял → исполнитель видит "Работа принята"
6. Когда создатель вернул с комментарием → исполнитель видит причину
7. При negotiation дедлайна обе стороны получают уведомления
8. При overdue оба получают сообщение
9. Каждое уведомление содержит ссылку `/tasks#<task_id>`

## Зависимости

**Входные:**
- ✅ п.1 базовая система задач
- ✅ п.8 отделы (PM использует существующий механизм видимости через task_query tool)
- ✅ п.9 владение и переходы (наши hook-точки)

**Выходные:**
- п.12 команды агентам — PM получит новые tools (`chat_post`, `summary` и т.д.)

## Открытые вопросы

1. **Кросс-org direct-чаты с system user** — на 99% работают как для существующих AI-ролей. Проверить на этапе имплементации первым делом, если что — fallback на системный org_id.
2. **Deep-link `/tasks#<task_id>`** — формат HASH-параметра. Предлагается `/tasks?expand=<id>` как query param, потому что Next.js App Router легче читает query чем hash. Финализировать на этапе UI fix.
3. **PM в `/users` интерфейсе** — будет ли PM показываться в списке пользователей `/users`? Существующие AI-роли (GPT-OSS и т.д.) уже фильтруются как `is_system=true` — PM попадёт в ту же категорию. Скорее всего никаких правок не нужно, но проверить.
