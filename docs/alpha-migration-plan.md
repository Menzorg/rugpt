# Alpha — План миграций

> Порядок создания новых таблиц и изменений в БД для Alpha.
> Существующие миграции: 001_initial, 002_role_evolution, 003_system_user, 004_mirror_user.

---

## Обзор миграций

| Миграция | Что создаёт | Зависимости |
|----------|------------|-------------|
| `005_tasks.sql` | tasks | organizations, users, roles, chats, messages |
| `006_task_polls.sql` | task_polls | organizations, users |
| `007_task_reports.sql` | task_reports | organizations, users, roles |
| `008_in_app_notifications.sql` | in_app_notifications | users, organizations |
| `009_user_files.sql` | user_files | users, organizations |

Миграции идемпотентны (`IF NOT EXISTS`, `ON CONFLICT`). Можно безопасно перезапускать.

---

## Порядок реализации модулей

Миграции создают только таблицы. Код реализуется в следующем порядке:

### Фаза A: In-App Notifications (фундамент)

Колокольчик — зависимость для всех остальных модулей (tasks, polls, reports создают уведомления).

1. Миграция `008_in_app_notifications.sql`
2. `models/in_app_notification.py`
3. `storage/in_app_notification_storage.py`
4. `services/in_app_notification_service.py`
5. `routes/in_app_notifications.py`
6. Регистрация в `engine_service.py`

### Фаза B: Task Management (ядро)

1. Миграция `005_tasks.sql`
2. `models/task.py`
3. `storage/task_storage.py`
4. `services/task_service.py` (использует in_app_notification_service)
5. `routes/tasks.py`
6. `agents/tools/task_tool.py` (task_create, task_query, task_update)
7. Регистрация tools в ToolRegistry
8. Обновление `role.tools` в БД для нужных ролей

### Фаза C: Task Polls (утренние опросы)

1. Миграция `006_task_polls.sql`
2. `models/task_poll.py`
3. `storage/task_poll_storage.py`
4. `services/task_poll_service.py` (использует task_service, in_app_notification_service)
5. `routes/task_polls.py`
6. Расширение `scheduler_service.py` — morning_poll_job

### Фаза D: Task Reports (вечерние отчёты)

1. Миграция `007_task_reports.sql`
2. `models/task_report.py`
3. `storage/task_report_storage.py`
4. `services/task_report_service.py` (использует task, poll, agent_executor, in_app_notification)
5. `routes/task_reports.py`
6. Расширение `scheduler_service.py` — evening_report_job

### Фаза E: File Management (RAG-основа)

1. Миграция `009_user_files.sql`
2. `models/user_file.py`
3. `storage/user_file_storage.py`
4. `services/file_service.py`
5. `routes/files.py`
6. Обновление `rag_tool.py` (stub → интеграция с file_service)

> RAG-индексация (vector store) реализуется вторым специалистом. FileService предоставляет интерфейс.

---

## SQL миграций

### 005_tasks.sql

```sql
-- Migration 005: Task Management
-- Creates tasks table for employee task tracking

CREATE TABLE IF NOT EXISTS tasks (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    org_id UUID NOT NULL REFERENCES organizations(id),
    title VARCHAR(500) NOT NULL,
    description TEXT,
    status VARCHAR(20) NOT NULL DEFAULT 'created',
    assignee_user_id UUID NOT NULL REFERENCES users(id),
    creator_user_id UUID REFERENCES users(id),
    role_id UUID REFERENCES roles(id),
    deadline TIMESTAMP WITH TIME ZONE,
    source_chat_id UUID REFERENCES chats(id),
    source_message_id UUID REFERENCES messages(id),
    is_active BOOLEAN DEFAULT true,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

-- Основной индекс: задачи сотрудника по статусу
CREATE INDEX IF NOT EXISTS idx_tasks_assignee
    ON tasks(assignee_user_id, status)
    WHERE is_active = true;

-- Задачи по организации
CREATE INDEX IF NOT EXISTS idx_tasks_org
    ON tasks(org_id)
    WHERE is_active = true;

-- Задачи по создателю (руководителю)
CREATE INDEX IF NOT EXISTS idx_tasks_creator
    ON tasks(creator_user_id)
    WHERE is_active = true;

-- Задачи по роли
CREATE INDEX IF NOT EXISTS idx_tasks_role
    ON tasks(role_id)
    WHERE is_active = true;

-- Активные задачи для scheduler (проверка overdue)
CREATE INDEX IF NOT EXISTS idx_tasks_deadline
    ON tasks(deadline)
    WHERE is_active = true AND deadline IS NOT NULL AND status NOT IN ('done', 'overdue');

COMMENT ON TABLE tasks IS 'Employee tasks managed by AI roles. Created via chat (@@mention) or UI.';
COMMENT ON COLUMN tasks.status IS 'created | in_progress | done | overdue';
COMMENT ON COLUMN tasks.assignee_user_id IS 'Employee assigned to this task';
COMMENT ON COLUMN tasks.creator_user_id IS 'Manager who created the task (NULL if created by AI role from chat)';
COMMENT ON COLUMN tasks.role_id IS 'AI role of the assignee that manages this task';
COMMENT ON COLUMN tasks.source_chat_id IS 'Chat where the task was created (if via @@mention)';
COMMENT ON COLUMN tasks.source_message_id IS 'Message that triggered task creation';
```

