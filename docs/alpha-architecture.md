# Alpha Architecture — технический документ

> Архитектура новых модулей для Alpha-релиза (апрель 2026).
> Описывает изменения относительно текущей системы.

---

## Обзор новых модулей

Alpha добавляет пять новых подсистем поверх текущей архитектуры:

```
┌─────────────────────────────────────────────────────────┐
│                    WebClient (UI)                        │
│                                                          │
│  [Задачи]  [Опросы]  [Отчёты]  [Файлы]  [Колокольчик] │
│      │         │         │         │          │          │
└──────┼─────────┼─────────┼─────────┼──────────┼──────────┘
       │         │         │         │          │
       ▼         ▼         ▼         ▼          ▼
┌──────────────────────────────────────────────────────────┐
│                    Engine (FastAPI)                        │
│                                                           │
│  Новые модули:                                            │
│  ├── TaskService          (управление задачами)           │
│  ├── TaskPollService      (утренние опросы)               │
│  ├── TaskReportService    (вечерние отчёты)               │
│  ├── InAppNotificationService (колокольчик)               │
│  └── FileService          (загрузка файлов, RAG)          │
│                                                           │
│  Изменения в существующих:                                │
│  ├── SchedulerService     (+morning_poll, +evening_report)│
│  ├── AgentExecutor        (+task tools)                   │
│  ├── ToolRegistry         (+task_create/query/update)     │
│  └── ChatService          (создание задач из @@ mention)  │
│                                                           │
│  PostgreSQL:                     StorageAdapter:            │
│  ├── tasks                (new)  ├── LocalStorageAdapter   │
│  ├── task_polls           (new)  │   └── /var/lib/rugpt/   │
│  ├── task_reports         (new)  │       uploads/          │
│  ├── in_app_notifications (new)  └── S3StorageAdapter      │
│  └── user_files           (new)      └── (SeaweedFS потом)│
└───────────────────────────────────────────────────────────┘
```

---

## 1. Task Management (Управление задачами)

### 1.1 Модель данных

#### Таблица `tasks`

```sql
CREATE TABLE tasks (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    org_id UUID NOT NULL REFERENCES organizations(id),
    title VARCHAR(500) NOT NULL,
    description TEXT,
    status VARCHAR(20) NOT NULL DEFAULT 'created',
        -- created | in_progress | done | overdue
    assignee_user_id UUID NOT NULL REFERENCES users(id),
        -- сотрудник-исполнитель
    creator_user_id UUID REFERENCES users(id),
        -- руководитель, создавший задачу (NULL если создана ролью из чата)
    role_id UUID REFERENCES roles(id),
        -- роль сотрудника, которая ведёт задачу
    deadline TIMESTAMP WITH TIME ZONE,
        -- nullable, задача может быть без дедлайна
    source_chat_id UUID REFERENCES chats(id),
        -- чат, из которого создана задача (если через @@)
    source_message_id UUID REFERENCES messages(id),
        -- конкретное сообщение
    is_active BOOLEAN DEFAULT true,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

CREATE INDEX idx_tasks_org ON tasks(org_id);
CREATE INDEX idx_tasks_assignee ON tasks(assignee_user_id, status)
    WHERE is_active = true;
CREATE INDEX idx_tasks_creator ON tasks(creator_user_id);
CREATE INDEX idx_tasks_role ON tasks(role_id);
CREATE INDEX idx_tasks_active_status ON tasks(status)
    WHERE is_active = true;
```

#### Dataclass

```python
@dataclass
class Task:
    id: UUID
    org_id: UUID
    title: str
    description: Optional[str]
    status: str                     # "created" | "in_progress" | "done" | "overdue"
    assignee_user_id: UUID
    creator_user_id: Optional[UUID]
    role_id: Optional[UUID]
    deadline: Optional[datetime]
    source_chat_id: Optional[UUID]
    source_message_id: Optional[UUID]
    is_active: bool
    created_at: datetime
    updated_at: datetime
```

### 1.2 Storage

**Файл:** `src/engine/storage/task_storage.py`

```python
class TaskStorage(BaseStorage):
    async def create(task: Task) -> Task
    async def get_by_id(task_id: UUID) -> Task?
    async def list_by_assignee(user_id: UUID, active_only: bool) -> List[Task]
    async def list_by_creator(user_id: UUID, active_only: bool) -> List[Task]
    async def list_by_org(org_id: UUID, active_only: bool) -> List[Task]
    async def list_by_role(role_id: UUID, active_only: bool) -> List[Task]
    async def list_active_with_assignees(org_id: UUID) -> List[Task]
        # Для scheduler: все активные задачи, сгруппированные по assignee
    async def update(task: Task) -> Task
    async def update_status(task_id: UUID, status: str) -> bool
    async def deactivate(task_id: UUID) -> bool
```

### 1.3 Service

**Файл:** `src/engine/services/task_service.py`

