# Проекты и чаты задач/проектов — дизайн

> Дата: 2026-04-13
> Статус: утверждено к имплементации
> Пункт роадмапа: 11

## Контекст

Пункт 9 добавил владение задачами, статусы-переходы и приоритизацию. Сейчас задачи — плоский список, без группировки и без места для обсуждения конкретной задачи.

Этот пункт добавляет:
1. **Проекты** как отдельную сущность для группировки задач
2. **Чаты задач** — каждая задача имеет свой чат для обсуждения
3. **Чаты проектов** — каждый проект имеет свой чат для координации
4. **Audit trail** — история изменений задачи как отдельные события (а не системные сообщения в чате)
5. **Ссылки** на задачи и проекты (`!<id>`, `!!<id>`) — параллельная подсистема к существующим `@`/`@@` mentions

Этот пункт блокирует:
- **п.10** (PM-агент и уведомления) — нуждается в URL чата задачи для ссылок в нотификациях
- **п.12** (команды агентам) — нуждается в существующих чатах задач/проектов для tools `task_chat_post` / `project_chat_post`

## Цели

1. Пользователь может группировать задачи в проекты, с отдельной таблицей (id + name + описание) и возможностью дубликатов имён.
2. Каждая задача при создании автоматически получает свой чат. Участники = создатель + исполнитель (+ admin через глобальное правило).
3. Каждый проект при первой задаче получает свой чат. Участники = все, у кого есть задачи в этом проекте.
4. История изменений задачи хранится отдельно (`task_events`), **никаких системных сообщений в чатах**. Видна в UI раскрытия строки.
5. Ссылки `!<id>` (задача) / `!!<id>` (проект) в сообщениях становятся кликабельными pills, ведут на соответствующие чаты. `@`/`@@` продолжают работать без изменений.

## Объём

### Входит
- Миграция: `projects` таблица, `tasks.project_id`, `task_events` таблица, `chats.task_id`, `chats.project_id`, чистка legacy main/group типов
- Чистка мёртвого кода group-чатов (метод сервиса, роут, фронт-фильтр, common тип)
- Модели: `Project`, `TaskEvent`
- Сервисы: новые `ProjectService`, `TaskEventService`, `ReferenceService`, расширение `TaskService` + `ChatService`
- Обновление `engine_service.py` wiring: новые singletons инициализируются в порядке `ChatService → TaskEventService → ProjectService → TaskService`. Новые зависимости `TaskService` (`chat_service`, `task_event_service`, `project_service`) — `Optional[...] = None` в `__init__`, чтобы существующие тесты не сломались. Методы, требующие этих сервисов, падают через `assert ... is not None`.
- API: CRUD `/projects`, `GET /tasks/{id}/events`, task/project chat resolution endpoints, фильтр `GET /chats/my?type=`
- WebClient proxy: новые эндпоинты, адаптер команды
- Frontend: dropdown/create проекта в форме задачи, колонка проекта, история в expand-row, новые routes `/chat/task/[id]`, `/chat/project/[id]`, раздел "Проекты" в сайдбаре, рендер reference pills (`!<uuid>` / `!!<uuid>`), autocomplete для `!`/`!!` в `ChatInput`
- Тесты

### НЕ входит
- PM-агент и уведомления (п.10, делается после)
- Команды агентам и tools (п.12, делается после)
- Drag-n-drop задач между проектами
- Кросс-org чаты (остаётся как сейчас для system AI)
- Поиск/фильтрация внутри чата
- Права на уровне проекта (типа "только админ проекта может добавлять задачи")

## Модель данных

### Миграция `017_projects_and_task_chats.sql`

```sql
-- Migration 017: Projects, task chats, project chats, task events

-- ============================================
-- 1. Projects table
-- ============================================
CREATE TABLE IF NOT EXISTS projects (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    org_id UUID NOT NULL REFERENCES organizations(id),
    name TEXT NOT NULL,
    description TEXT,
    created_by_user_id UUID REFERENCES users(id),
    is_active BOOLEAN NOT NULL DEFAULT true,
    created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_projects_org ON projects(org_id) WHERE is_active = true;

COMMENT ON TABLE projects IS 'Project entity for grouping tasks. Names can be duplicated within an org.';

-- ============================================
-- 2. Link tasks to projects
-- ============================================
ALTER TABLE tasks
    ADD COLUMN project_id UUID REFERENCES projects(id) ON DELETE SET NULL;

CREATE INDEX IF NOT EXISTS idx_tasks_project ON tasks(project_id) WHERE is_active = true;

COMMENT ON COLUMN tasks.project_id IS 'Optional link to a project. ON DELETE SET NULL — project deletion keeps task.';

-- ============================================
-- 3. Task events (audit trail)
-- ============================================
CREATE TABLE IF NOT EXISTS task_events (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    task_id UUID NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    actor_user_id UUID REFERENCES users(id),
    event_type TEXT NOT NULL,
    payload JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_task_events_task ON task_events(task_id, created_at DESC);

COMMENT ON TABLE task_events IS 'Audit log of task changes. Rendered as "History" in UI. NOT the same as chat messages.';
COMMENT ON COLUMN task_events.event_type IS 'created|took|marked_done|accepted|rejected|deadline_set|deadline_proposed|deadline_proposal_accepted|deadline_proposal_rejected|assignee_changed|project_changed|cancelled|overdue';

-- NOTE: event_type strings 'cancelled' and 'rejected' are audit events, NOT task.status values.
-- tasks.status ∈ {created, in_progress, awaiting_review, done, overdue} (see models/task.py:VALID_STATUSES).
-- reject_task returns the task to status='in_progress' and writes a 'rejected' event.
-- deactivate (soft-delete) sets is_active=false and writes a 'cancelled' event; status is not changed.

-- ============================================
-- 4. Extend chats for task and project types
-- ============================================
-- chats.type is VARCHAR(20) without CHECK constraint — new values 'task'/'project'
-- can be added without DDL. We just clean up legacy 'main'/'group' values
-- and add the new FK columns.

-- Convert legacy types to direct (preserves any messages, no data loss)
UPDATE chats SET type = 'direct' WHERE type IN ('main', 'group');

-- New default for clarity
ALTER TABLE chats ALTER COLUMN type SET DEFAULT 'direct';

-- New FK columns for task and project association.
-- Soft-delete policy: NO CASCADE. Deletion of a task or project is handled
-- at the service layer (archive_task_chat / archive_project_chat set is_active=false),
-- so chats are preserved as archive and never physically removed by FK propagation.
ALTER TABLE chats
    ADD COLUMN task_id UUID REFERENCES tasks(id),
    ADD COLUMN project_id UUID REFERENCES projects(id);

CREATE INDEX IF NOT EXISTS idx_chats_task ON chats(task_id) WHERE task_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_chats_project ON chats(project_id) WHERE project_id IS NOT NULL;

COMMENT ON COLUMN chats.type IS 'direct | task | project';
COMMENT ON COLUMN chats.task_id IS 'For type=task: references the task this chat belongs to';
COMMENT ON COLUMN chats.project_id IS 'For type=project: references the project this chat belongs to';
```

