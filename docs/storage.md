# RuGPT Storage Layer

PostgreSQL 16 + pgvector + pgcrypto. Физически — отдельная KVM-VM `postgres-vm` (B.II.1, `192.168.1.82`), 368 GB RAM, 35 pinned cores, 200+2000 GB диск. **Не** внутри rugpt-container. Auth: scram-sha-256 / md5. Backup: `pg_dump` → NAS + Proxmox snapshot.

### Multi-tenancy

- Большинство таблиц имеют `org_id` — row-level изоляция по организации. FK `org_id → organizations(id)` теперь повсеместно `ON DELETE CASCADE` (миграция 042: удаление организации сносит весь тенант).
- Support-тикеты живут в отдельной системной org `RuGPT Support` и хранят `requester_org_id` отдельно (для кросс-орг тех. поддержки).
- **pgvector — одна общая схема** (не per-role). Изоляция: `org_id AND (is_public OR user_id = viewer)` (admin видит шире — см. 040).
- **Bin-файлы** не в PG — в rugpt-container `/root/rugpt/uploads/{org_id}/{user_id}/{file_id}.{ext}`. В PG только метаданные (`user_files`). Дедуп per-user по `content_hash` (035).

### Vector-конфигурация (RAG)

- Dim: **1024** (Qwen3-Embedding-0.6B через LiteLLM).
- Индекс: **HNSW**.
- Embeddings-модель: `hosted_vllm/Qwen/Qwen3-Embedding-0.6B`.

Подробнее RAG-pipeline — `docs/rag-info.md`.

## Сетевой доступ

Engine (rugpt-container, `192.168.1.81`) → PostgreSQL (`192.168.1.82:5432`) — plaintext TCP по LAN, без TLS (security-пункт 1 из `tech-debt.md`). DSN — `POSTGRES_DSN` в `.env`.

## BaseStorage

**Файл:** `src/engine/storage/base.py`

Базовый класс для всех хранилищ.

```python
class BaseStorage:
    def __init__(self, postgres_dsn: str)

    async def init()                    # Инициализация пула (idempotent)
    async def close()                   # Закрытие соединений

    async def execute(query, *args) -> str          # Выполнить запрос
    async def fetch(query, *args) -> list            # Получить несколько строк
    async def fetchrow(query, *args) -> Record?      # Получить одну строку
    async def fetchval(query, *args) -> Any          # Получить одно значение
```

**Особенности:**
- Асинхронный пул соединений через asyncpg
- Автоматическое переподключение при смене процесса (для gunicorn) — `_init_postgres`
- Retry logic при подключении

---

## OrgStorage

**Файл:** `src/engine/storage/org_storage.py`

```python
class OrgStorage(BaseStorage):
    async def create(org: Organization) -> Organization
    async def get_by_id(org_id: UUID) -> Organization?
    async def get_by_slug(slug: str) -> Organization?
    async def list_all(active_only: bool = True) -> List[Organization]
    async def update(org: Organization) -> Organization
    async def delete(org_id: UUID) -> bool
    async def exists_by_slug(slug: str, exclude_id?: UUID) -> bool
```

`update` пишет в т.ч. `timezone` (014), `org_context` (015), `accountant_user_id` (044).

---

## UserStorage

**Файл:** `src/engine/storage/user_storage.py`

```python
class UserStorage(BaseStorage):
    async def create(user: User) -> User
    async def get_by_id(user_id: UUID) -> User?
    async def get_by_email(email: str) -> User?
    async def get_by_username(username: str, org_id: UUID) -> User?
    async def list_by_org(org_id: UUID, active_only: bool = True) -> List[User]
    async def list_by_role(role_id: UUID) -> List[User]
    async def get_system_users() -> List[User]
    async def get_system_user_by_username(username: str) -> User?
    async def get_system_user_by_model(model_code: str) -> User?
    async def list_admins_by_org(org_id: UUID) -> List[User]
    async def update(user: User) -> User
    async def update_last_seen(user_id: UUID) -> None
    async def assign_role(user_id: UUID, role_id?: UUID) -> bool
    async def delete(user_id: UUID) -> bool
    async def get_certain_users(user_ids: List[UUID]) -> List[User]
    async def exists_by_email(email: str, exclude_id?: UUID) -> bool
    async def exists_by_username(username: str, org_id: UUID, exclude_id?: UUID) -> bool
```

`User` несёт `department_id` и `is_head` (015).

---

## RoleStorage

**Файл:** `src/engine/storage/role_storage.py`

```python
class RoleStorage(BaseStorage):
    async def create(role: Role) -> Role
    async def get_by_id(role_id: UUID) -> Role?
    async def get_by_code(code: str, org_id: UUID) -> Role?
    async def list_by_org(org_id: UUID, active_only: bool = True) -> List[Role]
    async def update(role: Role) -> Role
    async def delete(role_id: UUID) -> bool
    async def exists_by_code(code: str, org_id: UUID, exclude_id?: UUID) -> bool
```

**Особенности:**
- `agent_config` (JSONB) и `tools` (JSONB) парсятся при чтении из БД
- `as_subagent_description` (039) — текст-описание роли как сабагента для supervisor-оркестрации

---

## RoleSubagentStorage

**Файл:** `src/engine/storage/role_subagent_storage.py`

Read-only маппинг supervisor → доступные роли-сабагенты (таблица `role_subagents`, 039).

```python
class RoleSubagentStorage(BaseStorage):
    async def list_available_subagent_roles(role_id: UUID) -> List[Role]
```

---

## ChatStorage

**Файл:** `src/engine/storage/chat_storage.py`