### 006_task_polls.sql

```sql
-- Migration 006: Task Polls
-- Morning polls for employees to report task status

CREATE TABLE IF NOT EXISTS task_polls (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    org_id UUID NOT NULL REFERENCES organizations(id),
    assignee_user_id UUID NOT NULL REFERENCES users(id),
    poll_date DATE NOT NULL,
    status VARCHAR(20) NOT NULL DEFAULT 'pending',
    responses JSONB DEFAULT '[]',
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    completed_at TIMESTAMP WITH TIME ZONE,
    expires_at TIMESTAMP WITH TIME ZONE
);

-- Один опрос на сотрудника в день
CREATE UNIQUE INDEX IF NOT EXISTS idx_task_polls_unique_daily
    ON task_polls(assignee_user_id, poll_date);

-- Поиск опросов сотрудника
CREATE INDEX IF NOT EXISTS idx_task_polls_assignee
    ON task_polls(assignee_user_id, poll_date DESC);

-- Pending опросы для scheduler (expire check)
CREATE INDEX IF NOT EXISTS idx_task_polls_pending
    ON task_polls(status, expires_at)
    WHERE status = 'pending';

-- Опросы по организации и дате (для вечернего отчёта)
CREATE INDEX IF NOT EXISTS idx_task_polls_org_date
    ON task_polls(org_id, poll_date);

COMMENT ON TABLE task_polls IS 'Daily morning polls for employees. One per employee per day.';
COMMENT ON COLUMN task_polls.status IS 'pending | completed | expired';
COMMENT ON COLUMN task_polls.responses IS 'JSON array: [{task_id, new_status, comment}]';
```

### 007_task_reports.sql

```sql
-- Migration 007: Task Reports
-- Evening AI-generated reports for managers

CREATE TABLE IF NOT EXISTS task_reports (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    org_id UUID NOT NULL REFERENCES organizations(id),
    generated_for_user_id UUID NOT NULL REFERENCES users(id),
    generated_by_role_id UUID REFERENCES roles(id),
    report_date DATE NOT NULL,
    content TEXT NOT NULL,
    task_summaries JSONB DEFAULT '[]',
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

-- Отчёты руководителя по дате
CREATE INDEX IF NOT EXISTS idx_task_reports_user
    ON task_reports(generated_for_user_id, report_date DESC);

-- Отчёты организации
CREATE INDEX IF NOT EXISTS idx_task_reports_org
    ON task_reports(org_id, report_date DESC);

COMMENT ON TABLE task_reports IS 'AI-generated evening reports for managers. Aggregates task poll responses.';
COMMENT ON COLUMN task_reports.generated_for_user_id IS 'Manager who receives this report';
COMMENT ON COLUMN task_reports.content IS 'AI-generated human-readable report text';
COMMENT ON COLUMN task_reports.task_summaries IS 'Structured data: [{task_id, title, assignee_user_id, assignee_name, old_status, new_status, employee_comment, poll_completed}]';
```

### 008_in_app_notifications.sql

