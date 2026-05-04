# Bell как единый центр нотификаций

**Дата:** 2026-05-03
**Статус:** утверждено пользователем для имплементации

## Контекст

В рамках item 10 в engine был введён `TaskNotificationService` (`src/engine/services/task_notification_service.py`): «PM-агент» постит сообщения в direct-чат `pm <-> recipient` при каждой transition задачи (take, mark_done, accept, reject, set_deadline, propose_deadline, accept_proposed_deadline, reject_proposed_deadline, overdue) и при add/remove participant. Сообщения дополнительно публикуются в Kafka `chat.events` для real-time доставки через WS.

Параллельно существует bell-механика (`InAppNotificationService` + `in_app_notifications` таблица), используемая только для:
- `new_task` (при создании — assignee)
- `task_status_change` для `overdue` (assignee only)
- `mention`, `poll`, `report`

**Проблема обнаружения (2026-05-03):**

PM-юзер имеет `is_system=true`. В webclient sidebar (`Sidebar.tsx:342`) фильтр `filter((c) => !c.isSystem)` скрывает PM-чат у всех. Главная страница `/` пробрасывает `systemUsers={user.isAdmin ? systemUsers : []}` — обычные сотрудники и head не получают переключатель системных AI вовсе. Получается, что PM-уведомления о transitions:

- админ — может найти их в переключателе системных AI на `/`,
- head / обычный — физически не видит.

То есть item-10 механика работала «в стол» для большинства пользователей.

## Цель

Bell становится единственным каналом нотификаций для всех task transitions, deadline-events и participant add/remove. PM-юзер сохраняется как AI-собеседник для руководителей (отдельная роль, не «стенгазета transitions»).

## Что меняется

### Удаляем

1. **Файл `src/engine/services/task_notification_service.py`** — целиком.

2. **В `src/engine/services/task_service.py`:**
   - Параметр `task_notification_service` из `__init__`.
   - Атрибут `self.task_notification_service`.
   - Helper `_notify(method_name, *args, **kwargs)` (мёртв после удаления вызовов).
   - Все 12 вызовов `await self._notify(...)`:
     - 9 transitions: `take_task`, `mark_done`, `accept_task`, `reject_task`, `set_deadline`, `propose_deadline`, `accept_proposed_deadline`, `reject_proposed_deadline`, `check_overdue`.
     - 3 participant: при создании задачи (`task_service.py:288`), при добавлении участника API (`task_service.py:634`), при удалении (`task_service.py:694`).

3. **В `src/engine/services/engine_service.py`:**
   - Импорт `from .task_notification_service import TaskNotificationService`.
   - Конструирование `TaskNotificationService(...)` (lines 162-170).
   - Параметр `task_notification_service=self.task_notification_service` в вызове `TaskService(...)` (line 179).

4. **Тесты на `task_notification_service`** (если есть в репе) — удалить.

### Сохраняем

- **PM-юзер в БД** — продолжает существовать как AI-собеседник для руководителей.
- **Все накопленные PM-сообщения и direct-чаты** `pm <-> руководитель` — нетронуты.
- **PM в админском переключателе системных AI** — `useUsers.ts:78` не правится, фильтр `INFRA_AI_USERNAMES` не расширяется.
- **`is_system` фильтр в Sidebar** — без изменений.

### Добавляем

1. **Inline `_resolve_recipients`** в `task_service.py` (текущая логика была в `task_notification_service`):
   - Собрать candidate user_ids: `created_by_user_id` + `assignee_user_id` + `participant_user_ids` (из `task_participant_storage.list_user_ids(task.id)` если storage доступен).
   - Дедуп.
   - Исключить `actor_user_id` (если задан).
   - Загрузить каждого через `user_storage.get_by_id(uid)`.
   - Отфильтровать `is_active == false` (fail-closed).

2. **Helper `_bell_to_user(user_id, org_id, task_id, title, content?)`** — single-recipient вариант для add/remove participant. Внутри: best-effort `notification_service.create(...)` с try/except, ошибки в лог. Не валит transition при отказе.

3. **Заменить 3 participant-вызова `notify_*_as_participant`** на `_bell_to_user(...)` с условием `actor_user_id != recipient_user_id` (повторяет существующий guard в `task_notification_service`).

`_bell_to_recipients` уже добавлен в текущий код и привязан к 9 transitions — после удаления `_notify` и inline-`_resolve_recipients` он становится самодостаточным.

## Содержимое bell-карточек

Все нотификации:
- `type = "task_status_change"`
- `reference_type = "task"`
- `reference_id = task.id`
- Кнопка «Перейти» на фронте уже работает → `/chat/task/{id}` (`NotificationDropdown.tsx:42-44`).

