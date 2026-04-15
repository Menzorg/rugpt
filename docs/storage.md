# RuGPT Storage Layer

PostgreSQL хранилище данных.

## BaseStorage

**Файл:** `src/engine/storage/base.py`

Базовый класс для всех хранилищ.

```python
class BaseStorage:
    def __init__(self, postgres_dsn: str)

    async def init()                    # Инициализация пула
    async def close()                   # Закрытие соединений

    # Базовые методы
    async def execute(query, *args) -> str          # Выполнить запрос
    async def fetch(query, *args) -> list           # Получить несколько строк
    async def fetchrow(query, *args) -> Record?     # Получить одну строку
    async def fetchval(query, *args) -> Any         # Получить одно значение
```

**Особенности:**
- Асинхронный пул соединений через asyncpg
- Автоматическое переподключение при смене процесса (для gunicorn)
- Retry logic при подключении (3 попытки)

---

## OrgStorage

**Файл:** `src/engine/storage/org_storage.py`

```python
class OrgStorage(BaseStorage):
    async def create(org: Organization) -> Organization
    async def get_by_id(org_id: UUID) -> Organization?
    async def get_by_slug(slug: str) -> Organization?
    async def list_all(active_only: bool) -> List[Organization]
    async def update(org: Organization) -> Organization
    async def delete(org_id: UUID) -> bool
    async def exists_by_slug(slug: str, exclude_id?: UUID) -> bool
```

---

## UserStorage

**Файл:** `src/engine/storage/user_storage.py`

```python
class UserStorage(BaseStorage):
    async def create(user: User) -> User
    async def get_by_id(user_id: UUID) -> User?
    async def get_by_email(email: str) -> User?
    async def get_by_username(username: str, org_id: UUID) -> User?
    async def get_system_users() -> List[User]
    async def get_system_user_by_username(username: str) -> User?
    async def get_system_user_by_model(model_code: str) -> User?
    async def list_by_org(org_id: UUID, active_only: bool) -> List[User]
    async def list_by_role(role_id: UUID) -> List[User]
    async def list_admins_by_org(org_id: UUID) -> List[User]
    async def update(user: User) -> User
    async def update_last_seen(user_id: UUID) -> None
    async def assign_role(user_id: UUID, role_id?: UUID) -> bool
    async def delete(user_id: UUID) -> bool
    async def exists_by_email(email: str, exclude_id?: UUID) -> bool
    async def exists_by_username(username: str, org_id: UUID, exclude_id?: UUID) -> bool
```

---

## RoleStorage

**Файл:** `src/engine/storage/role_storage.py`

```python
class RoleStorage(BaseStorage):
    async def create(role: Role) -> Role
    async def get_by_id(role_id: UUID) -> Role?
    async def get_by_code(code: str, org_id: UUID) -> Role?
    async def list_by_org(org_id: UUID, active_only: bool) -> List[Role]
    async def update(role: Role) -> Role
    async def delete(role_id: UUID) -> bool
    async def exists_by_code(code: str, org_id: UUID, exclude_id?: UUID) -> bool
```

**Особенности:**
- `agent_config` (JSONB) и `tools` (JSONB) парсятся при чтении из БД

---

## ChatStorage

**Файл:** `src/engine/storage/chat_storage.py`

```python
class ChatStorage(BaseStorage):
    async def create(chat: Chat) -> Chat
    async def get_by_id(chat_id: UUID) -> Chat?
    async def get_direct_chat(user1_id: UUID, user2_id: UUID) -> Chat?
    async def list_by_user(user_id: UUID, active_only: bool) -> List[Chat]
    async def list_by_org(org_id: UUID, active_only: bool) -> List[Chat]
    async def update(chat: Chat) -> Chat
    async def update_last_message(chat_id: UUID) -> None
    async def add_participant(chat_id: UUID, user_id: UUID) -> bool
    async def remove_participant(chat_id: UUID, user_id: UUID) -> bool
    async def delete(chat_id: UUID) -> bool
```

**Особенности:**
- `participants` хранится как TEXT[] (массив UUID в виде строк)
- Поиск по participants через GIN индекс

---

## MessageStorage

**Файл:** `src/engine/storage/message_storage.py`

```python
class MessageStorage(BaseStorage):
    async def create(message: Message) -> Message
    async def get_by_id(message_id: UUID) -> Message?
    async def list_by_chat(chat_id, limit=50, before_id=None) -> List[Message]
    async def validate(message_id: UUID, edited_content: str? = None) -> Message?
    async def reject(message_id: UUID) -> Message?
    async def list_pending_review(user_id: UUID) -> List[Message]
    async def delete(message_id: UUID) -> bool
    async def count_by_chat(chat_id: UUID) -> int
```