```python
class TaskService:
    def __init__(self, task_storage, user_storage, role_storage)

    # CRUD
    async def create_task(org_id, title, assignee_user_id,
                          creator_user_id?, description?, deadline?,
                          source_chat_id?, source_message_id?) -> Task
        # При создании:
        # 1. Проверяет что assignee существует и активен
        # 2. Находит role_id по assignee (user.role_id)
        # 3. Создаёт Task
        # 4. Создаёт in_app_notification для assignee (новая задача)

    async def get_task(task_id) -> Task?
    async def list_tasks(org_id, user_id, is_admin: bool) -> List[Task]
        # Если admin: все задачи org
        # Если не admin: только свои (assignee_user_id = user_id)

    async def update_task(task_id, title?, description?, deadline?, status?) -> Task?
    async def deactivate_task(task_id) -> bool

    # Для scheduler
    async def get_active_tasks_grouped_by_assignee(org_id) -> Dict[UUID, List[Task]]
    async def check_overdue() -> List[Task]
        # Находит задачи с deadline < NOW() и status != done/overdue
        # Обновляет статус на overdue
```

### 1.4 API Routes

**Файл:** `src/engine/routes/tasks.py`

```
GET    /api/v1/tasks                 — список задач (admin: все, user: свои)
POST   /api/v1/tasks                 — создать задачу
GET    /api/v1/tasks/{task_id}       — получить задачу
PATCH  /api/v1/tasks/{task_id}       — обновить задачу
DELETE /api/v1/tasks/{task_id}       — деактивировать задачу
GET    /api/v1/tasks/user/{user_id}  — задачи конкретного сотрудника (admin only)
```

### 1.5 Agent Tools (для создания задач из чата)

**Файл:** `src/engine/agents/tools/task_tool.py`

По аналогии с `calendar_tool.py` — factory pattern с замыканием на TaskService.

```python
def create_task_tools(task_service: TaskService):
    """Возвращает (task_create_tool, task_query_tool, task_update_tool)"""

    # task_create: создать задачу
    # Input: title, assignee_username, description?, deadline?
    # Роль вызывает когда руководитель пишет "@@oleg проверь договор до пятницы"
    # Tool находит user по username, создаёт Task

    # task_query: запросить задачи сотрудника
    # Input: assignee_username?, status?
    # Используется для утреннего опроса и контекста

    # task_update: обновить статус задачи
    # Input: task_id, status, comment?
    # Используется при обработке ответов на опрос
```

Регистрация в ToolRegistry:
```python
task_create_tool, task_query_tool, task_update_tool = create_task_tools(task_service)
registry.register("task_create", task_create_tool)
registry.register("task_query", task_query_tool)
registry.register("task_update", task_update_tool)
```

### 1.6 Flow создания задачи из чата

```
1. Руководитель: "@@oleg проверь договор до пятницы"
2. MentionService парсит @@ → находит user oleg
3. ChatService → AIService → AgentExecutor
4. Роль oleg'а (lawyer) получает сообщение + tools [task_create, ...]
5. Роль вызывает tool task_create:
   {title: "Проверить договор", assignee: "oleg", deadline: "пятница"}
6. TaskService.create_task() → Task в БД
7. InAppNotificationService → уведомление oleg'у (колокольчик)
8. Роль отвечает: "Задача создана: Проверить договор, дедлайн: 7 марта"
```

---

## 2. Task Polls (Утренние опросы)

### 2.1 Модель данных

#### Таблица `task_polls`

```sql
CREATE TABLE task_polls (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    org_id UUID NOT NULL REFERENCES organizations(id),
    assignee_user_id UUID NOT NULL REFERENCES users(id),
    poll_date DATE NOT NULL,
    status VARCHAR(20) NOT NULL DEFAULT 'pending',
        -- pending | completed | expired
    responses JSONB DEFAULT '[]',
        -- [{task_id, new_status, comment}]
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    completed_at TIMESTAMP WITH TIME ZONE,
    expires_at TIMESTAMP WITH TIME ZONE,

    UNIQUE(assignee_user_id, poll_date)
        -- один опрос на сотрудника в день
);

CREATE INDEX idx_task_polls_assignee ON task_polls(assignee_user_id, poll_date DESC);
CREATE INDEX idx_task_polls_status ON task_polls(status, poll_date)
    WHERE status = 'pending';
```

#### Dataclass

```python
@dataclass
class TaskPollResponse:
    task_id: UUID
    new_status: str             # "created" | "in_progress" | "done"
    comment: Optional[str]

@dataclass
class TaskPoll:
    id: UUID
    org_id: UUID
    assignee_user_id: UUID
    poll_date: date
    status: str                 # "pending" | "completed" | "expired"
    responses: List[TaskPollResponse]
    created_at: datetime
    completed_at: Optional[datetime]
    expires_at: Optional[datetime]
```

### 2.2 Storage

**Файл:** `src/engine/storage/task_poll_storage.py`

```python
class TaskPollStorage(BaseStorage):
    async def create(poll: TaskPoll) -> TaskPoll
    async def get_by_id(poll_id: UUID) -> TaskPoll?
    async def get_today_poll(user_id: UUID) -> TaskPoll?
    async def list_by_user(user_id: UUID, limit: int) -> List[TaskPoll]
    async def list_pending(org_id: UUID) -> List[TaskPoll]
    async def submit_responses(poll_id: UUID, responses: List[dict]) -> TaskPoll
    async def expire_overdue(before: datetime) -> int
        # Массовое истечение неотвеченных опросов
```