**Обратная совместимость:**
- Все новые поля либо nullable, либо имеют default
- Существующие задачи получают `project_id = NULL`
- Существующие direct-чаты не меняются. Legacy `'main'` и `'group'` (если есть) конвертируются в `'direct'` через UPDATE в миграции 017
- Используем существующий `chats.type` (varchar 20, без CHECK) для новых типов `'task'` / `'project'` — никаких новых колонок типа

**Инвариант multi-tenancy.** `org_id` на `tasks` и `projects` — источник правды для row-level изоляции, а не FK-денормализация. Хранится явно (а не выводится через `creator.org_id`), потому что:
- все горячие запросы формата «список X моей org» индексируются напрямую по `org_id`;
- row-level guard тривиален: `WHERE org_id = :actor_org`;
- устойчивость к soft-delete создателя/исполнителя — задача не теряет org;
- юзер в rugpt привязан к одной org навсегда, риска дрейфа нет.

Все сервисные операции фильтруют и проверяют по `org_id == user.org_id`. Для задач проекта действует инвариант `task.org_id == project.org_id`, обеспечиваемый `TaskService.create()` при выборе `project_id`.

### Модели

**`src/engine/models/project.py`:**

```python
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional
from uuid import UUID, uuid4


@dataclass
class Project:
    id: UUID = field(default_factory=uuid4)
    org_id: UUID = field(default_factory=uuid4)
    name: str = ""
    description: Optional[str] = None
    created_by_user_id: Optional[UUID] = None
    is_active: bool = True
    created_at: datetime = field(default_factory=datetime.utcnow)
    updated_at: datetime = field(default_factory=datetime.utcnow)

    def to_dict(self) -> dict:
        return {
            "id": str(self.id),
            "org_id": str(self.org_id),
            "name": self.name,
            "description": self.description,
            "created_by_user_id": str(self.created_by_user_id) if self.created_by_user_id else None,
            "is_active": self.is_active,
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
        }
```

**`src/engine/models/task_event.py`:**

```python
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional
from uuid import UUID, uuid4


@dataclass
class TaskEvent:
    id: UUID = field(default_factory=uuid4)
    task_id: UUID = field(default_factory=uuid4)
    actor_user_id: Optional[UUID] = None
    event_type: str = ""
    payload: dict = field(default_factory=dict)
    created_at: datetime = field(default_factory=datetime.utcnow)

    def to_dict(self) -> dict:
        return {
            "id": str(self.id),
            "task_id": str(self.task_id),
            "actor_user_id": str(self.actor_user_id) if self.actor_user_id else None,
            "event_type": self.event_type,
            "payload": self.payload,
            "created_at": self.created_at.isoformat(),
        }
```

**Расширение `Task` (`src/engine/models/task.py`):**

Добавить поле:
```python
    project_id: Optional[UUID] = None
```

И включить в `to_dict()`:
```python
    "project_id": str(self.project_id) if self.project_id else None,
```

**Расширение `Chat` (`src/engine/models/chat.py`):**

Заменить enum `ChatType` — убрать `GROUP`, добавить `TASK` и `PROJECT`:
```python
class ChatType(str, Enum):
    DIRECT = "direct"
    TASK = "task"
    PROJECT = "project"
```

`ChatType.GROUP` **удаляется** — это мёртвый код, никем не используется (см. секцию "Чистка group-чатов" ниже).

В датакласс Chat добавить:
```python
    task_id: Optional[UUID] = None
    project_id: Optional[UUID] = None
```

И в `to_dict()` / `from_dict()`.

## Чистка group-чатов (мёртвый код)

Group chats — scaffold-нутый функционал, никогда не использовавшийся в продакшене:
- `chat_service.create_group_chat()` метод существует, но никем не вызывается
- Роут `POST /api/v1/chats/group` не используется ни WebClient'ом, ни AI-tools
- В Sidebar.tsx есть фильтр `chats.filter(c => c.isGroup)` — но во всех call-site `isGroup` хардкодится в `false`
- В БД сейчас 0 group чатов и 1 legacy `main` (тоже не используется)

Чистка в составе п.11:

**Engine:**
- Удалить метод `ChatService.create_group_chat(...)` из `src/engine/services/chat_service.py`
- Удалить роут `POST /api/v1/chats/group` (`@router.post("/group", ...)`) из `src/engine/routes/chats.py`
- Удалить класс `CreateGroupChatRequest` из того же файла если он только для group использовался
- Удалить `ChatType.GROUP` из enum в `src/engine/models/chat.py`
- В `from_dict` / `_row_to_chat` убедиться что legacy `'main'` / `'group'` строки из БД конвертируются (или после миграции 017 их там не будет — миграция UPDATE их в `'direct'`)

**WebClient:**
- Удалить `isGroup` поле из `packages/common/src/types/chat.ts` (если есть)
- Удалить `isGroup` из `SidebarProps.chats[]` тип в `Sidebar.tsx`
- Удалить блок `groupChats = chats.filter(c => c.isGroup).slice(0, 2)` и его рендеринг
- Удалить `isGroup: false` из всех мест где Sidebar получает chats: `MainChat.tsx`, `settings/page.tsx`, `files/page.tsx`, `mentions/page.tsx`, `tasks/page.tsx`, `chat/[username]/page.tsx`

**Backend (NestJS):**
- Удалить любые методы / команды связанные с group chats в `chat.service.ts` и адаптере (если есть)

После чистки `ChatType` имеет ровно три значения: `direct`, `task`, `project`. Никаких legacy.

## Сервисы

### ProjectService (новый)

`src/engine/services/project_service.py`:

```python
class ProjectService:
    def __init__(self, storage: ProjectStorage, chat_service: "ChatService"):
        self.storage = storage
        self.chat_service = chat_service

    def _check_can_create(self, user: User):
        """Only is_head or is_admin can create projects (spec decision)."""
        if not (user.is_admin or user.is_head):
            raise PermissionError("Only department head or admin can create projects")

    def _check_same_org(self, project: Project, user: User):
        """Multi-tenancy guard. Raises ValueError (mapped to 404 at route layer) to avoid leaking existence.
        We use ValueError instead of a custom NotFoundError to stay consistent with TaskService/ChatService."""
        if project.org_id != user.org_id:
            raise ValueError(f"Project {project.id} not found")

    async def create(
        self, name: str, user: User, description: Optional[str] = None,
    ) -> Project:
        self._check_can_create(user)
        if not name.strip():
            raise ValueError("Project name is required")
        project = Project(
            org_id=user.org_id,
            name=name.strip(),
            description=description,
            created_by_user_id=user.id,
        )
        return await self.storage.create(project)

    async def list_by_org(self, org_id: UUID, include_archived: bool = False) -> List[Project]:
        return await self.storage.list_by_org(org_id, include_archived)

    async def get(self, project_id: UUID, user: User) -> Optional[Project]:
        project = await self.storage.get_by_id(project_id)
        if project is None:
            return None
        if project.org_id != user.org_id:
            return None  # hide cross-org projects as non-existent
        return project

    async def update(
        self, project_id: UUID, user: User,
        name: Optional[str] = None, description: Optional[str] = None,
    ) -> Project:
        self._check_can_create(user)  # same permissions: head/admin can edit
        project = await self.storage.get_by_id(project_id)
        if not project:
            raise ValueError(f"Project {project_id} not found")
        self._check_same_org(project, user)
        if name is not None:
            project.name = name.strip()
        if description is not None:
            project.description = description
        return await self.storage.update(project)

    async def delete(self, project_id: UUID, user: User) -> bool:
        """Soft-delete: is_active=false on project AND archive its chat.
        Tasks keep project_id (FK stays, они просто ссылаются на неактивный проект).
        Чтобы вывести задачи из-под архивного проекта, нужно явно PATCH project_id=null."""
        self._check_can_create(user)
        project = await self.storage.get_by_id(project_id)
        if not project:
            return False
        self._check_same_org(project, user)
        await self.storage.deactivate(project_id)
        await self.chat_service.archive_project_chat(project_id)
        return True
```

