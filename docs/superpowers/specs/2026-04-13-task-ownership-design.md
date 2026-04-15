# Приоритизация и владение задачами — дизайн

> Дата: 2026-04-13
> Статус: утверждено к имплементации
> Пункт роадмапа: 9

## Контекст

Текущая система задач (п.1) реализована: модели Task/TaskPoll/TaskReport, API, AI-инструменты, scheduler. Но остались принципиальные недоделки:

1. Не трекается **кто поставил** задачу (`created_by_user_id`)
2. Нет **приоритета** — все задачи равны
3. Нет механизма **приёмки работы** — исполнитель сам ставит `done`, что нарушает контроль
4. Дедлайн может менять кто угодно, нет **негоциации**

Пункт 9 закрывает эти недоделки. Он не зависит от других незавершённых пунктов и может быть реализован самостоятельно.

## Цели

1. Фиксировать **автора** задачи — `created_by_user_id` на записи
2. Автоматический **приоритет** задачи по роли создателя
3. Разделение прав: исполнитель движет статус до «готово», создатель принимает
4. **Негоциация дедлайна**: исполнитель может предложить другой, создатель принимает/отклоняет
5. Интерфейс: две вкладки в `/tasks` — «Мои задачи» и «Поставленные мной»

## Объём

### Входит
- Миграция БД: новые поля на `tasks`
- Обновление модели Task, storage, service
- Обновление существующих routes `/tasks/*` с новыми правами
- Новые эндпоинты для негоциации дедлайна
- Обновление AI-tools (`task_create`, `task_update`) — прокидывать `created_by_user_id`
- WebClient: вкладки, бейджи приоритета, кнопки приёмки и предложения срока

### НЕ входит
- Чаты задач и проектов (пункт 11)
- PM-агент и уведомления (пункт 10)
- Команды агентам (пункт 12)
- AI-генерация отчётов (хвост п.1)
- Мобильное приложение

## Модель данных

### Миграция `016_task_ownership.sql`

```sql
-- Миграция 016: Владение задачами и негоциация дедлайна
ALTER TABLE tasks
    ADD COLUMN created_by_user_id UUID REFERENCES users(id),
    ADD COLUMN awaiting_review_at TIMESTAMP WITH TIME ZONE,
    ADD COLUMN proposed_deadline TIMESTAMP WITH TIME ZONE,
    ADD COLUMN proposed_deadline_by UUID REFERENCES users(id);

-- Индекс для "Поставленные мной"
CREATE INDEX IF NOT EXISTS idx_tasks_created_by
    ON tasks(created_by_user_id)
    WHERE is_active = true;

COMMENT ON COLUMN tasks.created_by_user_id IS 'User who created the task (for priority calculation and ownership)';
COMMENT ON COLUMN tasks.awaiting_review_at IS 'Set when assignee moves task to awaiting_review; NULL otherwise';
COMMENT ON COLUMN tasks.proposed_deadline IS 'Alternative deadline proposed by assignee; NULL if no pending proposal';
COMMENT ON COLUMN tasks.proposed_deadline_by IS 'User who proposed the alternative deadline';
```

**Обратная совместимость:** все поля nullable. Существующие задачи:
- `created_by_user_id = NULL` — legacy-задачи без автора, приоритет по умолчанию = 1 (обычный)
- Новые задачи всегда заполняют `created_by_user_id`

**Backfill опционально:** если в будущем понадобится, можно заполнить `created_by_user_id` из истории сообщений чата, где была создана задача. На старте — не делаем.

### Обновление модели `Task`

```python
@dataclass
class Task:
    id: UUID = field(default_factory=uuid4)
    org_id: UUID = field(default_factory=uuid4)
    title: str = ""
    description: Optional[str] = None
    status: str = "created"  # created | in_progress | awaiting_review | done | overdue
    assignee_user_id: UUID = field(default_factory=uuid4)
    created_by_user_id: Optional[UUID] = None
    deadline: Optional[datetime] = None
    awaiting_review_at: Optional[datetime] = None
    proposed_deadline: Optional[datetime] = None
    proposed_deadline_by: Optional[UUID] = None
    is_active: bool = True
    created_at: datetime = field(default_factory=datetime.utcnow)
    updated_at: datetime = field(default_factory=datetime.utcnow)
```

