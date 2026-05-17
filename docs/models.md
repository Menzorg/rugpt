# RuGPT Models

Модели данных для RuGPT Engine.

## Organization (Организация)

**Файл:** `src/engine/models/organization.py`

Представляет организацию (tenant) в multi-tenant системе.

```python
@dataclass
class Organization:
    id: UUID
    name: str                       # "Acme Corp"
    slug: str                       # "acme-corp" (URL-safe)
    description: Optional[str]
    timezone: str                   # "Europe/Moscow" (IANA, для SchedulerService)
    is_active: bool
    created_at: datetime
    updated_at: datetime
```

**Особенности:**
- Все данные (пользователи, роли, чаты) изолированы по организации
- `slug` -- уникальный URL-safe идентификатор
- `timezone` -- используется SchedulerService для per-org morning/evening jobs
- Soft delete через `is_active`

---

## User (Пользователь)

**Файл:** `src/engine/models/user.py`

```python
@dataclass
class User:
    id: UUID
    org_id: UUID
    name: str                       # "Роман Петрович"
    username: str                   # "roman_petrovich" (для @ mentions)
    email: str
    password_hash: Optional[str]    # bcrypt hash
    role_id: Optional[UUID]         # Назначенная AI-роль
    is_admin: bool
    is_system: bool                 # Системный AI-пользователь
    is_active: bool
    avatar_url: Optional[str]
    created_at: datetime
    updated_at: datetime
    last_seen_at: Optional[datetime]
```

**is_system:**
- Системные пользователи представляют AI-ассистентов (AI GPT-4, AI Qwen, AI Claude)
- Создаются через миграцию 003_system_user.sql
- Не имеют пароля, не могут логиниться

**Mentions:**
- `@roman_petrovich` -- нотификация пользователю (MentionType.USER)
- `@@roman_petrovich` -- вызов AI-роли пользователя (MentionType.AI_ROLE)

---

## Role (AI-Агент)

**Файл:** `src/engine/models/role.py`

AI-агент с определённым поведением, инструментами и типом графа.

```python
@dataclass
class Role:
    id: UUID
    org_id: UUID
    name: str                       # "Юрист"
    code: str                       # "lawyer" (уникален в org)
    description: Optional[str]
    system_prompt: str              # Fallback промпт (в БД)
    rag_collection: Optional[str]
    model_name: str                 # "hosted_vllm/google/gemma-4-31B-it" (LiteLLM alias)
    agent_type: str                 # "simple" | "chain" | "multi_agent"
    agent_config: dict              # Конфигурация графа (JSONB)
    tools: List[str]                # ["calendar_create", "rag_search", ...]
    prompt_file: Optional[str]      # "lawyer.md" -- путь к файлу промпта
    is_active: bool
    created_at: datetime
    updated_at: datetime
```

**Роли предсозданы** через миграции/seed. CRUD через API убран.

**agent_type:**
- `simple` -- прямой вызов LLM (без tools) или ReAct agent (с tools)
- `chain` -- последовательные шаги из `agent_config["steps"]`
- `multi_agent` -- LangGraph StateGraph из `agent_config["graph"]`

**prompt_file:**
- Путь к файлу в `src/engine/prompts/` (напр. `lawyer.md`)
- Приоритет: prompt_file > system_prompt (fallback)
- Файлы кешируются через PromptCache, сброс через admin API

---

## Chat (Чат)

**Файл:** `src/engine/models/chat.py`

```python
class ChatType(str, Enum):
    DIRECT = "direct"      # Прямые сообщения (включая user <-> system user)
    TASK = "task"          # Чат задачи (auto-create, item 11). participants = {creator, assignee}
    PROJECT = "project"    # Чат проекта (auto-create при первой задаче, item 11)

@dataclass
class Chat:
    id: UUID
    org_id: UUID
    type: ChatType
    name: Optional[str]
    participants: List[UUID]
    created_by: Optional[UUID]
    task_id: Optional[UUID]        # если type=TASK (item 11)
    project_id: Optional[UUID]     # если type=PROJECT (item 11)
    is_active: bool
    created_at: datetime
    updated_at: datetime
    last_message_at: Optional[datetime]
```

**Legacy типы** `main` / `group` — удалены миграцией 017, смигрированы в DIRECT.

---

## Message (Сообщение)

**Файл:** `src/engine/models/message.py`

```python
class SenderType(str, Enum):
    USER = "user"
    AI_ROLE = "ai_role"

class MentionType(str, Enum):
    USER = "user"
    AI_ROLE = "ai_role"

@dataclass
class Mention:
    type: MentionType
    user_id: UUID
    username: str
    position: int

@dataclass
class Message:
    id: UUID
    chat_id: UUID
    sender_type: SenderType
    sender_id: UUID
    content: str
    mentions: List[Mention]
    reply_to_id: Optional[UUID]
    ai_is_valid: Optional[bool]     # None=pending review, True=approved, False=rejected
    ai_edited: bool                 # AI-ответ был отредактирован пользователем
    is_deleted: bool
    created_at: datetime
    updated_at: datetime
```