### ProjectStorage (новый)

`src/engine/storage/project_storage.py` — стандартный CRUD по образцу `department_storage.py`. Методы: `create`, `get_by_id`, `list_by_org(org_id, include_archived)`, `update`, `deactivate`.

### TaskService — расширения

**1. `create()` принимает `project_id`:**

```python
async def create(
    self,
    org_id: UUID,
    title: str,
    assignee_user_id: UUID,
    description: Optional[str] = None,
    deadline: Optional[datetime] = None,
    created_by_user_id: Optional[UUID] = None,
    project_id: Optional[UUID] = None,  # NEW
) -> Task:
    ...
    # Multi-tenancy guard: если указан project_id, он должен принадлежать той же org.
    if project_id is not None:
        project = await self.project_service.storage.get_by_id(project_id)
        if not project or project.org_id != org_id or not project.is_active:
            raise ValueError(f"Project {project_id} is not available in this organization")

    task = Task(
        ...,
        project_id=project_id,
    )
    created = await self.storage.create(task)

    # 1. Auto-create task chat
    await self.chat_service.create_task_chat(
        task_id=created.id,
        org_id=org_id,
        creator_id=created_by_user_id or assignee_user_id,
        assignee_id=assignee_user_id,
    )

    # 2. Link to project chat if project_id set (lazy-creates chat on first task)
    if project_id:
        await self.chat_service.ensure_project_chat_membership(
            project_id=project_id,
            org_id=org_id,
            user_ids=[u for u in [created_by_user_id, assignee_user_id] if u],
        )

    # 3. Write event
    await self.task_event_service.record(
        task_id=created.id,
        actor_user_id=created_by_user_id,
        event_type="created",
        payload={"title": title, "assignee": str(assignee_user_id)},
    )

    # 4. Existing in-app notification stays
    await self.notification_service.create(...)

    return created
```

**2. Методы перехода записывают в `task_events`.**

Пример для `take_task`:
```python
async def take_task(self, task_id: UUID, user: User) -> Task:
    task = await self.storage.get_by_id(task_id)
    ...
    task.status = "in_progress"
    updated = await self.storage.update(task)

    await self.task_event_service.record(
        task_id=task_id,
        actor_user_id=user.id,
        event_type="took",
        payload={"from_status": "created", "to_status": "in_progress"},
    )

    return updated
```

Аналогично для `mark_done`, `accept_task`, `reject_task` (payload включает comment), `set_deadline`, `propose_deadline`, `accept_proposed_deadline`, `reject_proposed_deadline`, `check_overdue`.

**3. `update()` может менять `project_id` — пересчитываем membership в project chat.**

При смене `project_id` задачи:
- Старая ассоциация (если была) остаётся в старом чате проекта — **не чистим** (решено: оставлять наблюдателей, историю не теряем)
- Новый проект (если есть) получает участников задачи в `ensure_project_chat_membership`
- Событие `project_changed` в `task_events`

**4. `update()` меняет `assignee_user_id` — добавляем в чат задачи.**

```python
if assignee_user_id and assignee_user_id != task.assignee_user_id:
    await self.chat_service.add_task_chat_participant(task.id, assignee_user_id)
    await self.task_event_service.record(
        task_id=task_id,
        actor_user_id=user.id,
        event_type="assignee_changed",
        payload={"from": str(task.assignee_user_id), "to": str(assignee_user_id)},
    )
```

Старый исполнитель остаётся в чате как наблюдатель (тот же принцип что с проектами). В сайдбаре он не увидит чат, если задача станет неактивной, но в базе останется — на случай если его снова добавят.

**5. `deactivate(task_id, user)` — soft-delete задачи архивирует её чат и, при необходимости, чат проекта.**

```python
async def deactivate(self, task_id: UUID, user: User) -> bool:
    task = await self.storage.get_by_id(task_id)
    if not task or task.org_id != user.org_id:
        return False
    await self.storage.deactivate(task_id)

    # Archive task chat
    await self.chat_service.archive_task_chat(task_id)

    # Event
    await self.task_event_service.record(
        task_id=task_id, actor_user_id=user.id,
        event_type="cancelled", payload={},
    )

    # If this was the last active task in the project → archive project chat
    if task.project_id:
        remaining = await self.storage.count_active_in_project(task.project_id)
        if remaining == 0:
            await self.chat_service.archive_project_chat(task.project_id)

    return True
```

`TaskStorage.count_active_in_project(project_id) -> int` — новый метод: `SELECT COUNT(*) FROM tasks WHERE project_id = $1 AND is_active = true`.

**6. DI:** TaskService получает новые зависимости `chat_service`, `task_event_service`, `project_service` (опциональные для тестов).

### TaskEventService (новый)

`src/engine/services/task_event_service.py`:

```python
class TaskEventService:
    def __init__(self, storage: TaskEventStorage):
        self.storage = storage

    async def record(
        self,
        task_id: UUID,
        actor_user_id: Optional[UUID],
        event_type: str,
        payload: dict,
    ) -> TaskEvent:
        event = TaskEvent(
            task_id=task_id,
            actor_user_id=actor_user_id,
            event_type=event_type,
            payload=payload,
        )
        return await self.storage.create(event)

    async def list_for_task(self, task_id: UUID, limit: int = 100) -> List[TaskEvent]:
        return await self.storage.list_by_task(task_id, limit)
```

### TaskEventStorage (новый)

`src/engine/storage/task_event_storage.py` — стандартные `create`, `list_by_task(task_id, limit)`.

### ChatService — расширения

**1. `create_task_chat(task_id, org_id, creator_id, assignee_id) -> Chat`:**

```python
async def create_task_chat(
    self, task_id: UUID, org_id: UUID,
    creator_id: UUID, assignee_id: UUID,
) -> Chat:
    participants = list({creator_id, assignee_id})  # dedupe
    chat = Chat(
        org_id=org_id,
        type=ChatType.TASK,
        task_id=task_id,
        participants=participants,
        created_by=creator_id,
    )
    return await self.chat_storage.create(chat)
```

**2. `get_task_chat(task_id) -> Optional[Chat]`:**