### 2.3 Service

**Файл:** `src/engine/services/task_poll_service.py`

```python
class TaskPollService:
    def __init__(self, poll_storage, task_storage, task_service,
                 notification_service)

    async def generate_morning_polls(org_id) -> List[TaskPoll]
        # 1. Получить все активные задачи, сгруппированные по assignee
        # 2. Для каждого сотрудника с задачами:
        #    a. Создать TaskPoll (poll_date=today)
        #    b. Создать in_app_notification (тип: poll)
        # 3. Вернуть список созданных опросов

    async def submit_poll(poll_id, responses: List[TaskPollResponse]) -> TaskPoll
        # 1. Сохранить ответы в task_polls.responses
        # 2. Обновить статусы задач через task_service.update_task()
        # 3. Отметить poll как completed
        # 4. Вернуть обновлённый опрос

    async def get_pending_poll(user_id) -> TaskPoll?
    async def expire_old_polls() -> int
```

### 2.4 API Routes

**Файл:** `src/engine/routes/task_polls.py`

```
GET    /api/v1/tasks/polls/current    — текущий опрос пользователя (pending)
POST   /api/v1/tasks/polls/{id}/submit — отправить ответы на опрос
GET    /api/v1/tasks/polls/history     — история опросов пользователя
```

### 2.5 Flow утреннего опроса

```
[Cron: 09:00]
     │
     ▼
SchedulerService._morning_poll_job()
     │
     ├── TaskPollService.generate_morning_polls(org_id)
     │       │
     │       ├── Для каждого сотрудника с активными задачами:
     │       │   ├── Создать TaskPoll
     │       │   └── Создать InAppNotification:
     │       │       type: "poll"
     │       │       title: "Утренний опрос по задачам"
     │       │       reference_type: "task_poll"
     │       │       reference_id: poll.id
     │       │
     │       └── Сотрудники, у которых нет активных задач → пропустить
     │
     ▼
Сотрудник видит колокольчик → кликает → попап с опросом
     │
     ▼
POST /api/v1/tasks/polls/{id}/submit
{
  "responses": [
    {"task_id": "uuid1", "new_status": "in_progress", "comment": "В процессе проверки"},
    {"task_id": "uuid2", "new_status": "done", "comment": "Готово, отправил на почту"}
  ]
}
     │
     ▼
TaskPollService.submit_poll()
     ├── Обновить статусы задач
     ├── Сохранить ответы
     └── Отметить poll = completed
```

---

## 3. Task Reports (Вечерние отчёты)

### 3.1 Модель данных

#### Таблица `task_reports`

```sql
CREATE TABLE task_reports (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    org_id UUID NOT NULL REFERENCES organizations(id),
    generated_for_user_id UUID NOT NULL REFERENCES users(id),
        -- руководитель, которому предназначен отчёт
    generated_by_role_id UUID REFERENCES roles(id),
        -- роль, сгенерировавшая отчёт (nullable для системных)
    report_date DATE NOT NULL,
    content TEXT NOT NULL,
        -- AI-сгенерированный текст отчёта
    task_summaries JSONB DEFAULT '[]',
        -- структурированные данные:
        -- [{task_id, title, assignee_user_id, assignee_name,
        --   old_status, new_status, employee_comment, poll_completed: bool}]
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

CREATE INDEX idx_task_reports_user ON task_reports(generated_for_user_id, report_date DESC);
CREATE INDEX idx_task_reports_org ON task_reports(org_id, report_date DESC);
```

#### Dataclass

```python
@dataclass
class TaskSummary:
    task_id: UUID
    title: str
    assignee_user_id: UUID
    assignee_name: str
    old_status: str
    new_status: str
    employee_comment: Optional[str]
    poll_completed: bool

@dataclass
class TaskReport:
    id: UUID
    org_id: UUID
    generated_for_user_id: UUID
    generated_by_role_id: Optional[UUID]
    report_date: date
    content: str
    task_summaries: List[TaskSummary]
    created_at: datetime
```

### 3.2 Storage

**Файл:** `src/engine/storage/task_report_storage.py`

```python
class TaskReportStorage(BaseStorage):
    async def create(report: TaskReport) -> TaskReport
    async def get_by_id(report_id: UUID) -> TaskReport?
    async def list_by_user(user_id: UUID, limit: int) -> List[TaskReport]
    async def get_by_date(user_id: UUID, report_date: date) -> TaskReport?
```

### 3.3 Service

**Файл:** `src/engine/services/task_report_service.py`

```python
class TaskReportService:
    def __init__(self, report_storage, task_storage, poll_storage,
                 user_storage, agent_executor, role_storage,
                 notification_service)

    async def generate_evening_reports(org_id) -> List[TaskReport]
        # 1. Найти всех admin-пользователей org (руководителей)
        # 2. Собрать task_summaries:
        #    - Все активные задачи org
        #    - Для каждой: сегодняшний poll response (если был)
        #    - old_status (на начало дня) vs new_status (текущий)
        # 3. Для каждого руководителя:
        #    a. Сформировать контекст для AI:
        #       "Вот данные по задачам сотрудников за день: ..."
        #    b. AgentExecutor.execute() → AI-анализ, сводка
        #    c. Сохранить TaskReport (content + task_summaries)
        #    d. InAppNotification для руководителя (тип: report)
        # 4. Вернуть список отчётов

    async def get_report(report_id) -> TaskReport?
    async def list_reports(user_id, limit=30) -> List[TaskReport]
```

