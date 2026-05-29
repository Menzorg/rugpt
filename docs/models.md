# RuGPT Models

Модели данных для RuGPT Engine. Все модели — dataclass'ы в `src/engine/models/`.

## Organization (Организация)

**Файл:** `models/organization.py`

Представляет организацию (tenant) в multi-tenant системе.

```python
@dataclass
class Organization:
    id: UUID
    name: str                       # "Acme Corp"
    slug: str                       # "acme-corp" (URL-safe)
    description: Optional[str]
    timezone: str                   # "Europe/Moscow" (IANA, для SchedulerService)
    org_context: str                # Описание оргструктуры для AI-промптов
    is_active: bool
    accountant_user_id: Optional[UUID]  # юзер, помечающий счета processed (migration 044)
    created_at: datetime
    updated_at: datetime
```

**Особенности:**
- Все данные (пользователи, роли, чаты) изолированы по организации
- `slug` -- уникальный URL-safe идентификатор
- `timezone` -- используется SchedulerService для per-org morning/evening jobs
- `org_context` -- свободный текст про структуру компании, подмешивается в промпты агентов
- Soft delete через `is_active`

---

## User (Пользователь)

**Файл:** `models/user.py`

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
    department_id: Optional[UUID]   # Отдел (FK departments)
    department_name: Optional[str]  # Имя отдела (join из departments на чтении, не колонка users)
    is_head: bool                   # Руководитель отдела
    is_active: bool
    avatar_url: Optional[str]
    created_at: datetime
    updated_at: datetime
    last_seen_at: Optional[datetime]
```

**is_system:**
- Системные пользователи представляют AI-ассистентов
- Не имеют пароля, не могут логиниться

**Отделы (item 8):**
- `department_id` -- членство в одном отделе
- `department_name` -- НЕ хранится в `users`, подтягивается JOIN'ом при чтении (нет в `from_dict`)
- `is_head` -- признак руководителя отдела (глава отдела трекается здесь, не в `departments`)

**Mentions:**
- `@roman_petrovich` -- нотификация пользователю (MentionType.USER)
- `@@roman_petrovich` -- вызов AI-роли пользователя (MentionType.AI_ROLE)

---

## Role (AI-Агент)

**Файл:** `models/role.py`

AI-агент с определённым поведением, инструментами и типом графа.

```python
@dataclass
class Role:
    id: UUID
    org_id: UUID
    name: str                       # "Юрист"
    code: str                       # "lawyer" (уникален в org)
    description: Optional[str]
    as_subagent_description: str    # Описание роли, когда она подключена как сабагент
    system_prompt: str              # Fallback промпт (в БД)
    rag_collection: Optional[str]
    model_name: str                 # default в dataclass = "qwen2.5:7b"
    agent_type: str                 # default "simple"
    agent_config: dict              # Конфигурация графа (JSONB)
    tools: List[str]                # ["calendar", "rag_search", ...]
    prompt_file: Optional[str]      # "lawyer.md" -- путь к файлу промпта
    is_active: bool
    created_at: datetime
    updated_at: datetime
```

**Роли предсозданы** через миграции/seed. CRUD через API убран.

**model_name:**
- Дефолт dataclass'а в коде (`models/role.py:39`) — `"qwen2.5:7b"`.
- Но в живой системе значение другое: сид-роли создаются через миграции 019/020 с LiteLLM-именами моделей (напр. `google/gemma-4-31B-it`). Дефолт dataclass'а в реальных строках БД не используется.

**agent_type (валидные значения в живой системе):**
- `simple` -- прямой вызов LLM или ReAct-agent (с tools)
- `supervisor` -- multiagent-оркестрация через роли-сабагенты и handoff-инструменты

(значения `chain` / `multi_agent` в текущей системе не используются.)

**as_subagent_description:**
- Текст, которым роль представляется supervisor'у, когда её подключают как сабагента.

**prompt_file:**
- Путь к файлу в `src/engine/prompts/` (напр. `lawyer.md`)
- Приоритет: prompt_file > system_prompt (fallback)
- Файлы кешируются через PromptCache, сброс через admin API

---

## Chat (Чат)

**Файл:** `models/chat.py`

```python
class ChatType(str, Enum):
    DIRECT = "direct"      # Прямые сообщения (включая user <-> system user)
    TASK = "task"          # Чат задачи (auto-create). participants = {creator, assignee}
    PROJECT = "project"    # Чат проекта (auto-create)
    SUPPORT = "support"    # Чат тикета техподдержки (cross-org)
    POLL = "poll"          # Чат утреннего опроса (AI-диалог по задачам)