```python
async def get_task_chat(self, task_id: UUID) -> Optional[Chat]:
    return await self.chat_storage.get_by_task_id(task_id)
```

**3. `add_task_chat_participant(task_id, user_id)` — тонкая обёртка:**

Делегирует существующий `ChatService.add_participant(chat_id, user_id)` — не дублирует SQL-логику. Lookup чата по `task_id`, затем существующий вызов:

```python
async def add_task_chat_participant(self, task_id: UUID, user_id: UUID) -> bool:
    chat = await self.chat_storage.get_by_task_id(task_id)
    if not chat:
        return False
    return await self.add_participant(chat.id, user_id)
```

Инвариант дедупа держит `ChatStorage.add_participant` (не добавляет дубль).

**4. `ensure_project_chat_membership(project_id, org_id, user_ids) -> Chat` — создаёт чат при первой задаче:**

Одна объединённая операция: lookup → если чата нет, создаём с участниками → если есть, реактивируем (если был архивирован) и мерджим participants.

```python
async def ensure_project_chat_membership(
    self, project_id: UUID, org_id: UUID, user_ids: List[UUID],
) -> Chat:
    # Multi-tenancy guard на callsite: user_ids уже должны быть той же org, что и project.
    # TaskService это гарантирует (creator/assignee живут в org задачи).
    existing = await self.chat_storage.get_by_project_id(project_id)
    if existing:
        # Реактивация, если был архивирован (напр., после удаления последней задачи,
        # а потом снова появилась задача — см. race condition в "Решённых вопросах").
        changed = False
        if not existing.is_active:
            existing.is_active = True
            changed = True
        new_participants = list(set(existing.participants) | set(user_ids))
        if new_participants != existing.participants:
            existing.participants = new_participants
            changed = True
        if changed:
            await self.chat_storage.update(existing)
        return existing
    # Чата ещё нет — создаём при первой задаче проекта.
    chat = Chat(
        org_id=org_id,
        type=ChatType.PROJECT,
        project_id=project_id,
        participants=list(set(user_ids)),
    )
    return await self.chat_storage.create(chat)
```

Callsite в `TaskService.create()`: `await chat_service.ensure_project_chat_membership(project_id, org_id, [creator_id, assignee_id])` — при первой задаче проекта чат создаётся сразу же, вместе с задачей. Отдельного метода `ensure_project_chat` без участников не заводим.

**6. `archive_task_chat(task_id)`:**

Soft-delete задачи → деактивация её чата. Participants и сообщения сохраняются.

```python
async def archive_task_chat(self, task_id: UUID) -> None:
    chat = await self.chat_storage.get_by_task_id(task_id)
    if chat and chat.is_active:
        chat.is_active = False
        await self.chat_storage.update(chat)
```

**7. `archive_project_chat(project_id)`:**

Вызывается при soft-delete проекта ИЛИ при деактивации последней активной задачи проекта.

```python
async def archive_project_chat(self, project_id: UUID) -> None:
    chat = await self.chat_storage.get_by_project_id(project_id)
    if chat and chat.is_active:
        chat.is_active = False
        await self.chat_storage.update(chat)
```

**8. Повторная активация project-чата.** Логика реактивации встроена в `ensure_project_chat_membership` (см. метод 4 выше) — при обращении к архивированному чату он автоматически реактивируется и мерджит новых участников. Это закрывает race condition с `archive_project_chat` (см. «Решённые вопросы» №7).

### ChatStorage — расширения

Новые методы:
- `get_by_task_id(task_id) -> Optional[Chat]`
- `get_by_project_id(project_id) -> Optional[Chat]`
- `update(chat)` — если ещё нет, нужен для обновления participants/is_active

Существующий `_row_to_chat` расширяется для `task_id`, `project_id`.

### MentionService — НЕ трогаем

Существующая система `@username` / `@@rolename` остаётся **без изменений**: паттерн, `Mention` dataclass, `MentionType` enum, резолверы юзеров и AI-ролей — всё как есть. Никаких `TASK`/`PROJECT` значений в `MentionType` не добавляем, поле `user_id` в `Mention` не переиспользуем.

Причина: поля `Mention.user_id: UUID` и `Mention.username: str` семантически принадлежат юзеру. Сериализация в response и фронтовые pills `@username` / `@@rolename` завязаны на этот контракт. Переиспользование под task/project UUID — silent type confusion.

### ReferenceService (новый)

Параллельная подсистема для ссылок на задачи и проекты в сообщениях. Использует префиксы `!` и `!!` (симметрично `@`/`@@` по модели «одинарный — основная сущность, двойной — агрегат»):

- `!<uuid>` — ссылка на задачу
- `!!<uuid>` — ссылка на проект

Пользователь никогда не печатает UUID руками: UI инсертит ссылку через autocomplete (см. раздел Frontend). Чистая rendering-контракт подсистема: **в БД ничего структурно не хранится**, резолв идёт на каждое чтение сообщения.

**Файл:** `src/engine/services/reference_service.py`

**Паттерн:**

```python
import re
from uuid import UUID
from typing import List, Tuple

REFERENCE_PATTERN = re.compile(
    r'!!([0-9a-fA-F-]{36})'   # !! project UUID
    r'|!([0-9a-fA-F-]{36})'   # !  task UUID
)

class ReferenceService:
    def __init__(
        self,
        task_storage: "TaskStorage",
        project_storage: "ProjectStorage",
    ):
        self.task_storage = task_storage
        self.project_storage = project_storage

    def parse(self, content: str) -> List[Tuple[str, UUID, int]]:
        """Returns list of (ref_type, uuid, position). ref_type ∈ {'task', 'project'}."""
        refs = []
        for m in REFERENCE_PATTERN.finditer(content):
            try:
                if m.group(1):  # project
                    refs.append(("project", UUID(m.group(1)), m.start()))
                elif m.group(2):  # task
                    refs.append(("task", UUID(m.group(2)), m.start()))
            except ValueError:
                continue  # non-canonical UUID — silently skip
        return refs
```

Порядок альтернатив важен: `!!(...)` первым, `!(...)` вторым. Python regex матчит альтернативы слева направо, так что `!!abc...` попадёт в `!!(...)` до того, как `!(...)` мог бы сжевать один `!` и оставить второй болтаться.

UUID капчуется как `[0-9a-fA-F-]{36}` — гибкая регулярка, пост-валидация через `UUID(...)` отбрасывает мусор (например, ровно 36 дефисов).

**Batch-резолв для ленты сообщений:**