**ai_is_valid (tri-state):**
- `None` -- ожидает проверки (по умолчанию для AI-ответов)
- `True` -- одобрен пользователем (автоматически для user messages)
- `False` -- отклонён пользователем (создаётся correction rule)

---

## CalendarEvent (Календарное событие)

**Файл:** `src/engine/models/calendar_event.py`

```python
@dataclass
class CalendarEvent:
    id: UUID
    role_id: UUID                   # Роль-агент для проактивного запуска
    org_id: UUID
    title: str
    description: Optional[str]
    event_type: str                 # "one_time" | "recurring"
    scheduled_at: Optional[datetime]    # Для one_time
    cron_expression: Optional[str]      # Для recurring ("0 10 * * 4")
    next_trigger_at: Optional[datetime] # Предвычисленное время
    last_triggered_at: Optional[datetime]
    trigger_count: int
    source_chat_id: Optional[UUID]      # Откуда событие создано
    source_message_id: Optional[UUID]
    metadata: dict                      # Доп. данные (JSONB)
    created_by_user_id: Optional[UUID]
    is_active: bool
    created_at: datetime
    updated_at: datetime
```

**event_type:**
- `one_time` -- срабатывает один раз в `scheduled_at`, затем деактивируется
- `recurring` -- cron-выражение, после срабатывания пересчитывается `next_trigger_at` через croniter

---

## NotificationChannel (Канал уведомлений)

**Файл:** `src/engine/models/notification.py`

```python
@dataclass
class NotificationChannel:
    id: UUID
    user_id: UUID
    org_id: UUID
    channel_type: str               # "telegram" | "email" | "chat"
    config: dict                    # {"chat_id": "..."} или {"email": "..."}
    is_enabled: bool
    is_verified: bool               # Подтверждён ли канал
    priority: int                   # Выше = пробуется первым
    created_at: datetime
    updated_at: datetime
```

- UNIQUE(user_id, channel_type) -- один канал каждого типа на пользователя

---

## NotificationLog (Лог уведомлений)

**Файл:** `src/engine/models/notification.py`

```python
@dataclass
class NotificationLog:
    id: UUID
    user_id: UUID
    channel_type: str
    event_id: Optional[UUID]        # Календарное событие
    role_id: Optional[UUID]         # Роль-агент
    content: str                    # Текст уведомления
    status: str                     # "pending" | "sent" | "failed"
    attempts: int
    error_message: Optional[str]
    created_at: datetime
    updated_at: datetime
```

---

## Task (Задача)

**Файл:** `src/engine/models/task.py`

```python
@dataclass
class Task:
    id: UUID
    org_id: UUID
    title: str
    description: Optional[str]
    status: str                     # "created" | "in_progress" | "awaiting_review" | "done" | "cancelled" | "overdue"
    assignee_user_id: UUID
    deadline: Optional[datetime]
    created_by_user_id: UUID        # item 9: кто автор задачи (для флоу take/mark-done/accept/reject)
    project_id: Optional[UUID]      # item 11: группировка в проект
    awaiting_review_at: Optional[datetime]       # item 9: когда assignee нажал mark-done
    proposed_deadline: Optional[datetime]        # item 9: предложенный assignee новый дедлайн
    proposed_deadline_by: Optional[UUID]         # item 9: кто предложил
    is_active: bool
    created_at: datetime
    updated_at: datetime
```

**Статусный флоу (item 9):**

```
created  ──▶  in_progress  ──▶  awaiting_review  ──▶  done
   │             ▲                      │
   │             └──────── reject ──────┘   (возврат с комментарием creator'а)
   └──▶ cancelled
```

`overdue` — вычисляемый статус (scheduler `check_overdue()` помечает задачи с просроченным `deadline`).

---

## TaskPoll (Утренний опрос)

**Файл:** `src/engine/models/task_poll.py`

```python
@dataclass
class TaskPoll:
    id: UUID
    org_id: UUID
    assignee_user_id: UUID
    poll_date: date
    status: str                     # "pending" | "completed" | "expired"
    responses: Optional[List[dict]] # [{task_id, new_status, comment}]
    created_at: datetime
    completed_at: Optional[datetime]
    expires_at: Optional[datetime]
```

- UNIQUE(assignee_user_id, poll_date) -- один опрос в день

---

## TaskReport (Вечерний отчёт)

**Файл:** `src/engine/models/task_report.py`