```python
class ChatStorage(BaseStorage):
    async def create(chat: Chat) -> Chat
    async def get_by_id(chat_id: UUID) -> Chat?
    async def get_direct_chat(user1_id: UUID, user2_id: UUID) -> Chat?
    async def is_ai_direct_chat(chat_id: UUID) -> bool
    async def get_attachments(chat_id: UUID, limit?: int) -> List[UUID]   # file_id из вложений чата
    async def list_by_user(user_id, active_only=True, ...) -> List[Chat]
    async def get_by_task_id(task_id: UUID) -> Chat?                       # task-чат (017)
    async def get_by_project_id(project_id: UUID) -> Chat?                 # project-чат (017)
    async def get_by_support_ticket(support_ticket_id: UUID) -> Chat?     # support-чат (026)
    async def get_by_poll_id(poll_id: UUID, ...) -> Chat?                 # poll-чат (029)
    async def list_by_org(org_id: UUID, active_only: bool = True) -> List[Chat]
    async def update(chat: Chat) -> Chat
    async def update_last_message(chat_id: UUID) -> None
    async def update_mem_id(chat_id: UUID, mem_id: UUID) -> None          # снимок памяти (023)
    async def add_participant(chat_id: UUID, user_id: UUID) -> bool
    async def remove_participant(chat_id: UUID, user_id: UUID) -> bool
    async def delete(chat_id: UUID) -> bool
```

**Особенности:**
- `participants` хранится как TEXT[] (массив UUID-строк), поиск через GIN индекс
- `chats.type` ∈ `direct | task | project | support | poll` (эволюция CHECK: 017 добавил task/project, 026 — support, 029 — poll)
- Колонки-ссылки: `task_id`, `project_id` (017), `support_ticket_id` (026), `poll_id` (029), `mem_id` (023)

---

## MessageStorage

**Файл:** `src/engine/storage/message_storage.py`

```python
class MessageStorage(BaseStorage):
    async def create(message: Message) -> Message
    async def get_by_id(message_id: UUID) -> Message?
    async def list_by_chat(chat_id, limit=50, before_id=None) -> List[Message]
    async def list_pending_review(user_id: UUID) -> List[Message]
    async def list_reviewed(user_id: UUID, limit: int = 50) -> List[Message]
    async def update(message: Message) -> Message
    async def validate(message_id: UUID, edited_content?: str) -> Message?
    async def reject(message_id: UUID) -> Message?
    async def delete(message_id: UUID) -> bool
    async def count_by_chat(chat_id: UUID) -> int
    async def messages_exist_from_sender(...) -> bool
    async def find_reply(reply_to_id: UUID, sender_id: UUID) -> Message?
```

**Особенности:**
- `mentions` хранится как JSONB; `metadata` JSONB (043, напр. modal-протокол)
- `mem_id` (023) — привязка к снимку памяти
- Вложения гидрируются через `_hydrate_attachments` (MessageAttachmentStorage)
- Пагинация через cursor (`before_id`); `list_by_chat` отдаёт сообщения в хронологическом порядке (ASC)
- `validate()` ставит `ai_is_valid=true`, опционально обновляет content; `reject()` ставит `ai_is_valid=false` (NULL = pending review)

---

## MessageAttachmentStorage

**Файл:** `src/engine/storage/message_attachment_storage.py`

M:N между `messages` и `user_files` (таблица `message_attachments`, 033).

```python
class MessageAttachmentStorage(BaseStorage):
    async def attach(message_id: UUID, file_ids: List[UUID]) -> None
    async def get_for_message(message_id: UUID) -> List[MessageAttachment]
    async def get_for_messages(message_ids) -> Dict[UUID, List[MessageAttachment]]   # batch для гидрации
    async def is_file_visible_to_user(...) -> bool                                    # проверка доступа
```

---

## ChatReadStateStorage

**Файл:** `src/engine/storage/chat_read_state_storage.py`

Per-user last-read маркер для счётчиков непрочитанного (таблица `chat_read_state`, 036).

```python
class ChatReadStateStorage(BaseStorage):
    async def upsert(chat_id, user_id, last_read_message_id, ...) -> None
    async def get_unread_counts_for_user(user_id, ...) -> Dict[UUID, int]   # {chat_id -> unread}
```

---

## CalendarStorage

**Файл:** `src/engine/storage/calendar_storage.py`

```python
class CalendarStorage(BaseStorage):
    async def create(event: CalendarEvent) -> CalendarEvent
    async def get_by_id(event_id: UUID) -> CalendarEvent?
    async def list_by_org(org_id: UUID, active_only: bool = True) -> List[CalendarEvent]
    async def list_by_role(role_id: UUID, active_only: bool = True) -> List[CalendarEvent]
    async def get_due_events(now?: datetime) -> List[CalendarEvent]
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
    async def update_status(log_id, status, attempts, error_message?) -> NotificationLog?
    async def list_by_user(user_id: UUID, limit=50) -> List[NotificationLog]
    async def list_by_event(event_id: UUID) -> List[NotificationLog]
```

---

## InAppNotificationStorage

**Файл:** `src/engine/storage/in_app_notification_storage.py`

```python
class InAppNotificationStorage(BaseStorage):
    async def create(notification: InAppNotification) -> InAppNotification
    async def get_by_id(notification_id: UUID) -> InAppNotification?
    async def list_by_user(user_id, type?=None, limit=50, offset=0,
                           unread_only=False, replied?=None) -> List[InAppNotification]
    async def count_unread(user_id: UUID) -> int
    async def mark_read(notification_id: UUID) -> bool
    async def mark_all_read(user_id: UUID) -> int
    async def exists_for_user_on_date_in_tz(user_id, type, tz_name) -> bool   # дневная идемпотентность в org-tz
```

**Особенности:**
- `list_by_user` возвращает computed-флаг `replied` per row (юзер ответил на mentioning-сообщение); опц. фильтры `type` / `replied` / `unread_only`
- Сортировка: непрочитанные сверху (`is_read ASC, created_at DESC`)

---

## TaskStorage

**Файл:** `src/engine/storage/task_storage.py`