@dataclass
class Chat:
    id: UUID
    org_id: UUID
    type: ChatType
    name: Optional[str]
    participants: List[UUID]       # monotonic-grow (audit trail)
    created_by: Optional[UUID]
    task_id: Optional[UUID]        # iff type == TASK
    project_id: Optional[UUID]     # iff type == PROJECT
    support_ticket_id: Optional[UUID]  # iff type == SUPPORT
    poll_id: Optional[UUID]        # iff type == POLL (FK task_polls.id)
    mem_id: Optional[UUID]         # FK memory_snapshots (активная память чата)
    is_active: bool
    created_at: datetime
    updated_at: datetime
    last_message_at: Optional[datetime]
```

**Legacy типы** `main` / `group` — удалены миграцией 017, смигрированы в DIRECT. `_coerce_chat_type` приводит их к DIRECT как fallback.

**SUPPORT (cross-org):** `chat.org_id = requester_org_id`, но оператор из RuGPT Support org добавляется в participants. Видимость через exemption в `ChatService.can_user_access_chat`.

---

## Message (Сообщение)

**Файл:** `models/message.py`

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
    attachments: List[MessageAttachment]  # гидрируются storage'ом из JOIN; в from_dict — raw dicts
    mem_id: Optional[UUID]          # FK memory_snapshots (копируется из чата на insert)
    metadata: dict                  # произвольный JSONB (напр. modal-протокол)
    created_at: datetime
    updated_at: datetime
```

**ai_is_valid (tri-state):**
- `None` -- ожидает проверки (по умолчанию для AI-ответов)
- `True` -- одобрен пользователем (автоматически для user messages)
- `False` -- отклонён пользователем (создаётся correction rule)

**attachments:** в `from_dict` остаются raw-dict'ами (UI-shape); реальные `MessageAttachment`-объекты гидрирует storage-слой из JOIN с `user_files`.

---

## MessageAttachment (Вложение сообщения)

**Файл:** `models/message_attachment.py`

Join-строка между `messages` и `user_files`. Несёт `position` для стабильного порядка отображения.

```python
@dataclass
class MessageAttachment:
    message_id: UUID
    file_id: UUID
    position: int = 0
    file: Optional[UserFile] = None          # гидрируется на чтении
    cloned_by_me_id: Optional[UUID] = None   # id active-клона текущего юзера для этого файла (per-current-user, route-слой)
    cloned_by_me_rag_status: Optional[str] = None  # rag_status клона ('indexed'|'indexing'|'pending'|'failed')
```

`cloned_by_me_id` / `cloned_by_me_rag_status` заполняются per-current-user в route-слое: truthiness = «файл уже есть в моих файлах». Фронт по этому скрывает кнопку «В память».

---

## CalendarEvent (Календарное событие)

**Файл:** `models/calendar_event.py`

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

**Файл:** `models/notification.py`

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

**Файл:** `models/notification.py`

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

**Файл:** `models/task.py`

```python
VALID_STATUSES = {"created", "in_progress", "awaiting_review", "done", "overdue"}

@dataclass
class Task:
    id: UUID
    org_id: UUID
    title: str
    description: Optional[str]
    status: str                     # см. VALID_STATUSES
    assignee_user_id: UUID
    created_by_user_id: Optional[UUID]   # item 9: автор задачи
    deadline: Optional[datetime]
    awaiting_review_at: Optional[datetime]   # item 9: когда assignee нажал mark-done
    proposed_deadline: Optional[datetime]    # item 9: предложенный новый дедлайн
    proposed_deadline_by: Optional[UUID]     # item 9: кто предложил
    project_id: Optional[UUID]      # item 11: группировка в проект
    priority: int                   # default 1
    is_active: bool
    created_at: datetime
    updated_at: datetime
```