| Transition | Title | Content |
|---|---|---|
| `take_task` | `Задача «{title}» взята в работу` | имя assignee |
| `mark_done` | `Задача «{title}» отмечена выполненной` | имя assignee |
| `accept_task` | `Задача «{title}» принята` | — |
| `reject_task` | `Задача «{title}» возвращена в работу` | комментарий руководителя (или пусто, если без коммента) |
| `set_deadline` | `Срок задачи «{title}» изменён` | `Новый срок: dd.mm.yyyy hh:mm` |
| `propose_deadline` | `Предложен новый срок для «{title}»` | `{actor name}: dd.mm.yyyy hh:mm` |
| `accept_proposed_deadline` | `Новый срок для «{title}» принят` | `dd.mm.yyyy hh:mm` |
| `reject_proposed_deadline` | `Новый срок для «{title}» отклонён` | — |
| `check_overdue` | `Задача просрочена: {title}` | — |
| `add_participant` | `Вас добавили в задачу «{title}»` | `Добавил: {actor name}` |
| `remove_participant` | `Вас исключили из задачи «{title}»` | `Исключил: {actor name}` |

`{actor name}` форматируется как `@{username}` если username есть, иначе `{name}` (повторяет логику `_actor_name` из удаляемого `task_notification_service`).

Дата формата `dd.mm.yyyy hh:mm` — `strftime("%d.%m.%Y %H:%M")`, та же что и в текущих PM-текстах.

## Покрытие recipients

- **9 transitions**: `_bell_to_recipients(task, actor_user_id=user.id, ...)` — bell всем involved минус actor.
- **`check_overdue`**: `_bell_to_recipients(task, actor_user_id=None, ...)` — bell всем involved (без исключений; scheduler-driven, актора нет).
- **`add_participant`**: `_bell_to_user(added_user_id, ...)` — только добавленному.
- **`remove_participant`**: `_bell_to_user(removed_user_id, ...)` — только удалённому.

Семантика recipients:
- Для **assignee-action** (take, mark_done, propose_deadline) — actor = assignee, recipients = creator + другие participants.
- Для **creator-action** (accept, reject, set_deadline, accept/reject_proposed_deadline) — actor = creator, recipients = assignee + participants.
- Для **system-action** (overdue) — actor = None, recipients = creator + assignee + participants.
- Для **add_participant** — recipient = добавленный, actor = тот кто добавил (исключается из recipient'а только если совпадают, см. condition).
- Для **remove_participant** — аналогично.

## Изменение охвата overdue

До: bell-уведомление только для assignee.
После: bell-уведомление для creator + assignee + participants.

Это расширение охвата — fix известного gap (creator не получал бы уведомление о просрочке своей же задачи).

## Frontend

Изменений нет. Уже на месте:
- `NotificationDropdown.tsx:130-137` — показывает `title` (line-clamp-2) и `content` (line-clamp-1) под ним.
- `NotificationDropdown.tsx:27-28` — лейбл «Задача» для `task_status_change`.
- `NotificationDropdown.tsx:42-44` — href `/chat/task/{id}` для `reference_type === 'task'`.
- Bell-счётчик в `useNotifications.ts:93` — суммирует unread in-app + pending validation.

## Миграции БД

Нет. Никаких изменений схемы или данных.

## Тесты

- Удалить тесты на `TaskNotificationService` если они есть в `tests/`.
- В тестах на `TaskService` (если есть) проверить, что мокаются ожидания `_notify` — заменить на ожидания `notification_service.create` с правильными аргументами.
- Добавить unit-тест на `_resolve_recipients` (после inline) — особенно edge-cases: actor=None (overdue), no participants, inactive users excluded.

## Совместимость

- Engine API не меняется — `/chats/{id}/messages`, `/in-app-notifications/*`, `/tasks/*` сохраняют контракт.
- WebClient backend не трогается.
- Frontend не трогается.
- Kafka topic `chat.events` остаётся для других producer'ов (mention notifications, correction echoes, AI responses) — не зависит от удаления `task_notification_service`.

## Риски

- Если в БД есть задачи с массой участников и активный scheduler — после деплоя при первой просрочке bell получат все involved (расширение охвата). Это намеренно.
- `_resolve_recipients` теперь живёт в `TaskService`. Если в будущем понадобится тот же резолв в другом месте — выносить в storage-helper или общий util.

## Зависимости

- `task_participant_storage` уже передаётся в `TaskService` (line 181 в `engine_service.py`).
- `user_storage` уже передаётся.
- `notification_service` уже передаётся как `in_app_notification_service`.

Никаких новых зависимостей не добавляется.