### 3.4 API Routes

**Файл:** `src/engine/routes/task_reports.py`

```
GET    /api/v1/tasks/reports           — список отчётов (для руководителя)
GET    /api/v1/tasks/reports/{id}      — конкретный отчёт
GET    /api/v1/tasks/reports/date/{date} — отчёт за дату
```

### 3.5 Flow вечернего отчёта

```
[Cron: 18:00]
     │
     ▼
SchedulerService._evening_report_job()
     │
     ├── TaskReportService.generate_evening_reports(org_id)
     │       │
     │       ├── Сбор данных:
     │       │   tasks + today's polls + user names
     │       │
     │       ├── Формирование task_summaries:
     │       │   [{task: "Проверить договор", assignee: "Олег",
     │       │     old_status: "created", new_status: "in_progress",
     │       │     comment: "В процессе", poll_completed: true},
     │       │    {task: "Подготовить отчёт", assignee: "Мария",
     │       │     old_status: "in_progress", new_status: "in_progress",
     │       │     comment: null, poll_completed: false}]
     │       │
     │       ├── AI-генерация отчёта:
     │       │   AgentExecutor → "Олег начал работу над договором.
     │       │   Мария не прошла опрос — рекомендую уточнить статус.
     │       │   Просроченных задач нет."
     │       │
     │       └── Сохранение + InAppNotification (колокольчик)
     │
     ▼
Руководитель видит колокольчик → Раздел "Отчёты"
```

---

## 4. In-App Notifications (Колокольчик)

### 4.1 Модель данных

#### Таблица `in_app_notifications`

```sql
CREATE TABLE in_app_notifications (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID NOT NULL REFERENCES users(id),
    org_id UUID NOT NULL REFERENCES organizations(id),
    type VARCHAR(30) NOT NULL,
        -- new_task | poll | report | mention | task_status_change | system
    title VARCHAR(500) NOT NULL,
    content TEXT,
    reference_type VARCHAR(30),
        -- task | task_poll | task_report | message | null
    reference_id UUID,
        -- ID связанной сущности
    is_read BOOLEAN DEFAULT false,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

CREATE INDEX idx_in_app_notif_user ON in_app_notifications(user_id, is_read, created_at DESC);
CREATE INDEX idx_in_app_notif_unread ON in_app_notifications(user_id)
    WHERE is_read = false;
```

#### Dataclass

```python
@dataclass
class InAppNotification:
    id: UUID
    user_id: UUID
    org_id: UUID
    type: str
    title: str
    content: Optional[str]
    reference_type: Optional[str]
    reference_id: Optional[UUID]
    is_read: bool
    created_at: datetime
```

### 4.2 Storage

**Файл:** `src/engine/storage/in_app_notification_storage.py`

```python
class InAppNotificationStorage(BaseStorage):
    async def create(notification: InAppNotification) -> InAppNotification
    async def list_by_user(user_id: UUID, limit: int, unread_only: bool) -> List[InAppNotification]
    async def count_unread(user_id: UUID) -> int
    async def mark_read(notification_id: UUID) -> bool
    async def mark_all_read(user_id: UUID) -> int
```

### 4.3 Service

**Файл:** `src/engine/services/in_app_notification_service.py`

```python
class InAppNotificationService:
    def __init__(self, storage)

    async def notify(user_id, org_id, type, title, content?,
                     reference_type?, reference_id?) -> InAppNotification
    async def get_unread(user_id, limit=50) -> List[InAppNotification]
    async def get_unread_count(user_id) -> int
    async def mark_read(notification_id) -> bool
    async def mark_all_read(user_id) -> int
```

### 4.4 API Routes

**Файл:** `src/engine/routes/in_app_notifications.py`

```
GET    /api/v1/notifications/in-app           — список (query: unread_only, limit)
GET    /api/v1/notifications/in-app/count     — количество непрочитанных
POST   /api/v1/notifications/in-app/{id}/read — пометить прочитанным
POST   /api/v1/notifications/in-app/read-all  — пометить все прочитанными
```

### 4.5 Типы уведомлений

| type | Когда создаётся | reference_type | Получатель |
|------|----------------|----------------|------------|
| `new_task` | Создана задача для сотрудника | task | assignee |
| `poll` | Утренний опрос сгенерирован | task_poll | assignee |
| `report` | Вечерний отчёт готов | task_report | admin |
| `mention` | @упоминание в чате | message | mentioned user |
| `task_status_change` | Статус задачи изменился | task | creator |
| `system` | Системное событие | null | user |

---

## 5. File Management (Загрузка файлов для RAG)

### 5.1 Архитектура хранилища — StorageAdapter

Файлы хранятся через абстракцию `StorageAdapter` (паттерн Strategy). Метаданные — в PostgreSQL.