**Статусы (`VALID_STATUSES`):** `created`, `in_progress`, `awaiting_review`, `done`, `overdue`.

`cancelled` — НЕ статус задачи; это только тип события (`task_events.event_type`). Деактивация задачи идёт через `is_active`.

`overdue` — вычисляемый статус (scheduler помечает задачи с просроченным `deadline`).

```
created ──▶ in_progress ──▶ awaiting_review ──▶ done
   │            ▲                  │
   │            └──── reject ──────┘   (возврат с комментарием creator'а)
   └──▶ (is_active=false / event "cancelled")
```

---

## TaskParticipant (Участник задачи)

**Файл:** `models/task_participant.py`

Дополнительный соучастник чата задачи. НЕ заменяет `assignee_user_id` / `created_by_user_id` (канонические исполнитель/автор) — даёт extra-юзерам видимость чата задачи. Добавляется head/admin через API.

```python
@dataclass
class TaskParticipant:
    task_id: UUID
    user_id: UUID
    added_at: datetime
    added_by_user_id: Optional[UUID] = None
```

---

## TaskPoll (Утренний опрос)

**Файл:** `models/task_poll.py`

```python
@dataclass
class TaskPoll:
    id: UUID
    org_id: UUID
    assignee_user_id: UUID
    poll_date: date
    status: str                     # "pending" | "completed" | "expired"
    responses: list                 # [{task_id, new_status, comment}]
    created_at: datetime
    completed_at: Optional[datetime]
    expires_at: Optional[datetime]
    summary: Optional[str]          # AI-markdown-резюме после завершения диалога
    task_ids: List[UUID]            # snapshot UUID задач опроса (фиксируется при создании)
```

- UNIQUE(assignee_user_id, poll_date) -- один опрос в день

---

## TaskReport (Вечерний отчёт)

**Файл:** `models/task_report.py`

```python
@dataclass
class TaskReport:
    id: UUID
    org_id: UUID
    generated_for_user_id: UUID     # Руководитель
    report_date: date
    content: str                    # Текст отчёта
    task_summaries: list            # структурированные данные
    created_at: datetime
```

---

## InAppNotification (In-app уведомление)

**Файл:** `models/in_app_notification.py`

```python
@dataclass
class InAppNotification:
    id: UUID
    user_id: UUID
    org_id: UUID
    type: str                       # new_task | poll | report | mention | task_status_change |
                                    # system | daily_admin_briefing | invoice_due
    title: str
    content: Optional[str]
    reference_type: Optional[str]   # task | task_poll | task_report | message
    reference_id: Optional[UUID]
    is_read: bool
    created_at: datetime
    replied: bool                   # computed на чтении: на mention уже ответили reply-to-mention
```

`replied` -- не колонка БД; вычисляется в `InAppNotificationStorage.list_by_user`.

---

## UserFile (Файл пользователя)

**Файл:** `models/user_file.py`

```python
@dataclass
class UserFile:
    id: UUID
    user_id: UUID                   # владелец-сотрудник
    org_id: UUID
    uploaded_by_user_id: UUID       # руководитель
    storage_key: str                # "{org_id}/{user_id}/{file_id}.{ext}"
    original_filename: str
    file_type: str                  # pdf | docx | ...
    file_size: int                  # Байты
    content_hash: Optional[str]     # SHA-256 hex-дайджест
    summary: str                    # LLM-резюме (RAG)
    is_table: bool                  # Табличный документ
    is_public: bool                 # Виден всем в организации
    rag_status: str                 # "pending" | "indexing" | "indexed" | "failed"
    rag_error: Optional[str]
    indexed_at: Optional[datetime]
    is_active: bool
    cloned_from_file_id: Optional[UUID]  # если не NULL — metadata-only клон файла-источника
    folder_id: Optional[UUID]       # NULL = root; folder.user_id должен == self.user_id
    created_at: datetime
    updated_at: datetime
```