```python
@dataclass
class TaskReport:
    id: UUID
    org_id: UUID
    generated_for_user_id: UUID     # Руководитель
    report_date: date
    content: str                    # Текст отчёта (plain text)
    task_summaries: Optional[List[dict]]  # [{task_id, assignee_user_id, assignee_name, new_status, employee_comment, poll_completed}]
    created_at: datetime
```

---

## InAppNotification (In-app уведомление)

**Файл:** `src/engine/models/in_app_notification.py`

```python
@dataclass
class InAppNotification:
    id: UUID
    user_id: UUID
    org_id: UUID
    type: str                       # "new_task" | "poll" | "report" | "mention" | "task_status_change" | "system"
    title: str
    content: str
    reference_type: Optional[str]   # "task" | "poll" | "report" | ...
    reference_id: Optional[UUID]
    is_read: bool
    created_at: datetime
```

---

## UserFile (Файл пользователя)

**Файл:** `src/engine/models/user_file.py`

```python
@dataclass
class UserFile:
    id: UUID
    user_id: UUID
    org_id: UUID
    uploaded_by_user_id: UUID
    storage_key: str                # "{org_id}/{user_id}/{file_id}.{ext}"
    original_filename: str
    file_type: str                  # Расширение (pdf, docx, xlsx...)
    file_size: int                  # Байты
    content_hash: str               # SHA-256 для дедупликации
    summary: str                    # LLM-резюме (RAG)
    is_table: bool                  # Табличный документ
    is_public: bool                 # Виден всем в организации
    rag_status: str                 # "pending" | "indexing" | "indexed" | "failed" | "unindexed"
    rag_error: Optional[str]
    indexed_at: Optional[datetime]
    is_active: bool
    created_at: datetime
    updated_at: datetime
```

---

## CorrectionRule (Правило коррекции AI)

**Файл:** `src/engine/models/correction_rule.py`

```python
@dataclass
class CorrectionRule:
    id: UUID
    role_id: UUID
    org_id: UUID
    original_message_id: UUID       # Вопрос пользователя
    ai_message_id: UUID             # Неправильный ответ AI
    chat_id: UUID
    user_question: str
    ai_answer: str
    correction_text: str            # Текст коррекции от пользователя
    rule_text: str                  # Сгенерированное LLM правило
    created_by_user_id: UUID
    is_active: bool
    created_at: datetime
    updated_at: datetime
```

Создаётся при отклонении AI-ответа. `rule_text` генерируется через `rule_generator` LangGraph graph.

---

## Project (item 11)

**Файл:** `src/engine/models/project.py`

Группировка задач. Создаётся head/admin. При первой задаче автоматически создаётся `PROJECT`-чат с участниками всех задач проекта.

```python
@dataclass
class Project:
    id: UUID
    org_id: UUID
    name: str
    description: Optional[str]
    created_by_user_id: UUID
    is_active: bool
    created_at: datetime
    updated_at: datetime
```

---

## TaskEvent (item 11 audit trail)

**Файл:** `src/engine/models/task_event.py`

Лог изменений задачи (создание, статусные переходы, deadline-negotiation). Отделён от `messages`, чтобы UI мог рендерить timeline независимо от чата.

```python
@dataclass
class TaskEvent:
    id: UUID
    task_id: UUID
    actor_user_id: Optional[UUID]   # кто инициировал (None для системных)
    event_type: str                 # "created" | "took" | "marked_done" | "accepted" |
                                    # "rejected" | "deadline_changed" | "deadline_proposed" |
                                    # "deadline_proposal_accepted" | "deadline_proposal_rejected"
    payload: dict                   # JSONB с деталями (comment, old/new value, ...)
    created_at: datetime
```

---

## AgentRun (item 10 async idempotency)

**Файл:** `src/engine/models/agent_run.py`

Запись о запуске агента через Kafka `agent.requests`. Атомарный CAS `pending → running` защищает от дублирования при Kafka redelivery (at-least-once).

```python
@dataclass
class AgentRun:
    request_id: UUID                # PK, = Kafka message key
    chat_id: UUID
    org_id: UUID
    trigger_user_id: UUID
    role_id: UUID
    mention_message_id: UUID
    status: str                     # "pending" | "running" | "done" | "failed"
    result_message_id: Optional[UUID]
    error: Optional[str]
    created_at: datetime
    updated_at: datetime
```

Атомарная защита в `AgentRunStorage.mark_running`: `UPDATE WHERE status='pending' RETURNING` — выигрывает ровно один вызов.

---

## Department (item 8)

**Файл:** `src/engine/models/department.py`

Плоский список отделов организации + симметричные правила видимости (кто из какого отдела может видеть/упоминать юзеров из какого).