```python
async def resolve_batch(
    self, contents: List[Tuple[UUID, str]], actor: "User",
) -> Dict[UUID, List[dict]]:
    """
    Input: list of (message_id, content).
    Output: {message_id: [{type, id, title, accessible, position}, ...]}.
    One batch SQL per entity kind. Per-viewer accessible is computed in memory.
    """
    # 1. Collect all unique task/project UUIDs across all messages.
    task_ids: set[UUID] = set()
    project_ids: set[UUID] = set()
    per_msg: Dict[UUID, List[Tuple[str, UUID, int]]] = {}
    for msg_id, content in contents:
        parsed = self.parse(content)
        per_msg[msg_id] = parsed
        for ref_type, ref_id, _ in parsed:
            if ref_type == "task":
                task_ids.add(ref_id)
            else:
                project_ids.add(ref_id)

    # 2. Batch-fetch by PK (microseconds on indexed id = ANY($1::uuid[])).
    tasks_by_id = await self.task_storage.get_many_by_ids(list(task_ids)) if task_ids else {}
    projects_by_id = await self.project_storage.get_many_by_ids(list(project_ids)) if project_ids else {}

    # 3. Resolve per message, computing accessible per-viewer.
    result: Dict[UUID, List[dict]] = {}
    for msg_id, parsed in per_msg.items():
        resolved = []
        for ref_type, ref_id, pos in parsed:
            if ref_type == "task":
                task = tasks_by_id.get(ref_id)
                if task is None:
                    resolved.append({"type": "task", "id": str(ref_id), "title": None, "accessible": False, "position": pos})
                    continue
                accessible = self._can_see_task(task, actor)
                resolved.append({
                    "type": "task",
                    "id": str(ref_id),
                    "title": task.title if accessible else None,
                    "accessible": accessible,
                    "position": pos,
                })
            else:  # project
                project = projects_by_id.get(ref_id)
                if project is None:
                    resolved.append({"type": "project", "id": str(ref_id), "title": None, "accessible": False, "position": pos})
                    continue
                accessible = (project.org_id == actor.org_id and project.is_active)
                resolved.append({
                    "type": "project",
                    "id": str(ref_id),
                    "title": project.name if accessible else None,
                    "accessible": accessible,
                    "position": pos,
                })
        result[msg_id] = resolved
    return result

@staticmethod
def _can_see_task(task: "Task", actor: "User") -> bool:
    """Strict visibility rule. Does NOT consider chat.participants (see resolved question #3)."""
    return (
        actor.id == task.created_by_user_id
        or actor.id == task.assignee_user_id
        or (actor.is_admin and actor.org_id == task.org_id)
    )
```

Новые методы в storage:
- `TaskStorage.get_many_by_ids(ids: List[UUID]) -> Dict[UUID, Task]` — `WHERE id = ANY($1::uuid[])`
- `ProjectStorage.get_many_by_ids(ids: List[UUID]) -> Dict[UUID, Project]` — то же

**Интеграция в MessageService:** при сборке response `GET /api/v1/chats/{id}/messages` для каждой страницы сообщений вызвать `ReferenceService.resolve_batch([(m.id, m.content) for m in messages], actor)` и прицепить результат к каждому сообщению как поле `references`. Резолв — per-viewer, на каждое чтение; stale-данных быть не может (названия и accessible всегда актуальные).

**Кросс-org guard:** `_can_see_task` и project visibility проверяют `actor.org_id == entity.org_id`. Даже если автор сообщения случайно вставил UUID чужой org — получатель видит `accessible=false, title=null`, утечки нет.

## API

### Projects

`GET /api/v1/projects?include_archived=false` — список проектов организации

`POST /api/v1/projects` — создать (head/admin only)
Body: `{ name: string, description?: string }`

`GET /api/v1/projects/{id}` — один проект

`PATCH /api/v1/projects/{id}` — изменить (head/admin only)
Body: `{ name?: string, description?: string }`

`DELETE /api/v1/projects/{id}` — soft delete (head/admin only)

### Task chat resolution

`GET /api/v1/tasks/{id}/chat` — возвращает `chat` объект задачи. Чистый GET, без side-effects. 404 если:
- задача не существует в org вызывающего (проверка `task.org_id == user.org_id`);
- чата нет (не должно случаться для задач, созданных после деплоя п.11; старые задачи в dev-БД игнорируем — backfill не делаем);
- юзер не проходит `can_see_task`.

Response: `{ id, participants, ... }` — стандартный чат-объект.

### Project chat resolution

`GET /api/v1/projects/{id}/chat` — возвращает `chat` объект проекта. Проверки: `project.org_id == user.org_id`. 404 иначе.

### Task events

`GET /api/v1/tasks/{id}/events?limit=100` — история изменений задачи

Response: `[{ id, task_id, actor_user_id, event_type, payload, created_at }, ...]`

### Tasks — фильтр по проекту

`GET /api/v1/tasks/my?project_id=<uuid>` — добавить query param
`GET /api/v1/tasks/created-by-me?project_id=<uuid>` — то же

### Task create/update — принимают project_id

`POST /api/v1/tasks` body расширяется: `project_id?: string`
`PATCH /api/v1/tasks/{id}` body расширяется: `project_id?: string | null`

### Chats — фильтр по типу

Task и project chats — обычные чаты, работают через существующие `/api/v1/chats/*`. Для сайдбара добавляется server-side фильтр:

`GET /api/v1/chats/my?type=direct|task|project` — опциональный query-параметр. Без параметра возвращаются все типы (обратно-совместимо).

Реализация:
- `ChatStorage.list_by_user(user_id, active_only, chat_type=None)` — новый опциональный параметр `chat_type`. При `None` — без фильтра. При заданном значении — `AND type = $N` в WHERE.
- Индекс `idx_chats_type` уже существует (`migrations/001_initial.sql:90`), отдельного нового индекса не требуется.
- Route: `@router.get("/my")` принимает `type: Optional[str] = Query(None)`, прокидывает в сервис, оттуда в сторадж.
- NestJS proxy: `/api/chats/my` пробрасывает query-param `type` без изменений.

### Message response — новое поле `references`

Существующий response сообщения дополняется параллельным полем:

```json
{
  "id": "msg-...",
  "chat_id": "...",
  "sender_id": "...",
  "content": "Посмотри !550e8400-... по проекту !!660e9500-...",
  "mentions": [...],
  "references": [
    {"type": "task",    "id": "550e8400-...", "title": "Обновить договор", "accessible": true,  "position": 9},
    {"type": "project", "id": "660e9500-...", "title": "Договоры 2026",    "accessible": true,  "position": 35}
  ],
  ...
}
```

Поле `mentions` не изменяется — `@`/`@@` идут как раньше. Поле `references` заполняется `ReferenceService.resolve_batch(...)` per-viewer на каждом чтении ленты. Отсутствующий или недоступный reference возвращается с `"accessible": false, "title": null`.

## WebClient (NestJS proxy)

### Engine adapter команды (добавить)

- `get_projects`, `get_project`, `create_project`, `update_project`, `delete_project`
- `get_task_chat`, `get_project_chat`
- `get_task_events`

### ProjectService (новый модуль NestJS)

`packages/backend/src/project/project.module.ts`, `project.controller.ts`, `project.service.ts` — по образцу `department/` модуля.

Роуты: `/api/projects` CRUD, head/admin check в сервисе.

### TaskService extensions

Методы: `getTaskChat(taskId)`, `getTaskEvents(taskId)`.

Контроллер: `GET /api/tasks/:id/chat`, `GET /api/tasks/:id/events`.

### Project controller для chat

`GET /api/projects/:id/chat` — прокидывает `get_project_chat`.