```python
class TaskStorage(BaseStorage):
    async def create(task: Task) -> Task
    async def get_by_id(task_id: UUID) -> Task?
    async def get_with_creator(task_id: UUID) -> dict?               # + роль/имя создателя
    async def list_by_assignee(user_id, status?) -> List[Task]
    async def list_by_assignee_with_priority(user_id, include_done?) -> List[dict]
    async def list_by_creator_with_assignee(user_id, include_done?) -> List[dict]
    async def list_by_org(org_id, status?) -> List[Task]
    async def list_by_deadline_range(...) -> List[Task]
    async def list_by_created_range(...) -> List[Task]
    async def list_active_with_deadline() -> List[Task]              # scheduler overdue check
    async def list_active_for_polls(assignee_user_id) -> List[Task]
    async def list_distinct_assignees() -> list
    async def update(task: Task) -> Task
    async def deactivate(task_id) -> bool
    async def list_archived_for_user(user_id, org_id, limit?) -> List[dict]   # done | cancelled
    async def count_active_in_project(project_id) -> int
    async def user_has_any_active_task_in_project(...) -> bool
    async def list_by_participant_with_priority(...) -> List[dict]   # задачи где юзер participant (034)
    async def list_done_for_user(user_id) -> List[dict]
    async def text_search(...) -> ...                               # полнотекст по tasks.tsv (027)
    async def get_many_by_ids(ids: List[UUID]) -> Dict[UUID, Task]  # batch для references
```

**Поля `tasks`:** помимо базовых — `created_by_user_id`, `awaiting_review_at`, `proposed_deadline`, `proposed_deadline_by` (016); `project_id` (017); `priority` (031, SMALLINT с CHECK); `tsv` (027, generated tsvector).

---

## TaskParticipantStorage

**Файл:** `src/engine/storage/task_participant_storage.py`

M:N соисполнители задач сверх единственного assignee (таблица `task_participants`, 034).

```python
class TaskParticipantStorage(BaseStorage):
    async def add(task_id, user_id, added_by_user_id?, ...) -> TaskParticipant
    async def remove(task_id: UUID, user_id: UUID) -> bool
    async def list_user_ids(task_id: UUID) -> List[UUID]
    async def list_active_user_dicts(task_id: UUID) -> List[dict]            # [{id, name, ...}]
    async def get_for_tasks(task_ids) -> Dict[UUID, List[dict]]             # batch {task_id -> [{id,name}]}
    async def list_tasks_for_user(user_id, ...) -> List[UUID]
```

---

## ProjectStorage

**Файл:** `src/engine/storage/project_storage.py`

```python
class ProjectStorage(BaseStorage):
    async def create(project: Project) -> Project
    async def get_by_id(project_id: UUID) -> Project?
    async def list_by_org(org_id, include_archived?) -> List[Project]
    async def list_visible_for_user(user_id, ...) -> List[Project]   # с учётом department-видимости
    async def update(project: Project) -> Project
    async def deactivate(project_id: UUID) -> bool
    async def get_many_by_ids(ids) -> Dict[UUID, Project]            # batch для references
```

`projects.department_id` (046) — владеющий отдел, фиксируется отделом создателя.

---

## TaskEventStorage

**Файл:** `src/engine/storage/task_event_storage.py`

```python
class TaskEventStorage(BaseStorage):
    async def create(event: TaskEvent) -> TaskEvent        # JSONB payload
    async def list_by_task(task_id, limit=100) -> List[TaskEvent]   # DESC by created_at
```

---

## AgentRunStorage

**Файл:** `src/engine/storage/agent_run_storage.py`

Idempotency для асинхронных agent executions через атомарный CAS (Kafka redelivery safety).

```python
class AgentRunStorage(BaseStorage):
    async def create(run: AgentRun) -> AgentRun           # status=pending
    async def get(request_id) -> AgentRun?
    async def mark_running(request_id) -> bool            # CAS pending→running, True iff выиграл гонку
    async def mark_done(request_id, result_message_id) -> None
    async def mark_failed(request_id, error: str) -> None
    async def count_failed_by_chat_and_kind(...) -> int   # для circuit-breaker / лимитов
    async def last_failed_at(...) -> datetime?
```

**Статусы:** `pending | running | done | failed`

---

## TaskPollStorage

**Файл:** `src/engine/storage/task_poll_storage.py`

```python
class TaskPollStorage(BaseStorage):
    async def create(poll: TaskPoll) -> TaskPoll
    async def get_by_id(poll_id: UUID) -> TaskPoll?
    async def get_by_user_and_date(user_id, poll_date, ...) -> TaskPoll?
    async def list_by_user(user_id, limit) -> List[TaskPoll]
    async def list_by_org_and_date(...) -> List[TaskPoll]
    async def list_pending_expired(now: datetime) -> List[TaskPoll]
    async def list_pending_today() -> List[TaskPoll]
    async def update(poll: TaskPoll) -> TaskPoll
    async def update_summary(poll_id: UUID, summary: str) -> None
    async def update_status(poll_id: UUID, status: str) -> None
```

`task_polls` несёт `summary` и `task_ids` JSONB (029).

---

## TaskReportStorage

**Файл:** `src/engine/storage/task_report_storage.py`

```python
class TaskReportStorage(BaseStorage):
    async def create(report: TaskReport) -> TaskReport
    async def get_by_id(report_id: UUID) -> TaskReport?
    async def exists_for_user_on_date(...) -> bool          # дневная идемпотентность
    async def list_by_user(user_id, limit) -> List[TaskReport]
    async def list_by_org_and_date(...) -> List[TaskReport]
```

---

## SupportTicketStorage

**Файл:** `src/engine/storage/support_ticket_storage.py`

Helpdesk-тикеты (таблица `support_tickets`, 026). Lifecycle: `open → in_progress (после take) → closed`, плюс AI-first triage.

```python
class SupportTicketStorage(BaseStorage):
    async def create(t: SupportTicket) -> SupportTicket
    async def get_by_id(ticket_id: UUID) -> SupportTicket?
    async def list_by_requester(user_id, ...) -> List[SupportTicket]
    async def list_queue(limit: int = 100) -> List[SupportTicket]     # open, без assignee
    async def list_by_assignee(user_id, ...) -> List[SupportTicket]
    async def take(ticket_id, assignee_user_id, ...) -> SupportTicket?  # взять из очереди
    async def set_ai_first_response(ticket_id: UUID) -> None
    async def set_ai_handoff(ticket_id: UUID) -> None
    async def close_ticket(ticket_id, ...) -> SupportTicket?
    async def reopen(ticket_id: UUID) -> SupportTicket?
```