**Особенности:**
- `mentions` хранится как JSONB
- Пагинация через cursor (`before_id`)
- `list_by_chat` возвращает сообщения в хронологическом порядке (ASC)
- `validate()` ставит `ai_is_valid=true`, опционально обновляет content
- `reject()` ставит `ai_is_valid=false`

---

## CalendarStorage

**Файл:** `src/engine/storage/calendar_storage.py`

```python
class CalendarStorage(BaseStorage):
    async def create(event: CalendarEvent) -> CalendarEvent
    async def get_by_id(event_id: UUID) -> CalendarEvent?
    async def list_by_org(org_id: UUID, active_only: bool) -> List[CalendarEvent]
    async def list_by_role(role_id: UUID, active_only: bool) -> List[CalendarEvent]
    async def get_due_events(now: datetime) -> List[CalendarEvent]
    async def update(event: CalendarEvent) -> CalendarEvent
    async def deactivate(event_id: UUID) -> bool
```

---

## NotificationChannelStorage

**Файл:** `src/engine/storage/notification_channel_storage.py`

```python
class NotificationChannelStorage(BaseStorage):
    async def create(channel: NotificationChannel) -> NotificationChannel
    async def get_by_user_and_type(user_id: UUID, channel_type: str) -> NotificationChannel?
    async def list_by_user(user_id: UUID, enabled_only: bool) -> List[NotificationChannel]
    async def update(channel: NotificationChannel) -> NotificationChannel
    async def delete_by_user_and_type(user_id: UUID, channel_type: str) -> bool
```

---

## NotificationLogStorage

**Файл:** `src/engine/storage/notification_log_storage.py`

```python
class NotificationLogStorage(BaseStorage):
    async def create(log_entry: NotificationLog) -> NotificationLog
    async def update_status(log_id: UUID, status: str, attempts: int, error_message?: str) -> NotificationLog?
    async def list_by_user(user_id: UUID, limit=50) -> List[NotificationLog]
    async def list_by_event(event_id: UUID) -> List[NotificationLog]
```

---

## TaskStorage

**Файл:** `src/engine/storage/task_storage.py`

```python
class TaskStorage(BaseStorage):
    async def create(task: Task) -> Task
    async def get_by_id(task_id: UUID) -> Task?
    async def list_by_assignee(user_id, status?) -> List[Task]
    async def list_by_assignee_with_priority(user_id, include_done?) -> List[dict]  # + creator role
    async def list_by_creator_with_assignee(user_id, include_done?) -> List[dict]
    async def list_archived_for_user(user_id, org_id, limit?) -> List[dict]  # item 11: done | cancelled
    async def list_by_org(org_id, status?) -> List[Task]
    async def list_active_for_polls(user_id) -> List[Task]
    async def list_active_with_deadline() -> List[Task]  # scheduler overdue check
    async def update(task: Task) -> Task
    async def deactivate(task_id) -> bool
    async def count_active_in_project(project_id) -> int  # item 11
    async def get_many_by_ids(ids: List[UUID]) -> Dict[UUID, Task]  # item 11 batch for references
```

---

## ProjectStorage (item 11)

**Файл:** `src/engine/storage/project_storage.py`

```python
class ProjectStorage(BaseStorage):
    async def create(project) -> Project
    async def get_by_id(project_id) -> Project?
    async def list_by_org(org_id, include_archived?) -> List[Project]
    async def update(project) -> Project
    async def deactivate(project_id) -> bool
    async def get_many_by_ids(ids) -> Dict[UUID, Project]  # batch for references
```

---

## TaskEventStorage (item 11)

**Файл:** `src/engine/storage/task_event_storage.py`

```python
class TaskEventStorage(BaseStorage):
    async def create(event: TaskEvent) -> TaskEvent  # JSONB payload
    async def list_by_task(task_id, limit?) -> List[TaskEvent]  # DESC by created_at
```

---

## AgentRunStorage (item 10)

**Файл:** `src/engine/storage/agent_run_storage.py`

Idempotency для асинхронных agent executions через атомарный CAS.