## Frontend

### Common types

`packages/common/src/types/project.ts`:
```typescript
export interface Project {
  id: string;
  orgId: string;
  name: string;
  description?: string | null;
  createdByUserId?: string | null;
  isActive: boolean;
  createdAt: string;
  updatedAt: string;
}
```

`packages/common/src/types/task.ts` — добавить:
```typescript
  projectId?: string | null;
```

`packages/common/src/types/task-event.ts` (новый):
```typescript
export interface TaskEvent {
  id: string;
  taskId: string;
  actorUserId?: string | null;
  eventType: string;
  payload: Record<string, unknown>;
  createdAt: string;
}
```

`packages/common/src/types/chat.ts` — обновить существующий `ChatType` enum (убрать `GROUP`, добавить `TASK`, `PROJECT`), добавить поля `taskId?: string | null`, `projectId?: string | null` в интерфейс `Chat`. Интерфейс `ChatMention` и enum `MentionType` **не трогаем** — они для `@`/`@@`, а task/project ссылки идут через новый `MessageReference`.

Добавить рядом:

```typescript
export enum ReferenceType {
  TASK = 'task',
  PROJECT = 'project',
}

export interface MessageReference {
  type: ReferenceType;
  id: string;
  title: string | null;   // null when accessible=false
  accessible: boolean;
  position: number;
}
```

В интерфейс `ChatMessage` добавить:
```typescript
  references?: MessageReference[];
```

`packages/common/src/index.ts` — реэкспорт новых типов.

### `/tasks` page

1. **Форма создания задачи** — добавить поле «Проект»:
   - Dropdown существующих проектов (запрос `GET /api/projects`)
   - Кнопка «+ Создать новый» → открывает второй mini-modal с полями `name`, `description`, создаёт через `POST /api/projects`, после создания выбирает его в текущей задаче
   - Кнопка видна только если `user.isHead || user.isAdmin`
   - Для обычных юзеров — только выбор из существующих

2. **Таблица задач** — добавить колонку «Проект» (показывает `project?.name` или `—`)

3. **Фильтр по проекту** (опционально, можно в следующей итерации)

4. **Expand-row** — добавить секцию «История» с загрузкой `GET /api/tasks/:id/events`:
   - Список событий в хронологическом порядке
   - Каждое событие как строка: `{icon} {event_type_localized} — {actor_name} — {created_at}` + payload деталей (например "с 20.04 на 25.04" для deadline_set)
   - Локализация event_type через константный словарь на фронте

5. **Кнопка «Открыть чат»** в expand-row строки задачи — `router.push(\`/chat/task/\${task.id}\`)`

### Новые routes

**`packages/frontend/src/app/chat/task/[id]/page.tsx`** — chat задачи
- `[id]` = task UUID
- Компонент загружает `GET /api/tasks/:id/chat` для получения `chat.id`
- Далее использует **существующий** chat component — не дублирует код
- Pinned card сверху: карточка задачи (title, status, deadline, assignee, creator, кнопки управления для creator)
- Всё остальное — обычный чат

**`packages/frontend/src/app/chat/project/[id]/page.tsx`** — chat проекта
- `[id]` = project UUID
- Загружает `GET /api/projects/:id/chat`
- Pinned card сверху: карточка проекта (name, description, счётчик активных задач, кнопка «Перейти к задачам проекта»)
- Обычный чат

### Sidebar

Расширить `Sidebar.tsx` — добавить две новые секции под существующей группировкой "Чаты":

**«Задачи»** (активные task-чаты, видимые текущему юзеру):
- Загружается через `GET /api/chats/my?type=task` (server-side фильтр, см. секцию API)
- **Фильтр:** `chat.is_active = true` AND `task.is_active = true` AND `task.status != 'done'` AND `can_see_task(user, task) = true` (строго: creator / current assignee / admin той же org).
- **Важно:** фильтр использует `can_see_task`, а **не** только participant membership. Бывший исполнитель остаётся в `chat.participants` на уровне БД (аудит-след, историю не теряем), но в сайдбаре его старый чат не показывается, потому что он выпал из `can_see_task`. Это решение зафиксировано в «Решённых вопросах» №3 и №6.
- Реализация фильтра: серверный `ChatStorage.list_by_user(..., chat_type='task')` возвращает все task-чаты, где юзер в participants; далее `ChatService.list_user_chats_for_sidebar` делает JOIN/IN-lookup по `tasks` и отбрасывает те, которые не проходят `can_see_task` и/или `task.status='done'` / `task.is_active=false`. Альтернатива — один SQL с JOIN, возвращающий только проходящие фильтр; выбор реализации — за имплементатором, API одинаковый.
- Каждая строка — название задачи + бейдж статуса
- Клик → `/chat/task/{taskId}`
- Сворачиваемый раздел (localStorage по паттерну отделов)

**Примечание про статусы:** `cancelled` и `rejected` **не являются значениями** `task.status` (см. `models/task.py:VALID_STATUSES = {created, in_progress, awaiting_review, done, overdue}`). Cancellation — это `task.is_active=false` через `TaskService.deactivate`. Reject возвращает задачу в `status='in_progress'` через `TaskService.reject_task`. Поэтому в фильтре сайдбара упоминается только `status != 'done'` — этого достаточно.

**«Проекты»** (активные project-чаты где пользователь участник):
- `GET /api/chats/my?type=project`
- **Фильтр:** `chat.is_active = true` AND `project.is_active = true` AND `user ∈ chat.participants`
- Видимость проектов в сайдбаре — по участникам чата проекта (т.е. если у юзера есть хоть одна задача в проекте, он там), а не по `ProjectService.can_see`. Это разумно, потому что «увидеть проект в сайдбаре» означает «у меня есть активная работа в нём».
- Каждая строка — название проекта + счётчик непрочитанных
- Клик → `/chat/project/{projectId}`
- Сворачиваемый

### References: инсерт и рендеринг

**Инсерт в `ChatInput.tsx` — по образцу существующего `detectMention`.**

Текущий `ChatInput.tsx` использует plain textarea + autocomplete popup: функция `detectMention(text, cursorPos)` парсит префикс перед курсором (`@@` / `@`), возвращает `{mode, query, startIndex}`, UI открывает dropdown со списком юзеров и по Enter/клику вставляет выбранный username. Никакого contenteditable / rich editor — и в п.11 мы это **не меняем**.

Для `!` / `!!` добавляется симметричная функция:

```typescript
interface ReferenceState {
  mode: '!' | '!!' | null;
  query: string;
  startIndex: number;
  results: Array<{ id: string; title: string }>;
  selectedIndex: number;
}

function detectReference(text: string, cursorPos: number): { mode: '!' | '!!'; query: string; startIndex: number } | null {
  const before = text.slice(0, cursorPos);
  // !! project first (greedy)
  const matchDouble = before.match(/(?:^|\s)(!!)(\S*)$/);
  if (matchDouble) {
    const start = cursorPos - matchDouble[0].length + (matchDouble[0].startsWith(' ') ? 1 : 0);
    return { mode: '!!', query: matchDouble[2], startIndex: start };
  }
  // ! task
  const matchSingle = before.match(/(?:^|\s)(!)(\S*)$/);
  if (matchSingle) {
    const start = cursorPos - matchSingle[0].length + (matchSingle[0].startsWith(' ') ? 1 : 0);
    return { mode: '!', query: matchSingle[2], startIndex: start };
  }
  return null;
}
```