Сейчас используется `LocalStorageAdapter` (файлы на диске). Потом переключаемся на `S3StorageAdapter` (SeaweedFS или другой S3-провайдер) — меняем одну строку в конфиге.

```
FileService
     │
     ├── upload_file()
     ├── get_file_content()
     ├── get_download_url()
     ├── delete_file()
     │
     └── self.adapter: StorageAdapter
              │
              ├── LocalStorageAdapter    ← Alpha (файлы на диске)
              │   └── /var/lib/rugpt/uploads/{org}/{user}/{id}.pdf
              │
              └── S3StorageAdapter       ← Потом (SeaweedFS / AWS S3)
                  └── boto3 → S3 endpoint
```

**Переключение:**
```env
# Alpha (локальная ФС):
STORAGE_BACKEND=local
STORAGE_LOCAL_DIR=/var/lib/rugpt/uploads

# Потом (S3):
STORAGE_BACKEND=s3
S3_ENDPOINT=http://localhost:8333
S3_ACCESS_KEY=...
S3_SECRET_KEY=...
S3_BUCKET=rugpt-files
```

### 5.2 StorageAdapter — интерфейс и реализации

**Файл:** `src/engine/storage/file_adapters/base.py`

```python
from abc import ABC, abstractmethod

class StorageAdapter(ABC):
    """Абстракция хранилища файлов. Реализации: local FS, S3."""

    @abstractmethod
    async def save(self, key: str, data: bytes, content_type: str) -> None:
        """Сохранить файл по ключу"""

    @abstractmethod
    async def read(self, key: str) -> bytes:
        """Прочитать содержимое файла"""

    @abstractmethod
    async def delete(self, key: str) -> None:
        """Удалить файл"""

    @abstractmethod
    async def get_download_url(self, key: str, expires_sec: int = 3600) -> str:
        """Получить URL для скачивания"""

    @abstractmethod
    async def exists(self, key: str) -> bool:
        """Проверить существование файла"""
```

**Файл:** `src/engine/storage/file_adapters/local.py`

```python
import os
import aiofiles
from pathlib import Path

class LocalStorageAdapter(StorageAdapter):
    def __init__(self, base_dir: str):
        self.base_dir = Path(base_dir)

    async def save(self, key: str, data: bytes, content_type: str) -> None:
        path = self.base_dir / key
        path.parent.mkdir(parents=True, exist_ok=True)
        async with aiofiles.open(path, "wb") as f:
            await f.write(data)

    async def read(self, key: str) -> bytes:
        path = self.base_dir / key
        async with aiofiles.open(path, "rb") as f:
            return await f.read()

    async def delete(self, key: str) -> None:
        path = self.base_dir / key
        if path.exists():
            os.remove(path)

    async def get_download_url(self, key: str, expires_sec: int = 3600) -> str:
        # Для локальной ФС — скачивание через Engine API
        # Возвращаем относительный путь, routes проксируют файл
        return f"/api/v1/files/download/{key}"

    async def exists(self, key: str) -> bool:
        return (self.base_dir / key).exists()
```

**Файл:** `src/engine/storage/file_adapters/s3.py`

```python
import boto3
from io import BytesIO

class S3StorageAdapter(StorageAdapter):
    """Реализация для S3-совместимых хранилищ (SeaweedFS, AWS S3, Garage и др.)"""

    def __init__(self, endpoint_url: str, access_key: str,
                 secret_key: str, bucket: str, secure: bool = False):
        self.bucket = bucket
        self.s3 = boto3.client(
            "s3",
            endpoint_url=endpoint_url,
            aws_access_key_id=access_key,
            aws_secret_access_key=secret_key,
        )
        # Создать бакет если не существует
        try:
            self.s3.head_bucket(Bucket=bucket)
        except:
            self.s3.create_bucket(Bucket=bucket)

    async def save(self, key: str, data: bytes, content_type: str) -> None:
        self.s3.put_object(
            Bucket=self.bucket, Key=key,
            Body=data, ContentType=content_type
        )

    async def read(self, key: str) -> bytes:
        response = self.s3.get_object(Bucket=self.bucket, Key=key)
        return response["Body"].read()

    async def delete(self, key: str) -> None:
        self.s3.delete_object(Bucket=self.bucket, Key=key)

    async def get_download_url(self, key: str, expires_sec: int = 3600) -> str:
        return self.s3.generate_presigned_url(
            "get_object",
            Params={"Bucket": self.bucket, "Key": key},
            ExpiresIn=expires_sec
        )

    async def exists(self, key: str) -> bool:
        try:
            self.s3.head_object(Bucket=self.bucket, Key=key)
            return True
        except:
            return False
```

### 5.3 Структура файлов адаптеров

```
src/engine/storage/
├── file_adapters/
│   ├── __init__.py
│   ├── base.py          # StorageAdapter (ABC)
│   ├── local.py         # LocalStorageAdapter
│   └── s3.py            # S3StorageAdapter
└── ...
```

### 5.4 Модель данных

#### Таблица `user_files`

