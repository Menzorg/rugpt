# Task Participants Design

**Status:** approved (brainstorming)
**Date:** 2026-05-02
**Authors:** Eduard Pankratov (product), Petr Bezdenezhnykh (engineering)

## Goal

Дать возможность вовлекать в задачу нескольких участников рядом с одним
ответственным. Цель — собрать в task chat всех кому нужно общаться и обмениваться
файлами по задаче, не размывая ответственность за её исполнение.

## Non-goals

- Multi-assignee (несколько ответственных). Ответственный остаётся ровно один.
- Расширение прав participants на изменение полей задачи (статус, дедлайн,
  переназначение). Только участие в чате и просмотр.
- Self-removal (participant не может сам выйти из задачи).
- Тонкая настройка кому какие уведомления приходят (отдельная задача,
  затрагивает и существующих creator/assignee).
- Подвкладки/фильтры внутри `Выполненные`. Единый пул всех done.
- Backfill participants для существующих задач.

## Solution overview

Добавляем сущность «participant» — пользователь, вовлечённый в задачу как
collaborator чата без полномочий на правку задачи. Хранится в отдельной таблице
M:N (`task_participants`). Ответственный (`tasks.assignee_user_id`, NOT NULL,
один) и создатель (`tasks.created_by_user_id`) — отдельные слоты с собственной
семантикой, не пересекаются с participants.

## Data model

### Migration 032

```sql
-- Migration 032: Task participants
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
  'Additional task participants (chat collaborators). Separate from assignee (one) and creator.';
```

- PK `(task_id, user_id)` — at-most-one participant на задачу. Дубликат INSERT
  → unique violation → 409 наружу.
- `idx_task_participants_user` — поддерживает запрос «все задачи где я
  participant».
- `ON DELETE CASCADE` по `task_id` — гигиена на случай физического удаления
  задачи (в текущем коде не используется, но опасности не создаёт).

### Что НЕ меняем в существующих таблицах

- `tasks.assignee_user_id UUID NOT NULL` — один ответственный.
- `tasks.created_by_user_id UUID` — без изменений.
- `tasks.is_active`, `task_events`, `chats`, `projects` — без структурных
  изменений. В `task_events.event_type` добавляется два значения:
  `participant_added`, `participant_removed`. Это документируется в комментарии,
  без CHECK constraint (его и сейчас нет).

## API

### New endpoints (engine, mounted under `/tasks`)

| Method | Path | Body | Returns |
|---|---|---|---|
| `POST` | `/tasks/{id}/participants` | `{user_id: UUID}` | 201 + `{participant: User}`. 409 если уже есть. |
| `DELETE` | `/tasks/{id}/participants/{user_id}` | — | 204. 404 если такого нет. |
| `GET` | `/tasks/participating` | — | `Task[]` где `user_id` есть в `task_participants`, `is_active=true`, `status != 'done'`. |
| `GET` | `/tasks/done` | — | `Task[]` со `status='done'`, `is_active=true`, где user — creator OR assignee OR participant. |

### Existing endpoints — что меняется

- `GET /tasks/my` — без изменений в фильтрации (assignee + active + status != done),
  но **`?include_done=true` параметр удаляется**. Done живут только в `/tasks/done`.
- `GET /tasks/created-by-me` — то же: `?include_done=true` удаляется.
- `GET /tasks/{id}` — в response добавляется поле `participants: User[]`
  (только активные пользователи, см. ниже про деактивацию).
- `GET /tasks/archive` — без изменений (фильтр `is_active=false`).
- `POST /tasks` — body опционально принимает `participant_user_ids: UUID[]`.
  Если передан, в одной транзакции создаём задачу + строки participants.
- `PATCH /tasks/{id}` — список participants через этот endpoint **не правится**.
  Только через POST/DELETE.

### Response shape для списочных endpoints

Каждая задача в `/my`, `/created-by-me`, `/participating`, `/done` несёт inline:

```jsonc
{
  "id": "...", "title": "...", "status": "...",
  "assignee": {"id": "...", "name": "...", "is_head": false},
  "creator":  {"id": "...", "name": "...", "is_admin": false, "is_head": false},
  "participants": [
    {"id": "...", "name": "..."},
    ...
  ]
}
```

`participants` строится через LEFT JOIN на `task_participants` + JOIN на `users`
с `array_agg`. Загружаем только `users.is_active=true`. Это убирает N+1 запросов
из UI.

### WebClient (NestJS proxy)

В `rugpt.adapter.ts` — соответствующие команды:

- `add_participant` → `POST /tasks/{id}/participants`
- `remove_participant` → `DELETE /tasks/{id}/participants/{user_id}`
- `tasks_participating` → `GET /tasks/participating`
- `tasks_done` → `GET /tasks/done`

В `tasks.controller.ts` — соответствующие routes под `JwtAuthGuard` и
signature pipeline (как остальные task routes).

## Permissions

| Operation | Allowed for |
|---|---|
| Add participant | creator OR head OR admin |
| Remove participant | creator OR head OR admin |
| Self-removal | not allowed (нет endpoint'а) |
| View task with participants | creator OR assignee OR any participant OR head OR admin |
| View `/tasks/{id}` | расширяется: participants теперь тоже могут видеть задачу |

`add_participant` / `remove_participant` симметричны существующим `assign_task`
и `archive_task` по правам — это позволяет переиспользовать существующий guard
helper в `task_service`.

## Hooks and consistency

### Hook: `add_participant(task_id, user_id, by_user)`

В одной транзакции engine:

1. INSERT в `task_participants(task_id, user_id, added_by_user_id, added_at=NOW())`.
   Unique violation на PK → 409 наружу.
2. INSERT в `task_events(event_type='participant_added', actor=by_user,
   payload={user_id, added_by})`.
3. `chat_service.ensure_task_chat(task_id)` — пересчёт participants task chat
   по формуле `creator ∪ assignee ∪ participants`.
4. Если `task.project_id IS NOT NULL` →
   `chat_service.ensure_project_chat(project_id)` — пересчёт participants
   project chat (формула расширяется до `⋃ всех task_chat.participants проекта`).

После транзакции (асинхронно):

5. `task_notification_service.notify_added_as_participant(task, added_user, by_user)`
   → одноразовое уведомление новому participant.
6. Все остальные событийные уведомления (см. секцию Notifications) идут
   единообразно через расширенный `_resolve_recipients` helper.

### Hook: `remove_participant(task_id, user_id, by_user)`

Симметрично:

1. DELETE из `task_participants WHERE task_id=$1 AND user_id=$2`. Если 0 строк → 404.
2. INSERT `task_events(event_type='participant_removed', ...)`.
3. `ensure_task_chat` — пересчёт. Юзер уйдёт из chat **только если** он не
   creator и не assignee этой задачи (формула union даёт это естественно).
4. Если в проекте — `ensure_project_chat`. Юзер уйдёт из project chat **только
   если** он не creator/assignee/participant ни в одной задаче проекта (формула
   union по всем задачам проекта).

После транзакции:

5. Уведомление удалённому — `task_removed_as_participant`.

### Hook: `create_task` с `participant_user_ids: UUID[]`

В транзакции создания: INSERT task → INSERT N строк в task_participants →
INSERT one task_event `created` → `ensure_task_chat` → `ensure_project_chat`
если в проекте.

После транзакции — пакетные уведомления: assignee получает `new_task`,
participants получают `task_added_as_participant`.

### Hook: reassign (`assign_task`)

Симметричный auto-swap между assignee и participants:

- **Новый assignee** становится `tasks.assignee_user_id`. Если он уже был в
  `task_participants` — DELETE его строки (он теперь assignee, в participants
  не дублируется).
- **Старый assignee** автоматически добавляется в `task_participants` —
  сохраняем его историю вовлечённости в задачу. Исключения:
  - если старый assignee совпадает с creator → не добавляем (creator не
    дублируется в participants);
  - если старый assignee совпадает с новым assignee → no-op reassign,
    участников не трогаем.
- `ensure_task_chat` запускается — формула union дедуплицирует, состав чата
  не меняется (старый assignee остаётся в чате через participants, новый
  assignee остаётся через assignee).
- `task_events`: пишем `assignee_changed` (как сейчас). Дополнительные
  `participant_added` / `participant_removed` для авто-свопа **не пишем** —
  это деталь реализации, не отдельное действие пользователя.

Если creator хочет полностью убрать старого assignee из задачи — вызывает
`remove_participant` отдельным запросом после reassign.

### Hook: archive task (soft-delete)

Без специальной обработки `task_participants` — таблица остаётся как есть.
Когда `tasks.is_active=false`, задача не показывается ни в одном из списочных
endpoints (фильтр `WHERE is_active = true`). Если когда-нибудь появится
unarchive — participants автоматически восстановятся, отдельной таблицы
истории не нужно.

### Concurrency

PRIMARY KEY `(task_id, user_id)` гарантирует at-most-one. Параллельный POST на
один и тот же `user_id` → один успех, второй 409. Параллельный POST разных
участников — конфликта нет.

### Helper: `_resolve_recipients`

Рефакторинг `task_notification_service`: вынести формирование receiver list
из 9 точечных методов в один helper:

```python
async def _resolve_recipients(
    self, task: Task, exclude_user_id: Optional[UUID] = None
) -> List[User]:
    """
    Returns active users involved in the task: creator + assignee + participants.
    Deduplicates and excludes the actor (or any other user_id passed in).
    Filters out is_active=false users.
    """
```

Каждый `notify_*` метод заменяется на цикл по результату helper'а с
актор-нейтральным текстом. См. секцию Notifications.

## Notifications

**Решение:** participants получают тот же поток что creator и assignee
(полный комплект). Тонкую настройку кому какие события нужны откладываем
на отдельную задачу, потому что и для existing receiver list (creator,
assignee) набор может быть избыточным.

### Receiver lists после правки

| Существующее событие | Сейчас получают | После правки |
|---|---|---|
| `new_task` | assignee | + participants |
| `task_status_change` (took / mark_done / accept / reject) | creator + assignee | + participants |
| `deadline_set` / `deadline_proposed` / `accepted` / `rejected` | creator + assignee | + participants |
| `assignee_changed` | new + old assignee, creator | + participants |
| `participant_added` / `participant_removed` (другого юзера) | — | creator + assignee + остальные participants |
| `project_changed` | creator + assignee | + participants |
| `task_archived` | assignee | + participants |
| `overdue` | creator + assignee | + participants |

### Новые одноразовые

| Event | Recipient | Channel |
|---|---|---|
| `task_added_as_participant` | новый participant | PM direct chat (тот же канал что `new_task`) |
| `task_removed_as_participant` | удалённый participant | PM direct chat |

### Каналы

Без изменений в transport-слое. Каждое уведомление пройдёт через
`notification_service` (Telegram → Email) и `task_notification_service`
(PM-сообщение в direct chat). Расширяется только receiver list через
`_resolve_recipients` helper.

### Содержание текстов

Используем единый обезличенный текст для каждого события — один набор на всех
получателей (creator, assignee, participants), без ветвления по роли. Это
упрощает helper и убирает риск рассинхронизации формулировок.

Большинство существующих текстов уже обезличены. Правим только те, где есть
обращение в первом лице к получателю:

| Метод | Было | Станет |
|---|---|---|
| `notify_accept` | `Ваша работа «X» принята. Спасибо.` | `Задача «X» принята.` |
| `notify_accept_proposed_deadline` | `Ваше предложение нового срока для «X» принято: Y` | `Предложение нового срока для «X» принято: Y` |
| `notify_reject_proposed_deadline` | `Ваше предложение нового срока для «X» отклонено.` | `Предложение нового срока для «X» отклонено.` |

Остальные тексты (`{actor} взял задачу...`, `Срок задачи изменён...`,
`Задача возвращена в работу`, `Задача просрочена`, и т.д.) — без изменений,
они уже подходят и для assignee, и для creator, и для participants.

Actor (тот кто инициировал событие) исключается из receiver list стандартным
`exclude_user_id=actor.id` в `_resolve_recipients` — он не получает уведомление
о собственном действии.

## UI

### Страница `/tasks`

Вкладки: `Мои` / `Поставленные мной` / **`Участвую`** (новая) /
**`Выполненные`** (новая) / `Архив`.

- Чекбокс «Показывать выполненные» в `Мои` / `Поставленные мной` удаляется.
  Done-задачи теперь только во вкладке `Выполненные`.
- `Участвую` — задачи где я в `participants`, `is_active=true`, `status != 'done'`.
- `Выполненные` — задачи где я как-то связан, `status='done'`, `is_active=true`.
- Карточка задачи в списке — добавляется компактный ряд аватаров participants
  (стек до 3, overflow `+N`). Не показывается если participants пустой.
- Бейджи рядом с названиями вкладок — количество задач в них (`Мои (12)`,
  `Участвую (5)`). Простой `count`, без unread-логики.

### Модалка создания задачи

Под существующим селектором «Ответственный» добавляется блок «Участники» —
multiselect (тот же search/picker что и для assignee, но множественный выбор).

Фильтрация в picker'е:
- Исключаем `is_system=true` (системные юзеры).
- Исключаем уже выбранного assignee.
- Исключаем самого создателя.

Submit формы передаёт `participant_user_ids: UUID[]` в body POST `/tasks`.

### Модалка редактирования задачи

Блок «Участники» — список бейджей с именами и кнопкой `×` у каждого. Кнопка
`+ Добавить участника` снизу.

Видимость кнопок:
- `×` и `+ Добавить участника` показаны только если текущий юзер — creator,
  head или admin.
- Для остальных (assignee, participant) — список read-only.

Эти кнопки работают **inline через отдельные API**: добавление → POST
`/tasks/{id}/participants`, удаление → DELETE `/tasks/{id}/participants/{user_id}`.
PATCH `/tasks/{id}` participants не правит — это отдельная операция с
немедленным persist (не нужно «сохранить форму»).

### Что НЕ меняется в UI

- Боковая панель чатов — без изменений. Task chat сам обновится через
  `ensure_task_chat`, у participant просто появится новый чат в списке.
- Дизайн карточки задачи (status, deadline, priority) — без изменений.
- Project chat в боковой панели — обновится автоматически.

## Edge cases

### Деактивированные пользователи

Если participant был добавлен и потом деактивирован (`users.is_active=false`):
- Строка в `task_participants` остаётся — это аудит.
- В API response `participants` его не отдаём (фильтр в JOIN).
- UI показывает participants только из активных пользователей.
- В уведомлениях деактивированный участник тоже исключён (через JOIN на
  `users.is_active=true` в `_resolve_recipients`).

### Дубликаты на разных уровнях

- Один и тот же user добавляется дважды → 409 (PK violation).
- Попытка добавить creator или assignee как participant → 400 с явным
  сообщением. Service guard проверяет `user_id != task.created_by_user_id`
  и `user_id != task.assignee_user_id` перед INSERT. Это нужно чтобы UI не
  показывал одного и того же человека в двух слотах. Picker фильтрует
  assignee/creator на фронте, но защищаемся и на сервере.
- Reassign: см. секцию `Hook: reassign` — авто-своп. Новый assignee удаляется
  из `task_participants` если был там. Старый assignee добавляется в
  `task_participants` (если не совпадает с creator).

### Удаление creator или assignee из users

Engine не позволяет hard-delete users, только деактивацию. Если creator
деактивирован — задача остаётся, в response `creator: null` или с пометкой
неактивности (как сейчас). Тот же подход для assignee.

### Большое количество participants

Realistically — 5-10 participants на задачу. Структурно ограничений нет, но
UI ограничивает picker (multiselect без upper-bound, обозримо для модератора).
Если потребуется лимит — добавим в service.

## Testing

### Engine

- Unit на `task_participant_storage`:
  - insert/delete/list happy path
  - duplicate insert → IntegrityError → 409
  - delete missing → 0 rows → caller returns 404
  - list filters out deactivated users in returned `User` shape

- Unit на `task_service`:
  - Permission guards: creator/head/admin allowed; assignee/participant/other denied
  - `add_participant` хук: вставка в storage + task_event + ensure_task_chat
    вызовы (mock chat_service, проверить что вызывается с правильным task_id)
  - `remove_participant` хук: симметрично
  - Reject add_participant если user_id == creator или == assignee (400)

- Unit на `_resolve_recipients` helper:
  - creator + assignee + participants
  - deduplication (если кто-то в нескольких ролях)
  - exclude actor
  - filter out deactivated users

- Integration на project chat:
  - Добавил participant в задачу проекта → юзер появился в project chat
  - Удалил из единственной задачи проекта где он был participant → ушёл из
    project chat (но остался во всех других где он creator/assignee)

### WebClient

- Backend: e2e через MockAdapter — новые routes проксируются, signature
  pipeline проходит.
- Frontend: smoke
  - `/tasks` → клик на `Участвую` → API дёргается, рендерится список
  - `/tasks` → клик на `Выполненные` → API дёргается, рендерится список
  - Модалка создания → выбрать 2 participants → submit → POST с правильным body
  - Модалка редактирования (как creator) → видны кнопки управления
  - Модалка редактирования (как participant) → кнопки скрыты

## Migration order (для исполнения plan'а)

1. Engine migration 032
2. Engine model `TaskParticipant`
3. Engine storage `task_participant_storage`
4. Engine service `task_service` extension (add/remove + helper + create extension)
5. Engine routes `/tasks/{id}/participants`, `/tasks/participating`, `/tasks/done`
6. Engine `task_notification_service` рефакторинг через `_resolve_recipients` +
   новые `notify_added_as_participant` / `notify_removed_as_participant`
7. Engine `chat_service.ensure_task_chat` / `ensure_project_chat` — расширение
   формулы союза
8. WebClient backend: adapter + chat.controller routes
9. WebClient frontend: новые вкладки, picker в модалках, бейджи participants
   в карточке списка
10. Тесты: engine unit + integration, webclient smoke