---

## UserFileFolder (Папка файлов)

**Файл:** `models/user_file_folder.py`

Личная папка файлов пользователя. Adjacency list через `parent_folder_id`.

```python
@dataclass
class UserFileFolder:
    id: UUID
    user_id: UUID                   # владелец (личные папки, не org-shared)
    org_id: UUID
    parent_folder_id: Optional[UUID]  # NULL = root
    name: str                       # case-insensitive unique per parent
    is_active: bool                 # soft-delete
    created_at: datetime
    updated_at: datetime
```

**Связи:**
```
UserFileFolder
    +-- UserFileFolder (children через parent_folder_id self-FK)
    +-- UserFile (через user_files.folder_id)
```

---

## MemorySnapshot (Снимок памяти агента)

**Файл:** `models/memory_snapshot.py`

Персистентный снимок памяти AI-агента. На него ссылаются `chats.mem_id` (активная память чата), `messages.mem_id` (копия на момент сообщения) и `correction_rules.mem_id`.

```python
@dataclass
class MemorySnapshot:
    id: UUID
    snapshot: str                   # текст памяти
    is_active: bool
    created_at: datetime
    updated_at: datetime
```

---

## CorrectionRule (Правило коррекции AI)

**Файл:** `models/correction_rule.py`

Создаётся при фидбэке пользователя на AI-ответ. Связывает роль, снимок памяти на момент ответа, исходные сообщения и извлечённый урок.

```python
@dataclass
class CorrectionRule:
    id: UUID
    role_id: UUID
    mem_id: Optional[UUID]                       # FK memory_snapshots
    mem_embedding: Optional[List[float]]         # vector(1024)
    src_user_message_id: Optional[UUID]          # FK messages — исходный вопрос
    user_message_embedding: Optional[List[float]]  # vector(1024)
    src_ai_response_id: Optional[UUID]           # FK messages — неверный ответ AI
    user_correction_text: Optional[str]          # текст коррекции от юзера
    extracted_lesson: Optional[str]              # извлечённый LLM урок
    is_active: bool
```

Нет полей `org_id` / `chat_id` / `created_by_user_id` / `rule_text` / таймстемпов — модель сведена к перечисленным выше полям.

---

## Project (item 11)

**Файл:** `models/project.py`

Группировка задач. Создаётся head/admin. Имена можно дублировать внутри org. При первой задаче автоматически создаётся `PROJECT`-чат с участниками всех задач проекта.

```python
@dataclass
class Project:
    id: UUID
    org_id: UUID
    name: str
    description: Optional[str]
    created_by_user_id: Optional[UUID]
    department_id: Optional[UUID]
    is_active: bool
    created_at: datetime
    updated_at: datetime
```

---

## TaskEvent (item 11 audit trail)

**Файл:** `models/task_event.py`

Лог изменений задачи. Отделён от `messages`, чтобы UI рендерил timeline («History» в expand-row) независимо от чата.

```python
@dataclass
class TaskEvent:
    id: UUID
    task_id: UUID
    actor_user_id: Optional[UUID]   # кто инициировал (None для системных)
    event_type: str                 # created | took | marked_done | accepted | rejected |
                                    # deadline_changed | deadline_proposed | ... |
                                    # assignee_changed | project_changed | cancelled | overdue
    payload: dict                   # JSONB с деталями (comment, old/new value, ...)
    created_at: datetime
```

`cancelled` и `overdue` существуют как типы событий, но НЕ как статусы задачи.

---

## AgentRun (item 10 async idempotency)

**Файл:** `models/agent_run.py`

Запись о запуске агента через Kafka `agent.requests`. Атомарный CAS `pending → running` защищает от дублирования при Kafka redelivery (at-least-once).

```python
VALID_AGENT_RUN_STATUSES = {"pending", "running", "done", "failed"}

@dataclass
class AgentRun:
    request_id: UUID                # PK, = Kafka message key
    chat_id: UUID
    user_message_id: Optional[UUID]
    triggering_user_id: Optional[UUID]
    role_code: str
    status: str                     # см. VALID_AGENT_RUN_STATUSES
    result_message_id: Optional[UUID]
    error_message: Optional[str]
    created_at: datetime
    started_at: Optional[datetime]
    finished_at: Optional[datetime]
```