```sql
CREATE TABLE user_files (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID NOT NULL REFERENCES users(id),
        -- сотрудник, которому принадлежит файл
    org_id UUID NOT NULL REFERENCES organizations(id),
    uploaded_by_user_id UUID NOT NULL REFERENCES users(id),
        -- руководитель, загрузивший файл
    storage_key VARCHAR(500) NOT NULL,
        -- ключ в хранилище: {org_id}/{user_id}/{file_id}.{ext}
        -- одинаков для local FS и S3
    original_filename VARCHAR(500) NOT NULL,
    file_type VARCHAR(20) NOT NULL,
        -- pdf | docx
    file_size BIGINT NOT NULL,
    content_type VARCHAR(100) NOT NULL DEFAULT 'application/octet-stream',
    rag_status VARCHAR(20) NOT NULL DEFAULT 'pending',
        -- pending | indexing | indexed | failed
    rag_error TEXT,
    indexed_at TIMESTAMP WITH TIME ZONE,
    is_active BOOLEAN DEFAULT true,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_user_files_user
    ON user_files(user_id)
    WHERE is_active = true;
CREATE INDEX IF NOT EXISTS idx_user_files_org
    ON user_files(org_id);
CREATE INDEX IF NOT EXISTS idx_user_files_pending_rag
    ON user_files(rag_status)
    WHERE rag_status IN ('pending', 'indexing');
```

> `storage_key` — единый ключ для обоих бэкендов. В local FS это путь относительно `base_dir`, в S3 это object key в бакете. Формат: `{org_id}/{user_id}/{file_id}.{ext}`.

#### Dataclass

```python
@dataclass
class UserFile:
    id: UUID
    user_id: UUID
    org_id: UUID
    uploaded_by_user_id: UUID
    storage_key: str                # ключ в хранилище (local path / S3 key)
    original_filename: str
    file_type: str                  # "pdf" | "docx"
    file_size: int
    content_type: str
    rag_status: str                 # "pending" | "indexing" | "indexed" | "failed"
    rag_error: Optional[str]
    indexed_at: Optional[datetime]
    is_active: bool
    created_at: datetime
    updated_at: datetime
```

### 5.5 Storage (PostgreSQL)

**Файл:** `src/engine/storage/user_file_storage.py`

```python
class UserFileStorage(BaseStorage):
    async def create(file: UserFile) -> UserFile
    async def get_by_id(file_id: UUID) -> UserFile?
    async def list_by_user(user_id: UUID, active_only: bool) -> List[UserFile]
    async def list_by_org(org_id: UUID) -> List[UserFile]
    async def list_pending_indexing() -> List[UserFile]
    async def update_rag_status(file_id: UUID, status: str, error?: str) -> bool
    async def deactivate(file_id: UUID) -> bool
```

### 5.6 FileService

**Файл:** `src/engine/services/file_service.py`

FileService не знает о конкретном бэкенде — работает через StorageAdapter.

```python
CONTENT_TYPES = {
    "pdf": "application/pdf",
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
}

class FileService:
    def __init__(self, file_storage: UserFileStorage,
                 storage_adapter: StorageAdapter):
        self.file_storage = file_storage
        self.adapter = storage_adapter

    async def upload_file(self, user_id, org_id, uploaded_by_user_id,
                          file_content: bytes, original_filename: str) -> UserFile:
        # 1. Валидация типа файла
        ext = original_filename.rsplit(".", 1)[-1].lower()
        if ext not in CONTENT_TYPES:
            raise ValueError(f"Unsupported file type: {ext}")

        # 2. Генерация ключа
        file_id = uuid4()
        storage_key = f"{org_id}/{user_id}/{file_id}.{ext}"
        content_type = CONTENT_TYPES[ext]

        # 3. Сохранение файла через адаптер
        await self.adapter.save(storage_key, file_content, content_type)

        # 4. Метаданные в PostgreSQL
        user_file = UserFile(
            id=file_id, user_id=user_id, org_id=org_id,
            uploaded_by_user_id=uploaded_by_user_id,
            storage_key=storage_key,
            original_filename=original_filename,
            file_type=ext, file_size=len(file_content),
            content_type=content_type,
            rag_status="pending", ...
        )
        return await self.file_storage.create(user_file)

    async def get_download_url(self, file_id, expires_sec=3600) -> str:
        file = await self.file_storage.get_by_id(file_id)
        return await self.adapter.get_download_url(file.storage_key, expires_sec)

    async def get_file_content(self, file_id) -> bytes:
        """Для RAG-индексации"""
        file = await self.file_storage.get_by_id(file_id)
        return await self.adapter.read(file.storage_key)

    async def delete_file(self, file_id) -> bool:
        file = await self.file_storage.get_by_id(file_id)
        if not file:
            return False
        await self.adapter.delete(file.storage_key)
        return await self.file_storage.deactivate(file_id)

    async def list_files(self, user_id=None, org_id=None) -> List[UserFile]:
        if user_id:
            return await self.file_storage.list_by_user(user_id, active_only=True)
        return await self.file_storage.list_by_org(org_id)

    async def get_user_file_ids(self, user_id) -> List[UUID]:
        """Для RAG-фильтрации"""
        files = await self.file_storage.list_by_user(user_id, active_only=True)
        return [f.id for f in files if f.rag_status == "indexed"]
```