```python
class AgentRunStorage(BaseStorage):
    async def create(run: AgentRun) -> AgentRun  # status=pending
    async def get(request_id) -> AgentRun?
    async def mark_running(request_id) -> bool
    # Atomic CAS: UPDATE WHERE status='pending' RETURNING
    # True iff this caller won the race (Kafka redelivery safety)
    async def mark_done(request_id, result_message_id)
    async def mark_failed(request_id, error: str)
```

**Статусы:** `pending | running | done | failed`

---

## TaskPollStorage

**Файл:** `src/engine/storage/task_poll_storage.py`

```python
class TaskPollStorage(BaseStorage):
    async def create(poll: TaskPoll) -> TaskPoll
    async def get_by_id(poll_id: UUID) -> TaskPoll?
    async def get_today(user_id: UUID, poll_date: date) -> TaskPoll?
    async def list_by_user(user_id: UUID, limit: int) -> List[TaskPoll]
    async def update(poll: TaskPoll) -> TaskPoll
```

---

## TaskReportStorage

**Файл:** `src/engine/storage/task_report_storage.py`

```python
class TaskReportStorage(BaseStorage):
    async def create(report: TaskReport) -> TaskReport
    async def get_by_id(report_id: UUID) -> TaskReport?
    async def list_by_user(user_id: UUID, limit: int) -> List[TaskReport]
```

---

## InAppNotificationStorage

**Файл:** `src/engine/storage/in_app_notification_storage.py`

```python
class InAppNotificationStorage(BaseStorage):
    async def create(notification: InAppNotification) -> InAppNotification
    async def list_by_user(user_id: UUID, limit: int) -> List[InAppNotification]
    async def get_unread_count(user_id: UUID) -> int
    async def mark_read(notification_id: UUID) -> bool
    async def mark_all_read(user_id: UUID) -> int
```

---

## UserFileStorage

**Файл:** `src/engine/storage/user_file_storage.py`

```python
class UserFileStorage(BaseStorage):
    async def create(file: UserFile) -> UserFile
    async def get_by_id(file_id: UUID) -> UserFile?
    async def list_by_user(user_id: UUID) -> List[UserFile]
    async def list_by_org(org_id: UUID) -> List[UserFile]
    async def find_duplicate(user_id: UUID, content_hash: str) -> UserFile?
    async def change_rag_status(file_id: UUID, status: str) -> None
    async def change_public(file_id: UUID, is_public: bool) -> UserFile?
    async def deactivate(file_id: UUID) -> bool
```

---

## CorrectionRuleStorage

**Файл:** `src/engine/storage/correction_rule_storage.py`

```python
class CorrectionRuleStorage(BaseStorage):
    async def create(rule: CorrectionRule) -> CorrectionRule
    async def get_rules_for_role(role_id: UUID) -> List[CorrectionRule]
    async def update_rule_text(rule_id: UUID, rule_text: str) -> None
```

---

## DeviceStorage

**Файл:** `src/engine/storage/device_storage.py`

```python
class DeviceStorage(BaseStorage):
    async def create(user_id, device_name, public_key_pem) -> dict
    async def list_by_user(user_id: UUID) -> List[dict]
    async def get_by_id(device_id: UUID) -> dict?
```

---

## RAG_store

**Файл:** `src/engine/storage/rag_store.py`

Отдельный storage для RAG-данных. Подробнее см. `docs/rag-info.md`.

```python
class RAG_store(BaseStorage):
    def __init__(self, dsn: str, vector_dim: int)

    async def update_user_file_rag_data(file_id, summary, summary_embedding) -> None
    async def insert_document_with_chunks(file_id, doc_title, summary, summary_embedding,
                                           org_id, user_id, chunks, chunk_embeddings) -> None
    async def insert_table_document_with_rows(file_id, doc_title, summary, summary_embedding,
                                               org_id, user_id, rows_text, row_embeddings) -> None
    async def delete_document(file_id) -> bool
    async def call_search_related_docs(org_id, user_id, query, query_embedding, top_k) -> List[RelatedDoc]
    async def call_search_abstract_chunks(file_id, query, query_embedding, top_k) -> List[ChunkSearchResult]
    async def call_search_concrete_chunks(file_id, query, query_embedding, top_k, tsv_weight) -> List[ChunkSearchResult]
```

---

## StorageAdapter

**Файл:** `src/engine/storage/storage_adapter.py`

Абстракция для бинарного хранения файлов (не PostgreSQL).

```python
class StorageAdapter(ABC):
    async def save(key: str, data: bytes, content_type: str) -> None
    async def read(key: str) -> bytes
    async def delete(key: str) -> None
    async def exists(key: str) -> bool

class LocalStorageAdapter(StorageAdapter):
    def __init__(self, base_dir: str)
    # Хранит файлы на локальной ФС
```