---

## SupportTicketEventStorage

**Файл:** `src/engine/storage/support_ticket_event_storage.py`

Audit trail тикетов (таблица `support_ticket_events`, 026).

```python
class SupportTicketEventStorage(BaseStorage):
    async def insert(event: SupportTicketEvent) -> SupportTicketEvent
    async def list_by_ticket(ticket_id, ...) -> List[SupportTicketEvent]
```

---

## MemorySnapshotStorage

**Файл:** `src/engine/storage/memory_snapshot_storage.py`

Снимки состояния диалога (текст) — адаптация rptext-механики (таблица `memory_snapshots`, 023). Embeddings снимка хранятся на `correction_rules` (`mem_embedding`), не на самом снимке.

```python
class MemorySnapshotStorage(BaseStorage):
    async def create(snapshot: MemorySnapshot) -> MemorySnapshot
    async def get_by_id(snapshot_id: UUID) -> MemorySnapshot?
    async def list_active() -> List[MemorySnapshot]
    async def update_snapshot(snapshot_id: UUID, snapshot_text: str) -> MemorySnapshot?
    async def deactivate(snapshot_id: UUID) -> bool
    async def delete(snapshot_id: UUID) -> bool
```

---

## CorrectionRuleStorage

**Файл:** `src/engine/storage/correction_rule_storage.py`

Правила коррекции AI на mem-based схеме (`correction_rules` пересоздана в 023, `is_active` добавлен в 025). Семантический поиск через SQL-функцию `search_correction_rules` (024, переписана со scope-фильтрами в 041).

```python
class CorrectionRuleStorage(BaseStorage):
    async def create(rule: CorrectionRule) -> CorrectionRule
    async def get_by_id(rule_id: UUID) -> CorrectionRule?
    async def list_active() -> List[CorrectionRule]
    async def list_by_role(role_id: UUID, active_only: bool = True) -> List[CorrectionRule]
    async def list_by_mem(mem_id: UUID, active_only: bool = True) -> List[CorrectionRule]
    async def update(rule: CorrectionRule) -> CorrectionRule?
    async def update_extracted_lesson(rule_id: UUID, extracted_lesson: str) -> CorrectionRule?
    async def search_by_embeddings(mem_embedding, user_message_embedding,
                                   top_k=5, role_id?, search_looseness=0.75) -> List[CorrectionRule]
    async def deactivate(rule_id: UUID) -> bool
```

---

## InvoiceStorage

**Файл:** `src/engine/storage/invoice_storage.py`

Счета на оплату (таблица `invoices`, 044). Флоу: uploader создаёт счёт со ссылкой на `user_files`-бинарник → admin approve/reject → accountant (`organizations.accountant_user_id`) mark_processed.

```python
class InvoiceStorage(BaseStorage):
    async def create(org_id, file_id, uploaded_by_user_id, due_date, ...) -> Invoice
    async def get_by_id(invoice_id: UUID) -> Invoice?
    async def list_by_org(org_id, status?, ...) -> List[Invoice]
    async def list_for_user(user_id, ...) -> List[Invoice]
    async def update_status(invoice_id, status, ...) -> Invoice?     # created→approved/rejected→processed
    async def list_due_today_or_tomorrow(...) -> List[Invoice]                # scheduler
    async def list_due_today_or_tomorrow_pending_notif(...) -> List[Invoice]  # ещё не уведомлённые сегодня
    async def update_last_notified_for_date(invoice_id: UUID, when: date) -> None
```

`invoices.last_notified_for_date` (045) — дневная идемпотентность напоминаний.

---

## DepartmentStorage

**Файл:** `src/engine/storage/department_storage.py`

Отделы и правила взаимной видимости (таблицы `departments`, `department_visibility`, 015).

```python
class DepartmentStorage(BaseStorage):
    async def create(department: Department) -> Department
    async def get_by_id(department_id: UUID) -> Department?
    async def list_by_org(org_id: UUID) -> List[Department]
    async def update(department_id: UUID, name: str) -> Department?
    async def delete(department_id: UUID) -> bool
    async def create_visibility_rule(...) -> DepartmentVisibility
    async def delete_visibility_rule(rule_id: UUID) -> bool
    async def list_visibility_rules(org_id: UUID) -> List[DepartmentVisibility]
    async def get_visible_department_ids(department_id: UUID) -> Set[UUID]   # симметричный обход пар
```

---

## UserFileStorage

**Файл:** `src/engine/storage/user_file_storage.py`

```python
class UserFileStorage(BaseStorage):
    async def create(file: UserFile) -> UserFile
    async def get_by_id(file_id: UUID) -> UserFile?
    async def get_status(file_id: UUID) -> str?
    async def get_many_by_ids(ids: List[UUID]) -> Dict[UUID, UserFile]
    async def list_by_user(user_id: UUID) -> List[UserFile]
    async def list_by_org(org_id: UUID) -> List[UserFile]
    async def list_pending_indexing() -> List[UserFile]
    async def find_duplicate(user_id: UUID, content_hash: str) -> UserFile?   # per-user дедуп (035)
    async def find_active_clone(user_id: UUID, source_file_id: UUID) -> UserFile?
    async def find_active_clones_by_source(...) -> List[UserFile]            # клоны по cloned_from (033)
    async def update_rag_status(...) -> ...
    async def change_rag_status(file_id: UUID, status: str) -> UserFile?
    async def change_public(file_id: UUID, is_public: bool) -> UserFile?
    async def deactivate(file_id: UUID) -> bool
    # Папки (037):
    async def list_by_user_in_folder(user_id, folder_id) -> List[UserFile]   # IS NOT DISTINCT FROM (NULL=root)
    async def list_by_folder_ids(folder_ids) -> List[UserFile]               # batch для cascade-delete
    async def move_to_folder(file_id, folder_id) -> ...
    async def deactivate_by_folder_ids(folder_ids: List[UUID]) -> List[UUID]
```