```python
@dataclass
class Department:
    id: UUID
    org_id: UUID
    name: str
    head_user_id: Optional[UUID]
    is_active: bool
    created_at: datetime
    updated_at: datetime

@dataclass
class DepartmentVisibility:
    id: UUID
    org_id: UUID
    department_a_id: UUID
    department_b_id: UUID           # симметричное правило: A видит B = B видит A
    created_at: datetime
```

`DepartmentService.get_visible_user_ids(viewer_user_id)` — резолвит множество видимых юзеров по правилам.

---

## UserDevice (Zero Trust)

**Файл:** `src/engine/models/user_device.py` (или прямо в `device_storage.py`)

Публичные ключи устройств (ECDSA P-256). Приватный ключ — только в IndexedDB браузера, non-extractable.

```python
@dataclass
class UserDevice:
    id: UUID
    user_id: UUID
    device_name: Optional[str]      # произвольное, для UX
    public_key_pem: str             # PEM-сериализованный ECDSA P-256 pubkey
    created_at: datetime
```

Используется `CryptoService.verify_signature`: итерируем по всем активным устройствам юзера, проверяем ECDSA — любой валидный public key из списка подтверждает запрос.

---

## RAG Models

**Файл:** `src/engine/models/rag.py`

```python
@dataclass
class RelatedDoc:
    """Результат doc-level поиска"""
    file_id: str
    org_id: str
    user_id: Optional[str]
    doc_title: str
    summary: str
    uploaded_at: Optional[datetime]
    created_at: Optional[date]
    vec_dist: float
    tsv_score: float
    mode_used: str                  # "concrete" | "abstract"

@dataclass
class ChunkSearchResult:
    """Результат chunk-level поиска"""
    chunk_id: str
    file_id: str
    chunk_text: str
    vec_dist: float
    tsv_score: float
    r_vec: Optional[int]
    r_tsv: Optional[int]
    final_rank: Optional[float]
    source_type: str                # "chunk" | "table_row"
```

---

## Связи между моделями

```
Organization (1)
    |
    +-- Role (*)                    # AI-агенты организации
    |       |
    |       +-- CalendarEvent (*)   # События привязаны к роли
    |       +-- CorrectionRule (*)  # Правила коррекции роли
    |
    +-- Department (*)              # item 8: плоский список отделов
    |       |
    |       +-- DepartmentVisibility (*)  # симметричные правила видимости
    |
    +-- User (*)                    # Пользователи организации
    |       |
    |       +-- Role (0..1)                 # Назначенная роль
    |       +-- Department (0..1)           # item 8
    |       +-- NotificationChannel (*)     # Каналы уведомлений
    |       +-- UserDevice (*)              # ECDSA P-256 pubkeys (Zero Trust)
    |       +-- Task (*)                    # Задачи (как assignee и creator, item 9)
    |       +-- TaskPoll (*)                # Утренние опросы
    |       +-- TaskReport (*)              # Вечерние отчёты (как руководитель)
    |       +-- UserFile (*)                # Файлы пользователя
    |       +-- InAppNotification (*)       # In-app уведомления
    |
    +-- Project (*)                 # item 11: группировка задач
    |       |
    |       +-- Task (*)                    # tasks.project_id
    |       +-- Chat (type=PROJECT)         # auto-created
    |
    +-- Chat (*)                    # Чаты организации (DIRECT / TASK / PROJECT)
            |
            +-- Message (*)         # Сообщения в чате

Task
  +-- TaskEvent (*)                 # item 11: audit trail
  +-- Chat (type=TASK)              # item 11: auto-created

AgentRun (*)                        # item 10: async idempotency для Kafka agent.requests
NotificationLog                     # лог доставки (user_id, event_id, role_id)
UserFile -> chunks / tables_rows_chunks  # RAG-индекс (pgvector 1024-dim, HNSW)
```

---

## UserFileFolder

**Файл:** `src/engine/models/user_file_folder.py`

Личная папка файлов пользователя. Adjacency list через `parent_folder_id`.

```python
@dataclass
class UserFileFolder:
    id: UUID
    user_id: UUID                            # владелец (личные папки, не org-shared)
    org_id: UUID                             # multi-tenancy
    parent_folder_id: Optional[UUID]         # NULL = root
    name: str                                # case-insensitive unique per parent
    is_active: bool                          # soft-delete
    created_at: datetime
    updated_at: datetime
```

**Изменения в `UserFile`:**
- `folder_id: Optional[UUID] = None` — NULL = root. `folder.user_id` должен совпадать с `file.user_id` (service-enforced).

**Связи:**
```
UserFileFolder
    +-- UserFileFolder (children через parent_folder_id self-FK)
    +-- UserFile (через user_files.folder_id)
```

Spec/plan: `docs/superpowers/specs/2026-05-12-file-folders-design.md`, `docs/superpowers/plans/2026-05-12-file-folders.md`.