Атомарная защита в `AgentRunStorage.mark_running`: `UPDATE WHERE status='pending' RETURNING` — выигрывает ровно один вызов.

---

## Department (item 8)

**Файл:** `models/department.py`

Плоский список отделов организации (без вложенности) + симметричные правила видимости. Членство — через `users.department_id`, глава — через `users.is_head` (в самой модели Department главы НЕТ).

```python
@dataclass
class Department:
    id: UUID
    org_id: UUID
    name: str
    created_at: datetime
    updated_at: datetime

@dataclass
class DepartmentVisibility:
    id: UUID
    org_id: UUID
    department_a_id: UUID
    department_b_id: UUID           # симметрично: A видит B = B видит A (CHECK a_id < b_id)
    created_at: datetime
```

`DepartmentService.get_visible_user_ids(viewer_user_id)` — резолвит множество видимых юзеров по правилам.

---

## SupportTicket (Тикет техподдержки)

**Файл:** `models/support_ticket.py`

Cross-org сущность: requester из клиентской org, assignee (если назначен) — из RuGPT Support org. Чат тикета имеет `org_id = requester_org_id` и тип `ChatType.SUPPORT`.

```python
class SupportTicketCategory(str, Enum):
    HOW_TO = "how_to"
    BUG = "bug"
    OTHER = "other"

class SupportTicketStatus(str, Enum):
    OPEN = "open"
    IN_PROGRESS = "in_progress"
    CLOSED = "closed"

class ClosedByRole(str, Enum):
    REQUESTER = "requester"
    OPERATOR = "operator"

@dataclass
class SupportTicket:
    id: UUID
    requester_user_id: UUID
    requester_org_id: UUID
    category: SupportTicketCategory       # default HOW_TO
    status: SupportTicketStatus           # default OPEN
    assignee_user_id: Optional[UUID]
    ai_handoff_at: Optional[datetime]
    ai_first_response_at: Optional[datetime]
    closed_at: Optional[datetime]
    closed_by_user_id: Optional[UUID]
    closed_by_role: Optional[ClosedByRole]
    title: Optional[str]
    created_at: datetime
    updated_at: datetime
```

Lifecycle: `open → in_progress` (после take) `→ closed` (любой стороной). Reopen-через-сообщение обрабатывает сервис-слой.

---

## SupportTicketEvent (Audit trail тикета)

**Файл:** `models/support_ticket_event.py`

Append-only: создаётся на каждом переходе состояния, никогда не обновляется/удаляется. Каскадится при удалении тикета.

```python
class SupportTicketEventType(str, Enum):
    CREATED = "created"
    AI_RESPONDED = "ai_responded"
    AI_HANDOFF = "ai_handoff"
    TAKEN = "taken"
    CLOSED = "closed"
    REOPENED = "reopened"
    MESSAGE = "message"

class SupportTicketActorRole(str, Enum):
    REQUESTER = "requester"
    OPERATOR = "operator"
    AI = "ai"
    SYSTEM = "system"

@dataclass
class SupportTicketEvent:
    id: UUID
    ticket_id: UUID
    actor_user_id: UUID
    actor_role: SupportTicketActorRole    # default SYSTEM
    event_type: SupportTicketEventType    # default CREATED
    payload: dict
    created_at: datetime
```

---

## Invoice (Счёт)

**Файл:** `models/invoice.py`

```python
class InvoiceStatus(str, Enum):
    CREATED = "created"
    APPROVED = "approved"
    REJECTED = "rejected"
    PROCESSED = "processed"

@dataclass
class Invoice:
    id: UUID
    org_id: UUID
    file_id: UUID                   # FK user_files
    uploaded_by_user_id: UUID
    due_date: Optional[date]
    status: InvoiceStatus
    approved_by_user_id: Optional[UUID]
    approved_at: Optional[datetime]
    rejected_at: Optional[datetime]
    rejection_reason: Optional[str]
    processed_at: Optional[datetime]
    processed_by_user_id: Optional[UUID]  # обычно organizations.accountant_user_id
    is_active: bool
    created_at: datetime
    updated_at: datetime
```