**Поля `user_files`:** `content_hash`, `summary`, `summary_embedding vector(1024)`, `is_table`, `is_public`, `tsv` (012); `cloned_from_file_id` (033); `folder_id` (037). Дедуп per-user через unique index по `(user_id, content_hash)` для активных (035).

---

## UserFileFolderStorage

**Файл:** `src/engine/storage/user_file_folder_storage.py`

PostgreSQL CRUD для `user_file_folders` (личные папки файлов, 037). Adjacency list, soft-delete через `is_active`.

```python
class UserFileFolderStorage(BaseStorage):
    async def create(folder: UserFileFolder) -> UserFileFolder       # UniqueViolationError на дубль имени
    async def get_by_id(folder_id: UUID) -> UserFileFolder?
    async def list_by_user(user_id: UUID) -> List[UserFileFolder]
    async def list_children(user_id: UUID, parent_folder_id?: UUID) -> List[UserFileFolder]
    async def list_subtree_ids(folder_id: UUID) -> Set[UUID]         # recursive CTE, включает self
    async def deactivate_subtree(folder_id: UUID) -> List[UUID]      # cascade soft-delete
    async def update(folder: UserFileFolder) -> UserFileFolder?
    async def get_depth(folder_id: UUID) -> int?                     # вверх по parent_folder_id
    async def get_subtree_max_depth(folder_id: UUID) -> int          # вниз до самого глубокого потомка
```

Recursive CTE'ы защищены hard-cap `depth < 20`. Уникальность имени в одном parent — partial unique index с `NULLS NOT DISTINCT` (PG 15+, lower(name)).

---

## DeviceStorage

**Файл:** `src/engine/storage/device_storage.py`

Хранит публичные ECDSA P-256 ключи устройств пользователей для Zero-Trust подписей.

- **Приватный ключ** живёт только в браузере клиента (IndexedDB, `extractable=false`), engine его никогда не видит.
- При логине (`POST /auth/login` с `device_public_key`) publicKey-PEM сохраняется в `user_devices` (idempotent через `ON CONFLICT (user_id, device_public_key)`).
- `SignatureService.verify_request_signature` (см. `services.md`) итерирует по `get_all_public_keys(user_id)` и верифицирует ECDSA через crypto-функцию `verify_device_signature` (не класс `CryptoService`).

```python
class DeviceStorage(BaseStorage):
    async def register_device(user_id, device_public_key, device_name?) -> str?   # device_id
    async def get_device_public_key(user_id, device_id?) -> str?                  # без id → most-recent active
    async def get_all_public_keys(user_id: UUID) -> List[str]
    async def update_last_used(user_id, device_public_key) -> bool
    async def get_user_devices(user_id: UUID) -> List[dict]
    async def deactivate_device(user_id: UUID, device_id: UUID) -> bool           # soft-revoke (is_active=false)
```

`deactivate_device` помечает устройство `is_active = false`; активные ключи везде фильтруются по `is_active = true`.

---

## RAG_store

**Файл:** `src/engine/storage/rag_store.py`

Отдельный storage для RAG-данных. Embedding-размерность валидируется на каждый вызов. Подробнее см. `docs/rag-info.md`.

```python
class RAG_store(BaseStorage):
    def __init__(dsn: str, vector_dim: int)

    # Ingestion
    async def update_user_file_summary(file_id, summary, summary_embedding) -> None
    async def insert_chunks_and_update_document_summary(
        file_id, summary, summary_embedding, chunks, chunk_embeddings) -> None
    async def insert_rows_chunks_and_update_table_summary(
        file_id, summary, summary_embedding, rows_text, row_embeddings) -> None
    async def delete_chunks(file_id) -> bool        # удаляет chunks/rows, rag_status='unindexed'

    # Retrieval
    async def get_doc_by_id(file_id) -> RelatedDoc?
    async def call_search_related_docs(
        org_id, user_id?, query, query_embedding, top_k,
        is_admin=False, filter_user_id?=None, exclude_images=True,
        search_mode='abstract') -> List[RelatedDoc]                # SQL: search_related_docs (040)
    async def call_search_abstract_chunks(file_id, query, query_embedding, top_k)
        -> List[ChunkSearchResult]                                 # SQL: search_rag 'abstract'
    async def call_search_concrete_chunks(file_id, query, query_embedding, top_k, tsv_weight=1)
        -> List[ChunkSearchResult]                                 # SQL: search_rag 'concrete'
    async def get_expanded_context_by_index(file_id, chunk_index, distance=1)
        -> List[ChunkRow]                                          # SQL: get_expanded_context_by_index (032)
    async def get_table_rows_by_range(file_id, row_start, row_end) -> List[ChunkSearchResult]
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
    def __init__(base_dir: str)
    # Хранит файлы на локальной ФС
```

---

## Схема базы данных

Ниже — ключевые таблицы. Точные DDL — в `src/engine/migrations/`.