> **Индексация в vector store** будет добавлена когда второй специалист определит архитектуру RAG. FileService предоставляет `get_file_content()` для получения бинарных данных и `list_pending_indexing()` + `update_rag_status()` для интеграции.

### 5.7 Инициализация в EngineService

```python
# В engine_service.py:
from src.engine.storage.file_adapters.local import LocalStorageAdapter
from src.engine.storage.file_adapters.s3 import S3StorageAdapter

async def initialize(self):
    ...
    # Выбор адаптера хранилища по конфигу
    if config.STORAGE_BACKEND == "s3":
        storage_adapter = S3StorageAdapter(
            endpoint_url=config.S3_ENDPOINT,
            access_key=config.S3_ACCESS_KEY,
            secret_key=config.S3_SECRET_KEY,
            bucket=config.S3_BUCKET,
        )
    else:
        storage_adapter = LocalStorageAdapter(
            base_dir=config.STORAGE_LOCAL_DIR
        )

    self.file_service = FileService(self.user_file_storage, storage_adapter)
    ...
```

### 5.8 Конфигурация (.env)

```env
# Выбор бэкенда: "local" или "s3"
STORAGE_BACKEND=local

# Для local:
STORAGE_LOCAL_DIR=/var/lib/rugpt/uploads

# Для s3 (потом, когда поднимем SeaweedFS):
# S3_ENDPOINT=http://localhost:8333
# S3_ACCESS_KEY=...
# S3_SECRET_KEY=...
# S3_BUCKET=rugpt-files

# Лимиты загрузки:
FILE_MAX_SIZE_MB=50
FILE_ALLOWED_TYPES=pdf,docx
```

### 5.9 API Routes

**Файл:** `src/engine/routes/files.py`

```
POST   /api/v1/files/upload              — загрузить файл (multipart/form-data)
                                            query: user_id (кому загружается)
GET    /api/v1/files                      — список файлов (admin: все, user: свои)
GET    /api/v1/files/user/{user_id}       — файлы сотрудника (admin only)
GET    /api/v1/files/{file_id}            — метаданные файла
DELETE /api/v1/files/{file_id}            — удалить файл (admin only)
GET    /api/v1/files/{file_id}/download   — скачать файл
```

**Download:**
- **Local-режим:** Engine проксирует файл (FileResponse или StreamingResponse)
- **S3-режим:** возвращает presigned URL, клиент скачивает напрямую из S3

```python
@router.get("/files/{file_id}/download")
async def download_file(file_id: UUID):
    url = await file_service.get_download_url(file_id)
    if config.STORAGE_BACKEND == "local":
        # Проксируем файл через Engine
        file = await file_service.file_storage.get_by_id(file_id)
        file_path = Path(config.STORAGE_LOCAL_DIR) / file.storage_key
        return FileResponse(file_path, filename=file.original_filename)
    else:
        # Redirect на presigned URL
        return {"download_url": url}
```

### 5.10 Связь с RAG

Роль пользователя при вызове tool `rag_search`:
1. Получает `user_id` из контекста вызова
2. `FileService.get_user_file_ids(user_id)` → список file_id проиндексированных файлов
3. Поиск в vector store с фильтром `file_id IN (...)` → изоляция по пользователю

```python
# В будущем rag_tool.py:
async def rag_search(query: str, user_id: UUID) -> str:
    file_ids = await file_service.get_user_file_ids(user_id)
    results = await vector_store.search(query, filter={"file_id": {"$in": file_ids}})
    return format_results(results)
```

> При переходе на S3 (SeaweedFS) — LangChain document loaders (S3DirectoryLoader, S3FileLoader) нативно поддерживают S3 API. RAG-специалист сможет работать с S3 без адаптеров.

---

## 6. Изменения в SchedulerService

### 6.1 Новые типы cron-задач

Текущий SchedulerService работает с `calendar_events`. Добавляются два новых типа задач:

```python
class SchedulerService:
    # Существующее:
    async def _poll_calendar_events(self)

    # Новое:
    async def _morning_poll_job(self)
        # Запускается по утреннему cron (настраивается в .env)
        # Вызывает TaskPollService.generate_morning_polls()

    async def _evening_report_job(self)
        # Запускается по вечернему cron
        # 1. TaskPollService.expire_old_polls() — истечь неотвеченные
        # 2. TaskService.check_overdue() — проверить просроченные
        # 3. TaskReportService.generate_evening_reports()

    async def _run(self):
        while True:
            now = datetime.now()
            # Проверка calendar_events (каждые 30 сек, как сейчас)
            await self._poll_calendar_events()
            # Проверка morning_poll (раз в минуту, если время пришло)
            if self._should_run_morning_poll(now):
                await self._morning_poll_job()
            # Проверка evening_report (раз в минуту, если время пришло)
            if self._should_run_evening_report(now):
                await self._evening_report_job()
            await asyncio.sleep(self.poll_interval)
```

### 6.2 Конфигурация

```env
# Существующее:
SCHEDULER_POLL_INTERVAL=30
SCHEDULER_ENABLED=true

# Новое:
TASK_MORNING_POLL_CRON=0 9 * * 1-5     # Пн-Пт 09:00
TASK_EVENING_REPORT_CRON=0 18 * * 1-5  # Пн-Пт 18:00
TASK_POLL_EXPIRE_HOURS=10              # Опрос истекает через 10 часов
```