---

## Схема базы данных

Основные таблицы (миграция 001 + последующие):

```sql
-- Организации (+ 014: timezone)
CREATE TABLE organizations (
    id UUID PRIMARY KEY,
    name VARCHAR(255) NOT NULL,
    slug VARCHAR(100) NOT NULL UNIQUE,
    description TEXT,
    timezone VARCHAR(64) DEFAULT 'Europe/Moscow',  -- IANA timezone
    is_active BOOLEAN DEFAULT true,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

-- Роли (AI-агенты) (001 + 002)
CREATE TABLE roles (
    id UUID PRIMARY KEY,
    org_id UUID REFERENCES organizations(id),
    name VARCHAR(255) NOT NULL,
    code VARCHAR(50) NOT NULL,
    description TEXT,
    system_prompt TEXT NOT NULL,
    rag_collection VARCHAR(255),
    model_name VARCHAR(100) DEFAULT 'qwen2.5:7b',
    agent_type VARCHAR(20) NOT NULL DEFAULT 'simple',
    agent_config JSONB DEFAULT '{}',
    tools JSONB DEFAULT '[]',
    prompt_file VARCHAR(255),
    is_active BOOLEAN DEFAULT true,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE(org_id, code)
);

-- Пользователи (001 + 003: is_system)
CREATE TABLE users (
    id UUID PRIMARY KEY,
    org_id UUID REFERENCES organizations(id),
    name VARCHAR(255) NOT NULL,
    username VARCHAR(50) NOT NULL,
    email VARCHAR(255) NOT NULL UNIQUE,
    password_hash VARCHAR(255),
    role_id UUID REFERENCES roles(id),
    is_admin BOOLEAN DEFAULT false,
    is_system BOOLEAN DEFAULT false,
    is_active BOOLEAN DEFAULT true,
    avatar_url TEXT,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW(),
    last_seen_at TIMESTAMPTZ,
    UNIQUE(org_id, username)
);

-- Сообщения (001 + 010: ai_is_valid)
CREATE TABLE messages (
    id UUID PRIMARY KEY,
    chat_id UUID REFERENCES chats(id),
    sender_type VARCHAR(20) DEFAULT 'user',
    sender_id UUID NOT NULL,
    content TEXT NOT NULL,
    mentions JSONB,
    reply_to_id UUID REFERENCES messages(id),
    ai_is_valid BOOLEAN,            -- NULL=pending, true=approved, false=rejected
    ai_edited BOOLEAN DEFAULT false,
    is_deleted BOOLEAN DEFAULT false,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

-- Задачи (005)
CREATE TABLE tasks (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    org_id UUID NOT NULL REFERENCES organizations(id),
    title VARCHAR(500) NOT NULL,
    description TEXT,
    status VARCHAR(20) NOT NULL DEFAULT 'created',
    assignee_user_id UUID NOT NULL REFERENCES users(id),
    deadline TIMESTAMPTZ,
    is_active BOOLEAN DEFAULT true,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

-- Утренние опросы (006)
CREATE TABLE task_polls (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    org_id UUID NOT NULL REFERENCES organizations(id),
    assignee_user_id UUID NOT NULL REFERENCES users(id),
    poll_date DATE NOT NULL,
    status VARCHAR(20) NOT NULL DEFAULT 'pending',
    responses JSONB,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    completed_at TIMESTAMPTZ,
    expires_at TIMESTAMPTZ,
    UNIQUE(assignee_user_id, poll_date)
);

-- Вечерние отчёты (007)
CREATE TABLE task_reports (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    org_id UUID NOT NULL REFERENCES organizations(id),
    generated_for_user_id UUID NOT NULL REFERENCES users(id),
    report_date DATE NOT NULL,
    content TEXT NOT NULL,
    task_summaries JSONB,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

-- In-app уведомления (008)
CREATE TABLE in_app_notifications (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID NOT NULL REFERENCES users(id),
    org_id UUID NOT NULL REFERENCES organizations(id),
    type VARCHAR(50) NOT NULL,
    title VARCHAR(500) NOT NULL,
    content TEXT,
    reference_type VARCHAR(50),
    reference_id UUID,
    is_read BOOLEAN DEFAULT false,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

-- Файлы (009 + 012: RAG-поля)
CREATE TABLE user_files (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID NOT NULL REFERENCES users(id),
    org_id UUID NOT NULL REFERENCES organizations(id),
    uploaded_by_user_id UUID NOT NULL REFERENCES users(id),
    storage_key VARCHAR(500) NOT NULL,
    original_filename VARCHAR(500) NOT NULL,
    file_type VARCHAR(20),
    file_size BIGINT,
    content_hash TEXT NOT NULL,
    summary TEXT NOT NULL DEFAULT '',
    summary_embedding vector(1024),
    is_table BOOLEAN NOT NULL DEFAULT false,
    is_public BOOLEAN NOT NULL DEFAULT false,
    rag_status VARCHAR(20) DEFAULT 'pending',
    rag_error TEXT,
    indexed_at TIMESTAMPTZ,
    is_active BOOLEAN DEFAULT true,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW(),
    tsv tsvector GENERATED ALWAYS AS (...) STORED
);

-- Правила коррекции AI (010)
CREATE TABLE correction_rules (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    role_id UUID NOT NULL REFERENCES roles(id),
    org_id UUID NOT NULL REFERENCES organizations(id),
    original_message_id UUID NOT NULL,
    ai_message_id UUID NOT NULL,
    chat_id UUID NOT NULL,
    user_question TEXT NOT NULL,
    ai_answer TEXT NOT NULL,
    correction_text TEXT NOT NULL,
    rule_text TEXT NOT NULL DEFAULT '',
    created_by_user_id UUID NOT NULL REFERENCES users(id),
    is_active BOOLEAN DEFAULT true,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

-- Устройства (011)
CREATE TABLE user_devices (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID NOT NULL REFERENCES users(id),
    device_name VARCHAR(255),
    public_key_pem TEXT NOT NULL,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

-- RAG: чанки (012)
CREATE TABLE chunks (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    file_id UUID NOT NULL REFERENCES user_files(id) ON DELETE CASCADE,
    chunk_text TEXT NOT NULL,
    embedding vector(1024) NOT NULL,
    metadata JSONB NOT NULL DEFAULT '{}',
    chunk_index INTEGER,
    tsv tsvector GENERATED ALWAYS AS (...) STORED
);

-- RAG: строки таблиц (012)
CREATE TABLE tables_rows_chunks (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    file_id UUID NOT NULL REFERENCES user_files(id) ON DELETE CASCADE,
    table_chunk_id UUID REFERENCES chunks(id) ON DELETE SET NULL,
    row_index INTEGER NOT NULL,
    row_text TEXT NOT NULL,
    embedding vector(1024) NOT NULL,
    tsv tsvector GENERATED ALWAYS AS (...) STORED,
    metadata JSONB NOT NULL DEFAULT '{}',
    created_at TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE(file_id, table_chunk_id, row_index)
);
```