```sql
-- Организации (001 + 014 timezone + 015 org_context + 044 accountant)
CREATE TABLE organizations (
    id UUID PRIMARY KEY,
    name VARCHAR(255) NOT NULL,
    slug VARCHAR(100) NOT NULL UNIQUE,
    description TEXT,
    timezone VARCHAR(64) NOT NULL DEFAULT 'Europe/Moscow',   -- IANA, 014
    org_context TEXT NOT NULL DEFAULT '',                     -- 015
    accountant_user_id UUID REFERENCES users(id) ON DELETE SET NULL,  -- 044
    is_active BOOLEAN DEFAULT true,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

-- Роли (AI-агенты) (001 + 002 + 039)
CREATE TABLE roles (
    id UUID PRIMARY KEY,
    org_id UUID REFERENCES organizations(id) ON DELETE CASCADE,
    name VARCHAR(255) NOT NULL,
    code VARCHAR(50) NOT NULL,
    description TEXT,
    system_prompt TEXT NOT NULL,
    rag_collection VARCHAR(255),
    model_name VARCHAR(100),
    agent_type VARCHAR(20) NOT NULL DEFAULT 'simple',
    agent_config JSONB NOT NULL DEFAULT '{}',
    tools JSONB NOT NULL DEFAULT '[]',
    prompt_file VARCHAR(255),
    as_subagent_description TEXT NOT NULL DEFAULT '',         -- 039
    is_active BOOLEAN DEFAULT true,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE(org_id, code)
);

-- Supervisor → subagent роли (039)
CREATE TABLE role_subagents (
    role_id UUID NOT NULL REFERENCES roles(id) ON DELETE CASCADE,
    subagent_role_id UUID NOT NULL REFERENCES roles(id) ON DELETE CASCADE,
    PRIMARY KEY (role_id, subagent_role_id),
    CHECK (role_id <> subagent_role_id)
);

-- Пользователи (001 + 003 is_system + 015 dept/is_head)
CREATE TABLE users (
    id UUID PRIMARY KEY,
    org_id UUID REFERENCES organizations(id) ON DELETE CASCADE,
    name VARCHAR(255) NOT NULL,
    username VARCHAR(50) NOT NULL,
    email VARCHAR(255) NOT NULL UNIQUE,
    password_hash VARCHAR(255),
    role_id UUID REFERENCES roles(id),
    is_admin BOOLEAN DEFAULT false,
    is_system BOOLEAN NOT NULL DEFAULT false,                 -- 003
    department_id UUID REFERENCES departments(id) ON DELETE SET NULL,  -- 015
    is_head BOOLEAN NOT NULL DEFAULT false,                   -- 015
    is_active BOOLEAN DEFAULT true,
    avatar_url TEXT,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW(),
    last_seen_at TIMESTAMPTZ,
    UNIQUE(org_id, username)
);

-- Чаты (001 + 017 task/project + 023 mem + 026 support + 029 poll)
CREATE TABLE chats (
    id UUID PRIMARY KEY,
    org_id UUID NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    type VARCHAR(20) NOT NULL DEFAULT 'direct'
        CHECK (type IN ('direct','task','project','support','poll')),  -- эволюция 017/026/029
    participants TEXT[],                                       -- GIN
    task_id UUID REFERENCES tasks(id),                        -- 017
    project_id UUID REFERENCES projects(id),                  -- 017
    support_ticket_id UUID,                                   -- 026
    poll_id UUID,                                             -- 029
    mem_id UUID REFERENCES memory_snapshots(id) ON DELETE SET NULL,  -- 023
    last_message_at TIMESTAMPTZ,
    is_active BOOLEAN DEFAULT true,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

-- Сообщения (001 + 010 ai_is_valid + 023 mem_id + 043 metadata)
CREATE TABLE messages (
    id UUID PRIMARY KEY,
    chat_id UUID REFERENCES chats(id),
    sender_type VARCHAR(20) DEFAULT 'user',
    sender_id UUID NOT NULL,
    content TEXT NOT NULL,
    mentions JSONB,
    reply_to_id UUID REFERENCES messages(id),
    ai_is_valid BOOLEAN,            -- NULL=pending, true=approved, false=rejected (010)
    ai_edited BOOLEAN DEFAULT false,
    mem_id UUID REFERENCES memory_snapshots(id) ON DELETE SET NULL,   -- 023
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,              -- 043 (modal-протокол и т.п.)
    is_deleted BOOLEAN DEFAULT false,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

-- M:N вложения сообщений ↔ файлы (033)
CREATE TABLE message_attachments (
    message_id UUID NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
    file_id UUID NOT NULL REFERENCES user_files(id) ON DELETE CASCADE,
    position SMALLINT NOT NULL DEFAULT 0,    -- порядок отображения
    PRIMARY KEY (message_id, file_id)
);

-- Per-user маркер прочитанного / high-water-mark (036)
CREATE TABLE chat_read_state (
    chat_id UUID NOT NULL REFERENCES chats(id) ON DELETE CASCADE,
    user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    last_read_message_id UUID REFERENCES messages(id) ON DELETE SET NULL,
    last_read_at TIMESTAMPTZ NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (chat_id, user_id)
);

-- Отделы (015)
CREATE TABLE departments (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    org_id UUID NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,  -- CASCADE с 042
    name VARCHAR(255) NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Симметричные правила видимости отделов (015)
CREATE TABLE department_visibility (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    org_id UUID NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    department_a_id UUID NOT NULL REFERENCES departments(id) ON DELETE CASCADE,
    department_b_id UUID NOT NULL REFERENCES departments(id) ON DELETE CASCADE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (department_a_id, department_b_id)
);

-- Проекты (017 + 046 department)
CREATE TABLE projects (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    org_id UUID NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    -- name, description, created_by_user_id, is_active, created_at/updated_at
    department_id UUID REFERENCES departments(id) ON DELETE SET NULL  -- 046
);

-- Задачи (005 + 016 ownership + 017 project + 027 tsv + 031 priority)
CREATE TABLE tasks (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    org_id UUID NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    title VARCHAR(500) NOT NULL,
    description TEXT,
    status VARCHAR(20) NOT NULL DEFAULT 'created',
    assignee_user_id UUID NOT NULL REFERENCES users(id),
    created_by_user_id UUID REFERENCES users(id),             -- 016
    awaiting_review_at TIMESTAMPTZ,                           -- 016
    proposed_deadline TIMESTAMPTZ,                           -- 016
    proposed_deadline_by UUID REFERENCES users(id),          -- 016
    project_id UUID REFERENCES projects(id) ON DELETE SET NULL,  -- 017
    priority SMALLINT NOT NULL DEFAULT 1,                     -- 031 (+ CHECK)
    deadline TIMESTAMPTZ,
    tsv tsvector,                                             -- 027 (generated, GIN)
    is_active BOOLEAN DEFAULT true,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

-- Соисполнители задач M:N (034)
CREATE TABLE task_participants (
    task_id UUID NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    added_by_user_id UUID REFERENCES users(id),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (task_id, user_id)
);

-- Audit trail задач (017)
CREATE TABLE task_events (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    task_id UUID NOT NULL REFERENCES tasks(id),
    -- actor, event_type, payload JSONB, created_at
);

-- Идемпотентность async agent-вызовов (018)
CREATE TABLE agent_runs (
    -- request_id, chat_id, status (pending|running|done|failed),
    -- result_message_id, error, created_at/updated_at
);

-- Утренние опросы (006 + 029 summary/task_ids)
CREATE TABLE task_polls (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    org_id UUID NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    assignee_user_id UUID NOT NULL REFERENCES users(id),
    poll_date DATE NOT NULL,
    status VARCHAR(20) NOT NULL DEFAULT 'pending',
    responses JSONB,
    summary TEXT,                                            -- 029
    task_ids JSONB NOT NULL DEFAULT '[]'::jsonb,            -- 029
    created_at TIMESTAMPTZ DEFAULT NOW(),
    completed_at TIMESTAMPTZ,
    expires_at TIMESTAMPTZ,
    UNIQUE(assignee_user_id, poll_date)
);

-- Вечерние отчёты (007)
CREATE TABLE task_reports (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    org_id UUID NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
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
    org_id UUID NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    type VARCHAR(50) NOT NULL,
    title VARCHAR(500) NOT NULL,
    content TEXT,
    reference_type VARCHAR(50),
    reference_id UUID,
    is_read BOOLEAN DEFAULT false,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

-- Support-тикеты (026; requester_org_id → CASCADE добавлен в 042)
CREATE TABLE support_tickets (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    requester_user_id UUID NOT NULL REFERENCES users(id),
    requester_org_id UUID NOT NULL REFERENCES organizations(id),  -- CASCADE с 042
    category VARCHAR(20) NOT NULL CHECK (category IN ('how_to','bug','other')),
    status VARCHAR(20) NOT NULL DEFAULT 'open'
        CHECK (status IN ('open','in_progress','closed')),
    assignee_user_id UUID REFERENCES users(id),
    ai_handoff_at TIMESTAMP,
    ai_first_response_at TIMESTAMP,
    closed_at TIMESTAMP,
    closed_by_user_id UUID REFERENCES users(id),
    closed_by_role VARCHAR(20) CHECK (closed_by_role IN ('requester','operator')),
    title VARCHAR(200),
    created_at TIMESTAMP NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMP NOT NULL DEFAULT NOW()
);

-- Audit trail тикетов (026)
CREATE TABLE support_ticket_events (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    ticket_id UUID NOT NULL REFERENCES support_tickets(id) ON DELETE CASCADE,
    actor_user_id UUID NOT NULL REFERENCES users(id),
    actor_role VARCHAR(20) NOT NULL CHECK (actor_role IN ('requester','operator','ai','system')),
    event_type VARCHAR(50) NOT NULL,
    payload JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMP NOT NULL DEFAULT NOW()
);

-- Счета на оплату (044 + 045 last_notified_for_date)
CREATE TABLE invoices (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    org_id UUID NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    file_id UUID NOT NULL REFERENCES user_files(id),
    uploaded_by_user_id UUID NOT NULL REFERENCES users(id),
    due_date DATE,
    status VARCHAR(20) NOT NULL DEFAULT 'created'
        CHECK (status IN ('created','approved','rejected','processed')),
    approved_by_user_id UUID REFERENCES users(id),
    approved_at TIMESTAMPTZ,
    rejected_at TIMESTAMPTZ,
    rejection_reason TEXT,
    processed_at TIMESTAMPTZ,
    processed_by_user_id UUID REFERENCES users(id),
    last_notified_for_date DATE,                             -- 045
    is_active BOOLEAN NOT NULL DEFAULT true,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Снимки памяти диалога (023). Триггер заполняет messages.mem_id из chats.mem_id при INSERT.
CREATE TABLE memory_snapshots (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    snapshot TEXT NOT NULL,
    is_active BOOLEAN NOT NULL DEFAULT true,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Правила коррекции AI — DROP+CREATE в 023 (старая message-id схема мертва), + is_active (025)
CREATE TABLE correction_rules (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    role_id UUID NOT NULL REFERENCES roles(id) ON DELETE CASCADE,
    mem_id UUID REFERENCES memory_snapshots(id) ON DELETE SET NULL,
    mem_embedding VECTOR(1024),
    src_user_message_id UUID REFERENCES messages(id) ON DELETE SET NULL,
    user_message_embedding VECTOR(1024),
    src_ai_response_id UUID REFERENCES messages(id) ON DELETE SET NULL,
    user_correction_text TEXT,
    extracted_lesson TEXT,
    is_active BOOLEAN NOT NULL DEFAULT true                  -- 025
);

-- Файлы (009 + 012 RAG-поля + 033 cloned_from + 037 folder)
CREATE TABLE user_files (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID NOT NULL REFERENCES users(id),
    org_id UUID NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
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
    cloned_from_file_id UUID REFERENCES user_files(id) ON DELETE SET NULL,  -- 033
    folder_id UUID REFERENCES user_file_folders(id),         -- 037
    rag_status VARCHAR(20) DEFAULT 'pending',
    rag_error TEXT,
    indexed_at TIMESTAMPTZ,
    is_active BOOLEAN DEFAULT true,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW(),
    tsv tsvector GENERATED ALWAYS AS (...) STORED
    -- per-user дедуп: UNIQUE (user_id, content_hash) WHERE is_active (035)
);

-- Личные папки файлов (037)
CREATE TABLE user_file_folders (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID NOT NULL REFERENCES users(id),
    org_id UUID NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    parent_folder_id UUID REFERENCES user_file_folders(id),  -- NULL = root, soft-delete (не CASCADE)
    name VARCHAR(255) NOT NULL,
    is_active BOOLEAN NOT NULL DEFAULT true,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    -- UNIQUE (user_id, parent_folder_id, lower(name)) NULLS NOT DISTINCT WHERE is_active
);

-- Устройства Zero Trust (011)
CREATE TABLE user_devices (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID NOT NULL REFERENCES users(id),
    device_name VARCHAR(255),
    device_public_key TEXT NOT NULL,
    is_active BOOLEAN DEFAULT true,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    last_used_at TIMESTAMPTZ,
    UNIQUE (user_id, device_public_key)
);

-- RAG: чанки (012)
CREATE TABLE chunks (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    file_id UUID NOT NULL REFERENCES user_files(id) ON DELETE CASCADE,
    chunk_text TEXT NOT NULL,
    embedding vector(1024) NOT NULL,            -- HNSW
    metadata JSONB NOT NULL DEFAULT '{}',
    chunk_index INTEGER,
    tsv tsvector GENERATED ALWAYS AS (...) STORED   -- GIN
);

-- RAG: строки таблиц (012)
CREATE TABLE tables_rows_chunks (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    file_id UUID NOT NULL REFERENCES user_files(id) ON DELETE CASCADE,
    table_chunk_id UUID REFERENCES chunks(id) ON DELETE SET NULL,
    row_index INTEGER NOT NULL,
    row_text TEXT NOT NULL,
    embedding vector(1024) NOT NULL,            -- HNSW
    tsv tsvector GENERATED ALWAYS AS (...) STORED,
    metadata JSONB NOT NULL DEFAULT '{}',
    created_at TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE(file_id, table_chunk_id, row_index)
);
```