`to_dict()` расширяется для новых полей. Priority не хранится — вычисляется в сервисе при выдаче.

## Статусы и переходы

**Набор статусов:** `created → in_progress → awaiting_review → done`, плюс `overdue` (автоматический).

**Диаграмма переходов:**

```
          [created]
              │
              │ assignee takes
              ▼
        [in_progress]  ◄──────┐
              │               │
              │ assignee done │ creator rejects
              ▼               │
       [awaiting_review] ─────┘
              │
              │ creator accepts
              ▼
            [done]
```

Любой статус → `overdue`: автоматически ставится scheduler-ом при `deadline < now` для не-`done` задач.

**Таблица прав:**

| Действие | Переход | Кто может | Примечание |
|---|---|---|---|
| Создать | — → `created` | любой видящий исполнителя | `created_by_user_id = current_user` |
| Взять в работу | `created → in_progress` | исполнитель | |
| Отметить готово | `in_progress → awaiting_review` | исполнитель | `awaiting_review_at = now` |
| Принять работу | `awaiting_review → done` | **создатель** | |
| Вернуть в работу | `awaiting_review → in_progress` | **создатель** | Опциональный комментарий |
| Изменить дедлайн | — | **создатель** | Сразу меняет `deadline` |
| Предложить срок | — | исполнитель | Пишет в `proposed_deadline` |
| Принять предложение | — | создатель | Копирует proposed в deadline, очищает proposed |
| Отклонить предложение | — | создатель | Очищает proposed |
| Переназначить | `assignee_user_id` | **создатель** + admin | |
| Отменить | `is_active = false` | **создатель** + admin | Soft delete |

**Исполнитель никогда не может:**
- Перевести `awaiting_review → done` (только создатель принимает)
- Менять дедлайн напрямую (только предлагать)
- Переназначать
- Отменять

## Приоритет

**Вычисление:** в сервисе при выдаче списка, не хранится в БД.

```python
def compute_priority(task: Task, creator: Optional[User]) -> int:
    """
    Priority by task creator's role:
    3 = admin (org owner)
    2 = head (department head)
    1 = regular user (default for legacy tasks without created_by_user_id)
    """
    if creator is None:
        return 1
    if creator.is_admin:
        return 3
    if creator.is_head:
        return 2
    return 1
```

**Сортировка списка задач:**

```sql
ORDER BY
  CASE
    WHEN creator.is_admin THEN 3
    WHEN creator.is_head THEN 2
    ELSE 1
  END DESC,
  tasks.deadline ASC NULLS LAST,
  tasks.created_at DESC
```

Вторичный ключ — дедлайн (раньше = выше). Третий — created_at (новые выше при равном дедлайне).

**В UI:** бейдж приоритета в каждой строке таблицы:
- `3` — красный «Срочно от руководителя»
- `2` — жёлтый «От руководителя отдела»
- `1` — серый или без бейджа

## API

**Изменённые эндпоинты:**

`POST /api/v1/tasks` — создание
- Автоматически проставляет `created_by_user_id = current_user.id`
- Валидация: `current_user` должен видеть `assignee_user_id` через `get_visible_user_ids()`

`PATCH /api/v1/tasks/{id}` — обновление
- Права по таблице выше
- Нельзя менять `created_by_user_id` ни при каких условиях
- Смена статуса идёт через отдельные эндпоинты (ниже) — PATCH только для title/description/deadline/assignee

`GET /api/v1/tasks` — общий список
- Добавить сортировку по priority DESC, deadline ASC
- Добавить фильтры: `?status=`, `?project=` (поле добавится в п.11), `?created_by=me`, `?assigned_to=me`

**Новые эндпоинты:**

### Списки