```sql
-- Migration 008: In-App Notifications
-- Bell icon notifications for tasks, polls, reports, mentions

CREATE TABLE IF NOT EXISTS in_app_notifications (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID NOT NULL REFERENCES users(id),
    org_id UUID NOT NULL REFERENCES organizations(id),
    type VARCHAR(30) NOT NULL,
    title VARCHAR(500) NOT NULL,
    content TEXT,
    reference_type VARCHAR(30),
    reference_id UUID,
    is_read BOOLEAN DEFAULT false,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

-- Основной индекс: уведомления пользователя (непрочитанные первыми)
CREATE INDEX IF NOT EXISTS idx_in_app_notif_user
    ON in_app_notifications(user_id, is_read, created_at DESC);

-- Быстрый count непрочитанных
CREATE INDEX IF NOT EXISTS idx_in_app_notif_unread
    ON in_app_notifications(user_id)
    WHERE is_read = false;

COMMENT ON TABLE in_app_notifications IS 'In-app bell notifications. Separate from external notification channels (telegram/email).';
COMMENT ON COLUMN in_app_notifications.type IS 'new_task | poll | report | mention | task_status_change | system';
COMMENT ON COLUMN in_app_notifications.reference_type IS 'task | task_poll | task_report | message | null';
COMMENT ON COLUMN in_app_notifications.reference_id IS 'ID of the related entity';
```

### 009_user_files.sql

```sql
-- Migration 009: User Files for RAG
-- File metadata in PostgreSQL, binary data via StorageAdapter (local FS or S3)
-- Files belong to users; user's AI role accesses all user's files

CREATE TABLE IF NOT EXISTS user_files (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID NOT NULL REFERENCES users(id),
        -- сотрудник, которому принадлежит файл
    org_id UUID NOT NULL REFERENCES organizations(id),
    uploaded_by_user_id UUID NOT NULL REFERENCES users(id),
        -- руководитель, загрузивший файл
    storage_key VARCHAR(500) NOT NULL,
        -- ключ в хранилище: {org_id}/{user_id}/{file_id}.{ext}
        -- для local FS: путь относительно base_dir
        -- для S3: object key в бакете
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

-- Файлы пользователя (для RAG-фильтрации)
CREATE INDEX IF NOT EXISTS idx_user_files_user
    ON user_files(user_id)
    WHERE is_active = true;

-- Файлы организации
CREATE INDEX IF NOT EXISTS idx_user_files_org
    ON user_files(org_id);

-- Pending файлы для индексации
CREATE INDEX IF NOT EXISTS idx_user_files_pending_rag
    ON user_files(rag_status)
    WHERE rag_status IN ('pending', 'indexing');

COMMENT ON TABLE user_files IS 'File metadata. Binary data stored via StorageAdapter (local FS or S3). Key format: {org_id}/{user_id}/{file_id}.{ext}';
COMMENT ON COLUMN user_files.user_id IS 'Employee who owns the file (role accesses these)';
COMMENT ON COLUMN user_files.uploaded_by_user_id IS 'Manager who uploaded the file';
COMMENT ON COLUMN user_files.storage_key IS 'Storage key: {org_id}/{user_id}/{file_id}.{ext}. Same format for local FS and S3.';
COMMENT ON COLUMN user_files.rag_status IS 'pending | indexing | indexed | failed';
COMMENT ON COLUMN user_files.file_type IS 'pdf | docx';
```

---

## Конфигурация (.env)

Новые переменные для Alpha:

```env
# Task Scheduler
TASK_MORNING_POLL_CRON=0 9 * * 1-5
TASK_EVENING_REPORT_CRON=0 18 * * 1-5
TASK_POLL_EXPIRE_HOURS=10

# File storage backend: "local" или "s3"
STORAGE_BACKEND=local
STORAGE_LOCAL_DIR=/var/lib/rugpt/uploads

# Для S3 (потом, когда поднимем SeaweedFS):
# S3_ENDPOINT=http://localhost:8333
# S3_ACCESS_KEY=...
# S3_SECRET_KEY=...
# S3_BUCKET=rugpt-files

# File upload limits
FILE_MAX_SIZE_MB=50
FILE_ALLOWED_TYPES=pdf,docx
```

---

## Обновление role.tools

После миграций нужно обновить tools у ролей, которым необходим доступ к задачам:

```sql
-- Добавить task tools ко всем активным ролям
UPDATE roles
SET tools = tools || '["task_create", "task_query", "task_update"]'::jsonb
WHERE is_active = true
  AND NOT tools @> '["task_create"]'::jsonb;
```

> Это можно включить в миграцию 005 или выполнить отдельно после верификации.