Также: таблицы `chats`, `calendar_events`, `notification_channels`, `notification_log` (без изменений с миграции 001).

---

## Миграции

| # | Файл | Описание |
|---|------|----------|
| 001 | initial.sql | organizations, users, roles, chats, messages, calendar_events, notification_channels, notification_log |
| 002 | role_evolution.sql | agent_type, agent_config, tools, prompt_file на roles |
| 003 | system_user.sql | is_system на users, системные AI-пользователи |
| 004 | mirror_user.sql | Mirror user support |
| 005 | tasks.sql | tasks |
| 006 | task_polls.sql | task_polls |
| 007 | task_reports.sql | task_reports |
| 008 | in_app_notifications.sql | in_app_notifications |
| 009 | user_files.sql | user_files |
| 010 | correction_rules.sql | correction_rules, ai_validated -> ai_is_valid |
| 011 | user_devices.sql | user_devices (Zero Trust) |
| 012 | rag_schema.sql | pgvector, chunks, tables_rows_chunks, RAG-поля в user_files |
| 013 | rag_functions.sql | SQL-функции гибридного поиска (7 функций) |
| 014 | org_timezone.sql | timezone в organizations |
| 015 | departments.sql | departments, department_visibility, dept поля на users, org_context (item 8) |
| 016 | task_ownership.sql | tasks.created_by_user_id, awaiting_review_at, proposed_deadline, proposed_deadline_by (item 9) |
| 017 | projects_and_task_chats.sql | projects, task_events, tasks.project_id, chats.task_id/project_id, legacy main/group→direct (item 11) |
| 018 | pm_role_and_agent_runs.sql | PM role + system user `pm`, agent_runs таблица для async idempotency (item 10) |