---

## 7. Изменения в EngineService

### 7.1 Новые зависимости

```python
class EngineService:
    # Существующие storages...

    # Новые storages:
    task_storage: TaskStorage
    task_poll_storage: TaskPollStorage
    task_report_storage: TaskReportStorage
    in_app_notification_storage: InAppNotificationStorage
    user_file_storage: UserFileStorage

    # Новые services:
    task_service: TaskService
    task_poll_service: TaskPollService
    task_report_service: TaskReportService
    in_app_notification_service: InAppNotificationService
    file_service: FileService
```

### 7.2 Порядок инициализации (обновлённый)

```
1. Storages (все, включая новые)
2. LLM Provider, PromptCache
3. InAppNotificationService (нужен другим сервисам)
4. TaskService (нужен task tools и poll service)
5. CalendarService, NotificationService
6. FileService
7. AgentExecutor, ToolRegistry (+ task tools, + rag tool с file_service)
8. TaskPollService (зависит от task, notification)
9. TaskReportService (зависит от task, poll, agent_executor)
10. SchedulerService (+ task_poll_service, task_report_service, task_service)
11. ChatService, AIService
```

---

## 8. Изменения в ToolRegistry

Новые tools для ролей:

| Tool | Описание | Используется |
|------|----------|-------------|
| `task_create` | Создать задачу из чата | При @@ mention от руководителя |
| `task_query` | Запросить задачи сотрудника | Для контекста при ответах |
| `task_update` | Обновить статус задачи | При обработке результатов |

Роли, которым нужны task tools, должны иметь их в `role.tools`:
```json
{
  "tools": ["task_create", "task_query", "task_update", "calendar_create", "calendar_query"]
}
```

---

## 9. Структура файлов (новые)

```
src/engine/
├── models/
│   ├── task.py                    # Task
│   ├── task_poll.py               # TaskPoll, TaskPollResponse
│   ├── task_report.py             # TaskReport, TaskSummary
│   ├── in_app_notification.py     # InAppNotification
│   └── user_file.py               # UserFile
│
├── storage/
│   ├── task_storage.py
│   ├── task_poll_storage.py
│   ├── task_report_storage.py
│   ├── in_app_notification_storage.py
│   └── user_file_storage.py
│
├── services/
│   ├── task_service.py
│   ├── task_poll_service.py
│   ├── task_report_service.py
│   ├── in_app_notification_service.py
│   └── file_service.py
│
├── routes/
│   ├── tasks.py
│   ├── task_polls.py
│   ├── task_reports.py
│   ├── in_app_notifications.py
│   └── files.py
│
├── agents/tools/
│   └── task_tool.py               # task_create, task_query, task_update
│
└── migrations/
    ├── 003_task_management.sql    # tasks, task_polls, task_reports
    ├── 004_in_app_notifications.sql
    └── 005_user_files.sql
```

---

## 10. Связи между модулями

```
                    SchedulerService
                    /       |       \
                   /        |        \
     morning_poll   calendar_events   evening_report
          |                                |
          ▼                                ▼
    TaskPollService                  TaskReportService
          |                           /          \
          ▼                          ▼            ▼
    InAppNotificationService    AgentExecutor   InAppNotificationService
          |                          |
          ▼                          ▼
    [Колокольчик UI]           [AI-анализ]


    ChatService (@@mention)
          |
          ▼
    AIService → AgentExecutor
                     |
                     ▼
               ToolRegistry
              /      |      \
    task_create  task_query  rag_search
         |                       |
         ▼                       ▼
    TaskService              FileService
         |                       |
         ▼                       ▼
    InAppNotif.Service       [Vector Store]
```

---

## 11. WebClient: новые разделы (для webclient_rugpt)

> Детальная реализация фронтенда — отдельный документ. Здесь описаны только необходимые API-вызовы и UI-структура.

### Навигация

```
Sidebar:
├── Чат (существующий)
├── Задачи                    (новый)
│   ├── Список задач
│   ├── Создание задачи
│   └── Отчёты
├── Файлы                     (новый)
│   └── Загрузка / список
├── Роли (существующий)
├── Календарь (существующий)
└── Настройки (существующий)

Header:
└── Колокольчик (badge с количеством непрочитанных)
    └── Dropdown: список уведомлений
        ├── Клик на poll → попап опроса
        ├── Клик на report → страница отчёта
        ├── Клик на task → страница задачи
        └── Клик на mention → сообщение в чате
```

### Попап опроса

```
┌─────────────────────────────────────────┐
│  Утренний опрос — 4 марта 2026          │
│                                          │
│  1. Проверить договор аренды             │
│     Текущий статус: создана              │
│     Новый статус: [ в работе ▼ ]         │
│     Комментарий: [____________________]  │
│                                          │
│  2. Подготовить отчёт по НДС            │
│     Текущий статус: в работе             │
│     Новый статус: [ выполнена ▼ ]        │
│     Комментарий: [____________________]  │
│                                          │
│           [ Отправить ]                  │
└──────────────────────────────────────────┘
```