Popup запрашивает список задач/проектов через новые эндпоинты (`GET /api/tasks/my?search=...`, `GET /api/projects?search=...` — если поиска ещё нет, для MVP можно отдать первые 20 записей и клиентский фильтр по `query`). По выбору инсертит в textarea сырой токен `!<uuid>` или `!!<uuid>`.

**UX-компромисс MVP:** после инсерта автор видит в черновике сырой `!<uuid>` (а не название). Это уродливо, но: а) совпадает со стилем существующего кода (plain textarea), б) после отправки сообщения pill в ленте рендерится с названием через `references` массив, в) миграция `ChatInput` на contenteditable (TipTap/Lexical) с inline-pill в черновике — работа вне scope п.11, делается после Alpha.

**Рендеринг в ленте сообщений.**

В компоненте, который отрисовывает `ChatMessage.content` (вероятно существующий `MessageContent.tsx` или inline в `MainChat.tsx`), добавить второй проход по `references`:

```typescript
function renderMessageContent(content: string, references: MessageReference[] = []): ReactNode[] {
  if (references.length === 0) return [content];
  // Sort by position ascending, walk content left-to-right, emit text chunks and pills.
  const sorted = [...references].sort((a, b) => a.position - b.position);
  const out: ReactNode[] = [];
  let cursor = 0;
  for (const ref of sorted) {
    const tokenLen = (ref.type === 'task' ? 1 : 2) + 36; // '!' or '!!' + UUID-36
    if (ref.position > cursor) out.push(content.slice(cursor, ref.position));
    if (ref.type === ReferenceType.TASK) {
      out.push(<TaskPill key={ref.position} id={ref.id} title={ref.title} accessible={ref.accessible} />);
    } else {
      out.push(<ProjectPill key={ref.position} id={ref.id} title={ref.title} accessible={ref.accessible} />);
    }
    cursor = ref.position + tokenLen;
  }
  if (cursor < content.length) out.push(content.slice(cursor));
  return out;
}
```

Компоненты `<TaskPill>` и `<ProjectPill>`:
- `accessible=true`: кликабельный элемент с подсветкой и названием, клик → `router.push('/chat/task/<id>')` или `/chat/project/<id>`.
- `accessible=false`: серый disabled span с title-атрибутом «Нет доступа» (вместо названия — обобщённое `[задача]` / `[проект]`).

Существующий `@username` / `@@rolename` рендеринг через `mentions` массив — без изменений, работает параллельно.

## Frontend: что НЕ делаем

- Отдельная страница `/projects` со списком всех проектов с UI управления — можно сделать **позже**, MVP работает через /tasks (выбор в форме задачи) + сайдбар раздел «Проекты». Для Alpha этого достаточно.
- Drag-n-drop задач в проекты
- Множественный выбор/массовые действия
- **Страница «Архив задач»** — UI-раздел со всеми закрытыми/неактивными задачами, где юзер был creator или assignee. Нужен чтобы дать доступ к истории после того, как чат исчез из сайдбара. Оставляем на после-Alpha, сейчас достаточно прямого URL `/chat/task/{id}` (доступен если в таблице `/tasks` включён фильтр «показать архив»).

## Тестирование

### Engine (pytest)

`tests/test_projects.py`:
- `test_create_requires_head_or_admin` — обычный юзер не может
- `test_create_head_can` — is_head может
- `test_create_admin_can` — is_admin может
- `test_list_by_org_filters_archived` — include_archived работает
- `test_update_changes_name` — rename
- `test_delete_soft_delete` — is_active = false
- `test_duplicate_names_allowed` — два проекта с одинаковым именем в одной org

`tests/test_task_events.py`:
- `test_create_task_records_event` — после create появляется event type=created
- `test_take_task_records_event` — took event
- `test_mark_done_records_event` — marked_done
- `test_accept_records_event` — accepted
- `test_reject_records_event_with_comment` — rejected, payload.comment = "X"
- `test_deadline_set_records_event` — deadline_set, payload.old/new
- `test_list_for_task_sorted_desc` — list_for_task возвращает по убыванию

`tests/test_task_chat_auto_create.py`:
- `test_create_task_creates_chat` — после task.create есть chat с type=task и task_id
- `test_chat_participants_include_creator_and_assignee`
- `test_reassign_task_adds_new_assignee_to_chat`
- `test_reassign_task_keeps_old_assignee_in_participants` — монотонный рост для аудита
- `test_get_task_chat_returns_correct_chat`
- `test_archive_task_chat_preserves_participants` — soft-delete не чистит participants

`tests/test_project_chat_integration.py`:
- `test_task_with_project_ensures_project_chat` — chat проекта создаётся при первой задаче (сразу, не лениво)
- `test_second_task_in_same_project_reuses_chat` — не дубликат, participants расширяются
- `test_project_change_keeps_old_chat_membership` — старый project chat не чистится
- `test_ensure_project_chat_membership_dedups` — повторный вызов с существующим участником не дублирует UUID
- `test_deactivate_last_task_archives_project_chat` — см. TaskService.deactivate
- `test_reactivate_project_chat_on_new_task_after_archive` — добавление задачи в проект с архивированным чатом поднимает `is_active` обратно
- `test_count_active_in_project_excludes_inactive` — инвариант `WHERE is_active = true`

`tests/test_multitenancy_guards.py`:
- `test_create_task_with_foreign_project_id_rejected` — юзер org A не может создать задачу с `project_id` из org B (ValueError)
- `test_get_project_cross_org_returns_none` — `ProjectService.get` скрывает чужие org как 404
- `test_list_by_user_does_not_leak_cross_org_chats`

`tests/test_references.py` (новый — бывший test_mentions_task_project):
- `test_parse_task_reference` — `!<uuid>` распарсен
- `test_parse_project_reference` — `!!<uuid>`
- `test_parse_order_double_before_single` — `!!abc...` не жуётся одинарным `!`
- `test_parse_invalid_uuid_silently_skipped` — 36 дефисов / битый UUID не ломает парсер
- `test_resolve_batch_task_visible` — creator/assignee/admin получают accessible=true и title
- `test_resolve_batch_task_not_visible` — не-creator/assignee/admin получает accessible=false, title=null
- `test_resolve_batch_task_cross_org_not_accessible` — UUID задачи чужой org → accessible=false, title=null
- `test_resolve_batch_project_same_org` — accessible=true
- `test_resolve_batch_project_inactive` — archived project → accessible=false
- `test_resolve_batch_project_cross_org_not_accessible`
- `test_resolve_batch_mixed_task_and_project` — сообщение с обоими типами, один batch-проход
- `test_resolve_batch_deleted_entity` — UUID уже удалённой сущности → accessible=false, title=null