`GET /api/v1/tasks/my` — задачи где `assignee_user_id = current_user`
- Возвращает все активные задачи с приоритетом и `creator` (вложенный user mini-object)
- Сортировка: priority DESC, deadline ASC

`GET /api/v1/tasks/created-by-me` — задачи где `created_by_user_id = current_user`
- То же + `assignee` (вложенный user mini-object)

### Переходы статусов

Вместо перегрузки PATCH — семантические эндпоинты:

`POST /api/v1/tasks/{id}/take` — взять в работу
- Проверка: `status == 'created'`, `current_user == assignee`
- Устанавливает `status = 'in_progress'`

`POST /api/v1/tasks/{id}/mark-done` — отметить готово
- Проверка: `status == 'in_progress'`, `current_user == assignee`
- Устанавливает `status = 'awaiting_review'`, `awaiting_review_at = now`

`POST /api/v1/tasks/{id}/accept` — принять работу
- Проверка: `status == 'awaiting_review'`, `current_user == creator`
- Устанавливает `status = 'done'`

`POST /api/v1/tasks/{id}/reject` — вернуть в работу
- Body: `{ comment?: string }` — опциональный комментарий с причиной возврата
- Проверка: `status == 'awaiting_review'`, `current_user == creator`
- Устанавливает `status = 'in_progress'`
- Комментарий **не сохраняется в БД** в рамках п.9 (только в server logs с correlation_id). Когда будет готов п.11 (чаты задач), reject endpoint начнёт автоматически постить этот комментарий как сообщение в чат задачи от имени создателя — исполнитель увидит причину возврата прямо в обсуждении задачи. UI принимает комментарий уже в п.9 — менять не придётся

### Дедлайн

`PATCH /api/v1/tasks/{id}/deadline` — создатель меняет напрямую
- Body: `{ deadline: string (iso) }`
- Проверка: `current_user == creator` (или admin)
- Очищает `proposed_deadline` если был

`POST /api/v1/tasks/{id}/deadline-proposal` — исполнитель предлагает
- Body: `{ proposed_deadline: string (iso) }`
- Проверка: `current_user == assignee`
- Записывает `proposed_deadline` + `proposed_deadline_by = current_user`

`POST /api/v1/tasks/{id}/deadline-proposal/accept` — создатель принимает
- Проверка: `current_user == creator`, `proposed_deadline != NULL`
- Копирует `proposed_deadline → deadline`, очищает proposed поля

`POST /api/v1/tasks/{id}/deadline-proposal/reject` — создатель отклоняет
- Проверка: `current_user == creator`, `proposed_deadline != NULL`
- Очищает proposed поля

## Сервисный слой (`TaskService`)

### Новые методы

```python
async def list_my_tasks(
    self, user_id: UUID, org_id: UUID,
    include_done: bool = False,
) -> List[Task]:
    """Tasks where current user is assignee. Sorted by priority+deadline."""

async def list_tasks_created_by(
    self, user_id: UUID, org_id: UUID,
    include_done: bool = False,
) -> List[Task]:
    """Tasks where current user is creator."""

async def take_task(self, task_id: UUID, user: User) -> Task:
    """Assignee takes task: created → in_progress."""

async def mark_done(self, task_id: UUID, user: User) -> Task:
    """Assignee marks task done: in_progress → awaiting_review."""

async def accept_task(self, task_id: UUID, user: User) -> Task:
    """Creator accepts: awaiting_review → done."""

async def reject_task(
    self, task_id: UUID, user: User, comment: Optional[str] = None,
) -> Task:
    """Creator rejects: awaiting_review → in_progress."""

async def set_deadline(
    self, task_id: UUID, user: User, deadline: datetime,
) -> Task:
    """Creator changes deadline directly."""

async def propose_deadline(
    self, task_id: UUID, user: User, proposed: datetime,
) -> Task:
    """Assignee proposes alternative deadline."""

async def accept_proposed_deadline(self, task_id: UUID, user: User) -> Task:
    """Creator accepts assignee's deadline proposal."""

async def reject_proposed_deadline(self, task_id: UUID, user: User) -> Task:
    """Creator rejects assignee's deadline proposal."""
```