---

## RAG Models

**Файл:** `models/rag.py`

```python
@dataclass
class RelatedDoc:
    """Результат doc-level поиска (search_related_docs)"""
    file_id: UUID
    org_id: UUID
    user_id: Optional[UUID]
    doc_title: str
    summary: str
    uploaded_at: Optional[datetime]
    created_at: Optional[date]
    vec_dist: Optional[float]
    tsv_score: Optional[float]
    mode_used: Optional[str]        # "concrete" | "abstract"

@dataclass
class ChunkSearchResult:
    """Результат chunk- / table-row-level поиска (search_rag)"""
    chunk_id: UUID
    file_id: UUID
    chunk_text: str
    chunk_index: Optional[int]
    vec_dist: Optional[float]
    tsv_score: Optional[float]
    r_vec: Optional[int]
    r_tsv: Optional[int]
    final_rank: Optional[float]
    source_type: str                # "chunk" | "table_row"

@dataclass
class ChunkRow:
    """Сырая строка таблицы chunks"""
    id: UUID
    file_id: UUID
    chunk_text: str
    metadata: dict[str, Any]
    chunk_index: Optional[int]
```

UUID-поля — настоящие `UUID`, не `str`.

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
    |       +-- Project (0..1 через project.department_id)
    |
    +-- User (*)                    # Пользователи организации
    |       |
    |       +-- Role (0..1)                 # Назначенная роль
    |       +-- Department (0..1)           # users.department_id + is_head
    |       +-- NotificationChannel (*)     # Каналы уведомлений
    |       +-- Task (*)                    # Задачи (assignee / creator, item 9)
    |       +-- TaskPoll (*)                # Утренние опросы
    |       +-- TaskReport (*)              # Вечерние отчёты (как руководитель)
    |       +-- UserFile (*)                # Файлы пользователя
    |       +-- UserFileFolder (*)          # Личные папки
    |       +-- InAppNotification (*)       # In-app уведомления
    |
    +-- Project (*)                 # item 11: группировка задач
    |       |
    |       +-- Task (*)                    # tasks.project_id
    |       +-- Chat (type=PROJECT)         # auto-created
    |
    +-- Invoice (*)                 # счета (FK user_files), accountant_user_id помечает processed
    |
    +-- Chat (*)                    # DIRECT / TASK / PROJECT / SUPPORT / POLL
            |
            +-- Message (*)         # Сообщения в чате
                    +-- MessageAttachment (*)  # join messages <-> user_files
            +-- MemorySnapshot (0..1 через chats.mem_id)

Task
  +-- TaskEvent (*)                 # item 11: audit trail
  +-- TaskParticipant (*)          # доп. участники чата задачи
  +-- Chat (type=TASK)             # item 11: auto-created

SupportTicket (cross-org)
  +-- SupportTicketEvent (*)       # append-only audit trail
  +-- Chat (type=SUPPORT)          # org_id = requester_org_id

AgentRun (*)                        # item 10: async idempotency для Kafka agent.requests
NotificationLog                     # лог доставки (user_id, event_id, role_id)
MemorySnapshot                      # ссылается из chats / messages / correction_rules (mem_id)
UserFile -> chunks / tables_rows_chunks  # RAG-индекс (pgvector 1024-dim, HNSW)
```

---

## Замечания

- **UserDevice** — отдельной dataclass-модели нет. Данные устройств (ECDSA P-256 pubkeys, Zero Trust) обрабатываются как dict'ы в `storage/device_storage.py`.
- `models/__init__.py` ре-экспортит только подмножество: `Organization, User, Role, Chat, ChatType, Message, Mention, SenderType, MentionType, CalendarEvent, NotificationChannel, NotificationLog, MemorySnapshot, CorrectionRule`. Остальные модели импортируются напрямую из своих модулей.