`tests/test_migration_017.py`:
- `test_legacy_main_chats_converted_to_direct` — после миграции 017 любой legacy `'main'` type стал `'direct'`, данные не потеряны
- `test_legacy_group_chats_converted_to_direct` — то же для `'group'`
- `test_chats_type_default_is_direct_after_migration`
- `test_chats_task_id_project_id_columns_added`
- `test_tasks_project_id_column_added_nullable`

`tests/test_sidebar_filter.py`:
- `test_sidebar_task_filter_excludes_done` — задача со `status='done'` не попадает
- `test_sidebar_task_filter_excludes_inactive_task` — `task.is_active=false` (cancelled) скрывает
- `test_sidebar_task_filter_excludes_non_visible_user` — старый assignee, который остался в participants, но выпал из `can_see_task`, не видит чат
- `test_sidebar_task_filter_includes_admin_same_org` — admin видит все активные таски своей org
- `test_sidebar_project_filter_excludes_inactive_project`

### WebClient

Тестов нет (проект не имеет frontend test harness). Ручная проверка по чеклисту:
1. Head/admin создаёт проект через UI → появляется в списке
2. Обычный юзер видит проекты в dropdown, но не видит кнопку "+ Создать"
3. Задача с проектом → в чате проекта появляется пользователь
4. Expand-row задачи → видна история изменений
5. Клик по задаче → открывает `/chat/task/{id}`, видна pinned card
6. Клик по проекту в сайдбаре → открывает `/chat/project/{id}`
7. Второй юзер вставляет ссылку `!<uuid>` в сообщение через autocomplete → рендерится как кликабельный pill с названием задачи

## Метрики успеха

По завершении п.11:
1. `is_head` и `is_admin` могут создавать проекты (проверить на обычном юзере — отказ)
2. В форме задачи есть выбор проекта + кнопка создать (для привилегированных)
3. При создании задачи автоматически появляется её чат в сайдбаре
4. Клик по задаче из `/tasks` → открывается её чат
5. Несколько задач в одном проекте → все связаны одним чатом проекта
6. Перенос задачи в другой проект → старый чат проекта сохраняется, новый пополняется
7. Удаление проекта админом → проект soft-delete (`is_active=false`), задачи остаются с `project_id` указывающим на неактивный проект, чат проекта архивируется (`is_active=false`)
8. История задачи в UI раскрытия — читается как линейный лог
9. `!<uuid>` (задача) и `!!<uuid>` (проект) в сообщении — кликабельные pills с названием, серые pills для недоступных сущностей
10. Ничего старого не сломано: существующие direct-чаты работают (legacy `main`/`group` конвертированы в `direct` миграцией 017)

## Зависимости

- ✅ п.9 (task ownership) — task_events цепляются на существующие методы перехода
- ✅ п.8 (departments) — `is_head` флаг используется для проверки прав создания проекта (не для видимости задач — видимость строгая, см. Решённые вопросы №3)
- ✅ п.1 (базовые задачи)

## Решённые вопросы

1. **Удаление задачи/проекта — soft-delete всюду.** CASCADE убран из миграции (`chats.task_id`, `chats.project_id` — FK без ON DELETE). При `TaskService.deactivate` вызывается `ChatService.archive_task_chat(task_id)`. При `ProjectService.delete` — `archive_project_chat(project_id)`. Чаты, participants и сообщения сохраняются навсегда как архив, просто `is_active=false`.

2. **Удаление последней задачи проекта.** `TaskService.deactivate` после удаления проверяет `TaskStorage.count_active_in_project(project_id)`. Если 0 — автоматически архивирует чат проекта через `archive_project_chat`.

3. **Видимость `!<task>` — строгая, расходится с `chat.participants`.** Правило:
   ```
   can_see_task(X, T) = (X.id == T.created_by_user_id)
                     OR (X.id == T.assignee_user_id)   -- ТЕКУЩИЙ assignee
                     OR (X.is_admin AND X.org_id == T.org_id)
   ```
   `chat.participants` **шире**, чем `can_see_task`: при смене assignee старый исполнитель остаётся в participants (для аудита и восстановления контекста), но выпадает из `can_see_task`. Это осознанное расхождение, не баг. Следствия:
   - В sidebar-фильтре используем **`can_see_task`**, а не participant membership — старый assignee не видит чат задачи после переназначения.
   - Reference pill `!<uuid>` в любом сообщении резолвится по `can_see_task`: серый, если выпал. Консистентно с сайдбаром.
   - Прямой URL `/chat/task/{id}` доступен только тем, кто проходит `can_see_task` — старый assignee, даже если раньше работал с задачей, через URL её не откроет (проверка в роуте).
   - Запись в participants остаётся навсегда, чисто для истории: админ может через SQL увидеть «Боб был тут как исполнитель».
   
   Head/department-based видимость НЕ вводим — риск слишком широкого доступа.

4. **Видимость `!!<project>` — org-wide.** Все юзеры своей org видят все активные проекты своей org. Проекты чужих org скрыты (`accessible: false`). Sidebar-секция «Проекты» дополнительно фильтруется по participant membership (видим только проекты, где у юзера есть активная работа).

5. **`GET /tasks/{id}/chat` — чистый GET без side-effects.** Старые задачи в dev-БД (до деплоя п.11) без чатов — игнорируем, backfill не делаем. 404 приемлемо. Роут проверяет `can_see_task` перед отдачей.

6. **Монотонный рост `participants` — намеренный.** Physical participants не чистятся никогда (аудит-след). UI-видимость даётся через `can_see_task` в сайдбаре и через visibility check в роуте чата. Это НЕ даёт бесконечного роста сайдбара: старый исполнитель не видит старые задачи, как только он больше не проходит `can_see_task`. Сохраняется возможность восстановления: админ может вернуть юзера в `can_see_task` через переназначение — и чат сразу снова появится в его сайдбаре.

7. **Race condition при архивации project chat.** Сценарий: `TaskService.deactivate` считает `count_active_in_project=0` и вызывает `archive_project_chat`; параллельно другой юзер создаёт новую задачу в том же проекте, вызывая `ensure_project_chat_membership`. Race решён **идемпотентностью `ensure_project_chat_membership`**: если чат существует и архивирован, он реактивируется (`is_active=true`) и мерджит participants. Даже если архивация и создание происходят в обратном порядке, конечное состояние корректно: активный чат с нужными участниками. Транзакция/lock не нужны.

## Открытые вопросы (остаются на имплементацию)

1. **Scoping project creation** — "head может создавать проект" — любой head или только head своего отдела? Для Alpha считаем что любой head может создавать проект в своей org. Если нужна более точная проверка (head только своего отдела), добавить позже.

2. **Renaming проекта** — изменение имени затрагивает чат проекта (у него нет имени в таблице, но в UI он отображается по `project.name`). Не требует миграций, просто UI перечитывает. OK.

3. **Audit trail в `task_events.payload`** — формат payload для каждого event_type не жёстко стандартизован. Общее правило: ключи в snake_case, значения — строки/числа/булевы, UUID как строки. Нет строгой схемы для Alpha.