### Общий приватный хелпер

```python
async def _load_task_with_creator(
    self, task_id: UUID, org_id: UUID,
) -> Tuple[Task, Optional[User]]:
    """Returns (task, creator_user) — creator may be None for legacy tasks."""
```

Используется каждым методом перехода статуса для проверки прав + для вычисления приоритета при возврате.

### Проверка прав

```python
def _check_assignee(self, task: Task, user: User):
    if task.assignee_user_id != user.id:
        raise PermissionError("Only assignee can perform this action")

def _check_creator(self, task: Task, user: User):
    if task.created_by_user_id is not None and task.created_by_user_id != user.id:
        if not user.is_admin:
            raise PermissionError("Only task creator can perform this action")
    # Legacy task without creator: только admin может
    elif task.created_by_user_id is None and not user.is_admin:
        raise PermissionError("Only admin can manage legacy tasks")
```

## Storage слой

### `TaskStorage` — новые методы

- `list_by_creator(user_id, org_id)` — задачи где creator = user
- `get_with_creator(task_id, org_id)` — JOIN на users для получения creator.is_admin/is_head

### Обновление existing методов

- `create_task(...)` принимает `created_by_user_id`
- `list_by_assignee(...)` — добавить JOIN на users для priority
- `update_task(...)` — принимает новые поля (awaiting_review_at, proposed_deadline, proposed_deadline_by)

## Обновление AI-tools

`src/engine/agents/tools/task_tool.py`:

- `task_create`: прокинуть `created_by_user_id = invoking_user.id` из agent context
- `task_update`: запретить менять created_by_user_id в любом случае
- `task_query`: добавить фильтры `filter_by=my|created`

## WebClient

### Backend (NestJS proxy)

В `packages/backend/src/task/task.service.ts` добавить методы и роуты:

- `listMy()`, `listCreatedByMe()`
- `take()`, `markDone()`, `accept()`, `reject()`
- `setDeadline()`, `proposeDeadline()`, `acceptProposal()`, `rejectProposal()`

В `rugpt.adapter.ts` — добавить соответствующие команды (`take_task`, `mark_task_done`, и т.д.) маппящиеся на новые engine-эндпоинты.

### Frontend (Next.js)

**Страница `/tasks` — табличный интерфейс с раскрытием строк:**

- Две вкладки: **«Мои задачи»** (default, `/api/tasks/my`) и **«Поставленные мной»** (`/api/tasks/created-by-me`)
- Таблица с колонками: Название · Статус · Приоритет · Дедлайн · (для «Мои») Создатель / (для «Поставленные») Исполнитель · Действия
- Бейдж приоритета: красный `3`/жёлтый `2`/серый `1`
- Бейдж статуса: цветной индикатор для каждого статуса, специальный цвет для `awaiting_review`

**Раскрытие строки (по клику на строку):**
- Разворачивается вниз дополнительный блок с описанием задачи (полный текст)
- В разворачивании — секция с предложенным сроком (если `proposed_deadline != NULL`): "Новое предложение: `<date>`" + кнопки «Принять» / «Отклонить» (только у создателя)
- В разворачивании — полная карточка участников (создатель + исполнитель с аватарами)

**Inline-кнопки в колонке «Действия» (зависят от статуса и роли):**

| Статус | Роль | Кнопки |
|---|---|---|
| `created` | assignee | «Взять в работу» |
| `in_progress` | assignee | «Отметить готово», «Предложить срок» |
| `awaiting_review` | creator | «Принять», «Вернуть в работу» |
| любой | creator | «Изменить срок», «Переназначить» (в overflow-меню) |

**Popover-инпуты для действий, требующих ввода:**
- «Изменить срок» / «Предложить срок» → date-picker popover прямо у кнопки
- «Вернуть в работу» → маленький popover с textarea для комментария (опционально) + кнопка Confirm
- «Переназначить» → dropdown с видимыми пользователями

Никаких отдельных модалок и страниц задачи — вся информация видна в таблице + expand-row, все действия по месту.