Также: `calendar_events`, `notification_channels`, `notification_log` (002), без последующих структурных изменений.

---

## Миграции

Непрерывный диапазон **001–046** (без пропусков). Файлы — `src/engine/migrations/`.

| # | Файл | Описание |
|---|------|----------|
| 001 | initial.sql | organizations, users, roles, chats, messages + trigger `update_updated_at_column` |
| 002 | role_evolution.sql | roles: agent_type/agent_config/tools/prompt_file; + calendar_events, notification_channels, notification_log |
| 003 | system_user.sql | users.is_system + системные AI-пользователи |
| 004 | mirror_user.sql | Mirror user support (данные) |
| 005 | tasks.sql | tasks |
| 006 | task_polls.sql | task_polls (утренние опросы) |
| 007 | task_reports.sql | task_reports (вечерние отчёты) |
| 008 | in_app_notifications.sql | in_app_notifications |
| 009 | user_files.sql | user_files (метаданные файлов) |
| 010 | correction_rules.sql | messages.ai_validated → ai_is_valid (nullable); correction_rules (старая message-id схема) |
| 011 | user_devices.sql | user_devices (Zero Trust публичные ключи) |
| 012 | rag_schema.sql | pgvector; chunks, tables_rows_chunks; RAG-поля + tsv в user_files |
| 013 | rag_functions.sql | SQL-функции гибридного поиска (concrete/abstract chunks+rows, search_related_docs, search_rag, get_expanded_context) |
| 014 | org_timezone.sql | organizations.timezone |
| 015 | departments.sql | departments, department_visibility; users.department_id/is_head; organizations.org_context |
| 016 | task_ownership.sql | tasks: created_by_user_id, awaiting_review_at, proposed_deadline, proposed_deadline_by |
| 017 | projects_and_task_chats.sql | projects, task_events; tasks.project_id; chats.task_id/project_id; chats.type default 'direct' |
| 018 | pm_role_and_agent_runs.sql | PM-роль/системный юзер `pm`; agent_runs (async idempotency, CAS) |
| 019 | three_main_agents.sql | seed трёх основных агентов (данные) |
| 020 | litellm_model_names.sql | переименование model_name в LiteLLM-формат (данные) |
| 021 | agent_tools_user_search_list_documents.sql | tools: user_search/list_documents у агентов (данные) |
| 022 | pm_calendar_tools.sql | PM calendar-инструменты (данные) |
| 023 | memory_snapshots_and_correction_rules_rework.sql | memory_snapshots; DROP+CREATE correction_rules (mem-based схема); chats.mem_id, messages.mem_id |
| 024 | search_correction_rules_function.sql | SQL-функция search_correction_rules |
| 025 | correction_rules_is_active.sql | correction_rules.is_active |
| 026 | support_tickets.sql | support_tickets, support_ticket_events; chats.type += 'support'; chats.support_ticket_id; chats.org_id → nullable |
| 027 | tasks_tsv.sql | tasks.tsv (generated tsvector) + GIN |
| 028 | report_generator_role.sql | роль генератора отчётов (данные) |
| 029 | poll_chat_and_roles.sql | chats.type += 'poll'; chats.poll_id; task_polls.summary/task_ids |
| 030 | lower_oc_search_threshold.sql | пересоздание search_related_docs с пониженным порогом |
| 031 | task_priority.sql | tasks.priority SMALLINT (+ CHECK, индекс assignee+priority) |
| 032 | chunk_context_by_index.sql | пересоздание search_*/search_rag + get_expanded_context_by_index |
| 033 | chat_attachments.sql | message_attachments (M:N); user_files.cloned_from_file_id |
| 034 | task_participants.sql | task_participants (M:N соисполнители) |
| 035 | user_files_user_content_hash.sql | per-user unique index (user_id, content_hash) для дедупа |
| 036 | chat_read_state.sql | chat_read_state (last-read маркер для unread-счётчиков) |
| 037 | user_file_folders.sql | user_file_folders (adjacency list, NULLS NOT DISTINCT unique); user_files.folder_id |
| 038 | rename_private_documents_tool.sql | переименование инструмента private_documents (данные) |
| 039 | supervisor_subagent_roles.sql | roles.as_subagent_description; role_subagents (supervisor → сабагенты) |
| 040 | search_related_docs_admin_access.sql | пересоздание search_related_docs с admin/scope-доступом |
| 041 | search_correction_rules_scope_filters.sql | пересоздание search_correction_rules со scope-фильтрами |
| 042 | cascade_org_fks.sql | ON DELETE CASCADE на всех FK org_id (departments, tasks, user_files, projects, support_tickets и др.) |
| 043 | messages_metadata.sql | messages.metadata JSONB |
| 044 | invoices.sql | invoices; organizations.accountant_user_id |
| 045 | invoice_clerk_and_notify_tracker.sql | роль/системный юзер `invoice_clerk`; invoices.last_notified_for_date |
| 046 | project_department.sql | projects.department_id (+ backfill из отдела создателя) |