**Создание задачи** — отдельная модалка «+ Создать задачу» (по образцу `/departments` CRUD):
- Поля: название, описание, исполнитель (dropdown из видимых), дедлайн (date-picker)
- Кнопки: «Отмена», «Создать»

**Что НЕ меняется в UI пока:**
- Главный экран, сайдбар, роли — без изменений
- Уведомления о статусах — **НЕ** реализуются в этом пункте (ждут п.10 PM-агента)
- Чат задачи — ждёт п.11

## Обработка ошибок

Стандартный паттерн Engine:
- `PermissionError` → HTTP 403
- `NotFoundException` → HTTP 404
- `ValueError` (например, переход статуса не разрешён) → HTTP 400
- Неожиданные → HTTP 500 с логом в `logs/`

В WebClient — стандартный try/catch + `setError()` (по образцу существующих страниц).

## Тестирование

Engine имеет тестовый harness (pytest). Минимальный набор:

1. **test_task_priority_computation** — парит creator с разными ролями, проверяет priority 1/2/3
2. **test_create_task_sets_created_by** — создание задачи проставляет `created_by_user_id`
3. **test_assignee_cannot_set_done** — исполнитель не может перевести в `done`
4. **test_creator_accepts_awaiting_review** — создатель принимает работу
5. **test_creator_rejects_returns_to_in_progress** — отклонение возвращает в `in_progress`
6. **test_deadline_negotiation_flow** — исполнитель предлагает, создатель принимает
7. **test_legacy_task_no_creator** — задача без `created_by_user_id` имеет приоритет 1
8. **test_list_my_sorted_by_priority** — список отсортирован правильно

WebClient — тестов нет (известно), проверка ручная по чек-листу.

## Миграционный риск

**Легаси-задачи без creator:** все существующие задачи будут иметь `created_by_user_id = NULL`. Поведение:
- Приоритет = 1 (обычный)
- Только admin может принимать/отклонять/переназначать — обычный пользователь не сможет управлять такими задачами
- Либо можно в UI предложить руководителю «присвоить» старую задачу (установить creator = current_user) через спец-эндпоинт — но это уже полировка, не MVP

**Конфликт статусов:** существующие задачи могут находиться в старых статусах `created/in_progress/done/overdue`. Новый статус `awaiting_review` — опциональный, никого не ломает. Миграция не трогает статусы существующих задач.

## Метрики успеха

По завершении п.9 должно работать:
1. Руководитель ставит задачу сотруднику → задача появляется у сотрудника с нужным приоритетом
2. Сотрудник берёт в работу, делает, отмечает готово → руководителю нужно принять (через UI)
3. Если исполнителю нужно больше времени — предлагает новый срок, руководитель принимает/отклоняет
4. Руководитель видит отдельно «Мои задачи» (что он сам делает) и «Поставленные мной»
5. Приоритет визуально разделяет срочные от обычных

## Зависимости

**Входные (должны быть готовы):**
- ✅ п.1 базовая система задач
- ✅ п.8 отделы и видимость (для проверки `assignee_user_id` через `get_visible_user_ids`)

**Выходные (блокируется этим пунктом):**
- п.10 PM-агент и уведомления — нужен `created_by_user_id` и `awaiting_review` для логики уведомлений
- п.11 Проекты — для поля `created_by_user_id` при фильтрации

## Решённые вопросы по UI

- **Никаких модалок и страниц задачи.** Табличный интерфейс с раскрытием строк (expand-row для описания и proposed_deadline), inline-кнопки действий в строке, popover-инпуты для действий с вводом (date-picker, textarea комментария, dropdown переназначения).
- **Комментарий при reject** — опционально в теле запроса, не сохраняется в БД в п.9 (только server logs). Когда в п.11 появятся чаты задач, тот же endpoint начнёт автоматически постить комментарий как сообщение в чат задачи от имени создателя. UI принимает текст уже в п.9 и менять его не придётся.
- **Приоритет** — вычисляется на бэке. Фронт отображает готовое значение из `task.priority` поля в response.
