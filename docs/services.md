# RuGPT Services

Сервисы содержат бизнес-логику приложения.

## EngineService

**Файл:** `src/engine/services/engine_service.py`

Композитный сервис-синглтон. Управляет всеми хранилищами, сервисами, агентами и планировщиком.

```python
class EngineService:
    # Storages (core)
    org_storage: OrgStorage
    user_storage: UserStorage
    role_storage: RoleStorage
    role_subagent_storage: RoleSubagentStorage         # supervisor → subagent маппинг
    chat_storage: ChatStorage
    message_storage: MessageStorage
    message_attachment_storage: MessageAttachmentStorage
    chat_read_state_storage: ChatReadStateStorage
    calendar_storage: CalendarStorage
    notification_channel_storage: NotificationChannelStorage
    notification_log_storage: NotificationLogStorage

    # Storages (alpha)
    in_app_notification_storage: InAppNotificationStorage
    task_storage: TaskStorage
    task_participant_storage: TaskParticipantStorage
    task_poll_storage: TaskPollStorage
    task_report_storage: TaskReportStorage
    user_file_storage: UserFileStorage
    user_file_folder_storage: UserFileFolderStorage
    correction_rule_storage: CorrectionRuleStorage
    device_storage: DeviceStorage
    department_storage: DepartmentStorage              # item 8
    project_storage: ProjectStorage                    # item 11
    task_event_storage: TaskEventStorage               # item 11
    agent_run_storage: AgentRunStorage                 # item 10
    support_ticket_storage: SupportTicketStorage       # техподдержка
    support_ticket_event_storage: SupportTicketEventStorage
    memory_snapshot_storage: MemorySnapshotStorage     # сжатие истории чата
    invoice_storage: InvoiceStorage                    # счета
    rag_store: RAG_store
    nonce_store: NonceStore                            # Zero Trust replay (Redis)

    # Services (core)
    chat_service: ChatService
    mention_service: MentionService
    ai_service: AIService
    calendar_service: CalendarService
    scheduler_service: SchedulerService
    notification_service: NotificationService

    # Services (alpha)
    in_app_notification_service: InAppNotificationService
    task_service: TaskService
    task_poll_service: TaskPollService
    task_report_service: TaskReportService
    file_service: FileService
    folder_service: FolderService
    rag_service: RAGService
    correction_rule_service: CorrectionRuleService
    memory_service: MemoryService                      # memory-snapshot
    department_service: DepartmentService              # item 8
    project_service: ProjectService                    # item 11
    task_event_service: TaskEventService               # item 11
    reference_service: ReferenceService                # item 11 (!/!! ссылки)
    role_subagent_service: RoleSubagentService         # supervisor-сабагенты
    support_ticket_service: SupportTicketService       # техподдержка
    support_notification_service: SupportNotificationService
    invoice_service: InvoiceService                    # счета
    signature_service: SignatureService                # Zero Trust проверка подписи

    # Kafka (item 10)
    kafka_producer: KafkaProducerService
    agent_request_consumer: KafkaConsumerLoop
    _agent_request_handler: AgentRequestHandler

    # Agents
    prompt_cache: PromptCache
    tool_registry: ToolRegistry
    agent_executor: AgentExecutor
    action_registry: ActionRegistry                    # show_modal / action-подтверждения

    # File storage
    storage_adapter: LocalStorageAdapter

# Использование
engine = get_engine_service()
await engine.initialize()
```

> Никакого `OllamaProvider` / `llm_provider` на синглтоне нет — инференс идёт
> через `AgentExecutor` (LangChain ChatOpenAI → LiteLLM). См. `docs/llm.md`.

**Методы:**
- `initialize()` -- `init()` всех хранилищ, токенайзер, проводка RAG/tool, старт Kafka + scheduler, заполнение action registry
- `close()` -- остановка планировщика и Kafka, закрытие соединений, reset action registry

**Порядок инициализации:**

`__init__()` (синхронный, конструирует все объекты, БД ещё не подключена):
1. Storages — ~29 хранилищ через DSN; `message_storage.attachment_storage`
   проводится вручную для auto-hydration `Message.attachments`
2. `NonceStore` (Redis), `SignatureService(device_storage, nonce_store, tolerance)`
3. `PromptCache`, `CalendarService`, `RoleSubagentService`, `DepartmentService`
4. `KafkaProducerService` (no-op при `KAFKA_ENABLED=false`)
5. `InAppNotificationService`, `SupportNotificationService`
6. `ChatService` → `SupportTicketService` → `TaskEventService` →
   `ProjectService` → `TaskService` → `ReferenceService` →
   `TaskPollService` → `TaskReportService` (порядок важен для DI)
7. `LocalStorageAdapter`, `FileService`, `InvoiceService`
8. `RAG_store`, `RAGService`, `FolderService`
9. `NotificationService` + регистрация `TelegramSender`/`EmailSender`
   (только если заданы `TELEGRAM_BOT_TOKEN` / `SMTP_HOST`)
10. tools + `ActionRegistry` (пустой) + `ToolRegistry` (регистрация инструментов)
11. `AgentExecutor` → `MemoryService` → проводка
    `agent_executor.memory_service` и `task_report_service.agent_executor`
    (разрыв chicken-egg через сеттеры)
12. `SchedulerService`, `MentionService`, `AIService`; проводка AI/chat-зависимостей
    в `support_ticket_service`, `task_poll_service`, `scheduler_service`
13. `AgentRequestHandler` + `KafkaConsumerLoop(agent.requests)`
14. `CorrectionRuleService`; проводка `agent_executor.correction_rule_service`

`initialize()` (асинхронный):
1. `await storage.init()` для всех хранилищ + `nonce_store.init()` + `rag_store.init()`
2. `init_token_counter()` (локальный токенайзер, fallback tiktoken)
3. Проводка общего `RAGService` в rag/table_rows/list_documents tools
4. `kafka_producer.start()` + `agent_request_consumer.start()` (no-op при disabled)
5. `register_all_actions(action_registry)` → регистрация `show_modal` tool
6. Регистрация `list_invoices` / `get_invoice` tools (фабрики захватывают `self`)
7. `scheduler_service.start()`

---

## PromptCache

**Файл:** `src/engine/services/prompt_cache.py`

In-memory кеш системных промптов из файлов.

```python
class PromptCache:
    def __init__(self, prompts_dir: str)

    def get_prompt(role) -> str
    # Приоритет: prompt_file (из файла) -> system_prompt (из БД)

    def clear(prompt_file?: str)
    # Сбросить конкретный файл или весь кеш
```

**Как работает:**
1. `Role.prompt_file = "lawyer.md"` в БД
2. `get_prompt()` проверяет кеш (dict in-memory)
3. Если кеш пуст -- читает файл из `src/engine/prompts/`
4. Возвращает текст промпта
5. Fallback: если `prompt_file` не задан -- читает `system_prompt` из БД

**Промпты:** lawyer.md, accountant.md, hr.md, chu.md, admin_assistant.md, humorist.md

**Сброс без рестарта:**
- `POST /api/v1/roles/admin/cache/prompts/clear`
- `POST /api/v1/roles/admin/cache/prompts/clear/{role_code}`

---

## AgentExecutor

**Файл:** `src/engine/agents/executor.py`

Маршрутизатор агентов по `role.agent_type`.

```python
class AgentExecutor:
    def __init__(self, base_url, api_key, default_model, prompt_cache=None,
                 tool_registry=None, timeout=300.0, memory_service=None)

    async def execute(role, messages, caller_user_id=None, temperature=0.7,
                      max_tokens=2048, callee_user_id=None,
                      invocation_kind=None, chat_id=None) -> AgentResult
```

`memory_service` и `correction_rule_service` проставляются сеттерами после
конструирования (chicken-egg, см. `EngineService.__init__`).

**RunnableConfig (configurable) для scope-aware инструментов:**
`org_id`, `caller_user_id`, `callee_user_id`, `invocation_kind`, `chat_id`,
`is_admin`, `timezone`, `role`. Строится в `_make_config()`.

**Маршрутизация по agent_type** (только два варианта):
- `simple` без tools -> прямой вызов ChatOpenAI (LiteLLM → vLLM)
- `simple` с tools -> LangGraph ReAct agent
- `supervisor` -> LangGraph multi-agent supervisor над ролями-сабагентами

`agent_config["steps"]` / `["graph"]` НЕ читаются. Подробнее — `docs/llm.md`.

**AgentResult:**
```python
@dataclass
class AgentResult:
    content: str            # Ответ агента
    model: str
    agent_type: str
    tool_calls: List[ToolCall]
    tokens_used: int
    finish_reason: str      # "stop", "error"
    error: Optional[str]
```

---

## ToolRegistry

**Файл:** `src/engine/agents/tools/registry.py`

Реестр инструментов, доступных агентам.

```python
class ToolRegistry:
    def register(name: str, tool: BaseTool)
    def resolve(names: List[str]) -> Tuple[List[BaseTool], str]  # (tools, warning)
    def get(name: str) -> Optional[BaseTool]
    @property available_tools -> List[str]
```

`resolve()` возвращает **кортеж** `(resolved_tools, warning)`; неизвестные имена
пропускаются и попадают в строку-предупреждение.

**Зарегистрированные инструменты (19):**

Регистрируются в `__init__`:
- `calendar_create`, `calendar_query` -- календарные события
- `task_create`, `task_query`, `task_update`, `task_deadline_proposal`,
  `get_own_tasks` -- задачи (`create_task_tools` возвращает 5 tools)
- `rag_search` -- гибридный поиск по документам (pgvector + TSV)
- `expand_chunk` -- развернуть соседние чанки документа
- `table_rows_search` -- поиск по табличным данным (xlsx/csv)
- `web_search` -- веб-поиск
- `role_call` -- вызов другой роли (stub)
- `list_documents`, `list_own_documents` -- список документов
- `analyze_image` -- анализ изображения
- `user_search` -- поиск пользователей (`create_user_tools` возвращает 1 tool)

Регистрируются в `initialize()` (после заполнения action registry):
- `show_modal` -- запрос action-подтверждения у пользователя через ActionRegistry
- `list_invoices`, `get_invoice` -- счета (фабрики захватывают engine instance)

Часть tools использует factory pattern:
```python
def create_calendar_tools(calendar_service) -> (create_tool, query_tool)
def create_task_tools(task_service) -> (create, query, update, deadline_proposal, get_own)
def create_user_tools(user_storage, role_storage, department_service) -> (user_search,)
```

---

## CalendarService

**Файл:** `src/engine/services/calendar_service.py`

Бизнес-логика календарных событий.

```python
class CalendarService:
    async def create_event(role_id, org_id, title, event_type, ...) -> CalendarEvent
    async def create_from_ai_detection(role_id, org_id, title, date_str, ...) -> CalendarEvent
    async def get_event(event_id) -> CalendarEvent?
    async def list_events(org_id, active_only=True) -> List[CalendarEvent]
    async def list_role_events(role_id, active_only=True) -> List[CalendarEvent]
    async def get_due_events() -> List[CalendarEvent]
    async def mark_triggered(event) -> CalendarEvent
    async def update_event(event_id, title?, description?, ...) -> CalendarEvent?
    async def deactivate_event(event_id) -> bool

    @staticmethod _compute_next_trigger(cron_expression) -> datetime
```

**mark_triggered():**
- Инкрементирует `trigger_count`, ставит `last_triggered_at`
- Для recurring: пересчитывает `next_trigger_at` через croniter
- Для one_time: деактивирует событие

---

## SchedulerService

**Файл:** `src/engine/services/scheduler_service.py`

Фоновый планировщик (asyncio task).

```python
class SchedulerService:
    def __init__(self, calendar_service,
                 notification_service=None,
                 agent_executor=None,
                 role_storage=None,
                 user_storage=None,
                 org_storage=None,
                 task_service=None,
                 task_poll_service=None,
                 task_report_service=None,
                 poll_interval=30,
                 enabled=True,
                 morning_hours=(8, 9, 10),
                 evening_hours=(18, 19, 20))

    async def start()       # asyncio.create_task
    async def stop()        # cancel task
```

**Цикл работы (каждые poll_interval секунд):**

Phase 1 -- Calendar events:
1. `SELECT * FROM calendar_events WHERE is_active=true AND next_trigger_at <= NOW()`
2. Для каждого события:
   a. `mark_triggered()` -- обновление счётчика и next_trigger
   b. Загрузка роли -> `agent_executor.execute()` с контекстом события
   c. Агент генерирует текст уведомления (или fallback)
   d. `notification_service.send_notification()` -- отправка пользователю

Phase 2 -- Task jobs (per-org timezone):
Для каждой организации определяется текущий час по `org.timezone` (`ZoneInfo`):

Morning (часы 8, 9, 10):
- `task_service.check_overdue()` -- помечает просроченные задачи, шлёт in-app уведомления
- `task_poll_service.expire_stale_polls()` -- закрывает неотвеченные опросы (10ч)
- `task_poll_service.create_daily_poll()` -- утренний опрос для каждого сотрудника

Evening (часы 18, 19, 20):
- `task_report_service.generate_report()` -- вечерний отчёт для руководителей

**Обработка ошибок:** каждая организация и каждое событие обрабатывается в отдельном try/except. Невалидный timezone логируется как warning, организация пропускается.

---

## NotificationService

**Файл:** `src/engine/services/notification_service.py`

Оркестрация уведомлений по каналам.

```python
class NotificationService:
    def __init__(self, channel_storage, log_storage, senders?)

    def register_sender(channel_type: str, sender: BaseSender)

    # Отправка
    async def send_notification(user_id, content, event_id?, role_id?) -> bool
    async def send_to_multiple_users(user_ids, content, event_id?, role_id?) -> dict

    # Управление каналами
    async def register_channel(user_id, org_id, channel_type, config, priority) -> NotificationChannel
    async def verify_channel(user_id, channel_type) -> NotificationChannel?
    async def get_user_channels(user_id, enabled_only=False) -> List[NotificationChannel]
    async def remove_channel(user_id, channel_type) -> bool

    # Лог
    async def get_notification_log(user_id, limit=50) -> List[NotificationLog]

    async def close()
```

**Алгоритм send_notification():**
1. Загрузить enabled каналы пользователя (сортировка по priority desc)
2. Пропустить неверифицированные каналы
3. Попробовать отправить через sender первого канала
4. Если успех -> логировать `sent`, вернуть True
5. Если ошибка -> логировать `failed`, попробовать следующий канал
6. Если все каналы failed -> вернуть False

**Senders (каналы доставки):**
- `TelegramSender` -- httpx -> Telegram Bot API (`/sendMessage`)
- `EmailSender` -- aiosmtplib -> SMTP

---

## OrgService

**Файл:** `src/engine/services/org_service.py`

```python
class OrgService:
    async def create_organization(name, slug?, description?, timezone="Europe/Moscow") -> Organization
    async def get_organization(org_id) -> Organization?
    async def get_organization_by_slug(slug) -> Organization?
    async def list_organizations(active_only=True) -> List[Organization]
    async def update_organization(org_id, ...) -> Organization?
    async def deactivate_organization(org_id) -> bool
```

---

## UsersService

**Файл:** `src/engine/services/users_service.py`

```python
class UsersService:
    async def create_user(org_id, name, username, email, password, ...) -> User
    async def get_user(user_id) -> User?
    async def get_user_by_email(email) -> User?
    async def get_user_by_username(username, org_id) -> User?
    async def list_users(org_id, active_only=True) -> List[User]
    async def update_user(user_id, ...) -> User?
    async def update_last_seen(user_id) -> None
    async def change_password(user_id, new_password) -> bool
    async def verify_password(user_id, password) -> bool
    async def assign_role(user_id, role_id?) -> bool
    async def deactivate_user(user_id) -> bool
```

**Особенности:**
- Пароли хешируются через bcrypt (12 раундов)

---

## RolesService

**Файл:** `src/engine/services/roles_service.py`

> Роли предсозданы. CRUD (create/update/delete) убран. Только чтение + управление кешем.

```python
class RolesService:
    async def get_role(role_id) -> Role?
    async def get_role_by_code(code, org_id) -> Role?
    async def list_roles(org_id, active_only=True) -> List[Role]
    async def get_users_with_role(role_id) -> List[User]
    def get_system_prompt(role) -> str       # через PromptCache
    def clear_prompt_cache(prompt_file?) -> None
```

---

## ChatService

**Файл:** `src/engine/services/chat_service.py`

```python
class ChatService:
    def __init__(self, chat_storage, message_storage,
                 chat_read_state_storage=None, user_file_storage=None,
                 message_attachment_storage=None)

    # Chat operations
    async def create_direct_chat(user1_id, user2_id, org_id) -> Chat
    async def create_group_chat(name, participants, created_by, org_id) -> Chat
    async def get_chat(chat_id) -> Chat?
    async def list_user_chats(user_id, active_only=True) -> List[Chat]
    async def add_participant(chat_id, user_id) -> bool
    async def remove_participant(chat_id, user_id) -> bool
    async def archive_chat(chat_id) -> bool

    # Message operations
    async def send_message(chat_id, sender_id, content, sender_type=USER, mentions=None, reply_to_id=None) -> Message
    async def get_message(message_id) -> Message?
    async def list_messages(chat_id, limit=50, before_id=None) -> List[Message]
    async def validate_ai_message(message_id, edited_content=None) -> Message?
    async def reject_ai_message(message_id) -> Message?
    async def get_pending_review_messages(user_id) -> List[Message]
    async def delete_message(message_id) -> bool
```

**Важно:** ChatService отвечает только за хранение. Парсинг упоминаний и генерация AI-ответов происходят в MentionService и AIService соответственно.

---

## MentionService

**Файл:** `src/engine/services/mention_service.py`

```python
class MentionService:
    def __init__(self, user_storage: UserStorage)

    def parse_mentions(content: str) -> List[Tuple[MentionType, str, int]]
    # Синхронный. Возвращает (тип, username, позиция)

    async def resolve_mentions(content: str, org_id: UUID) -> List[Mention]
    # Асинхронный. Парсит + резолвит username -> user_id

    def get_ai_mentions(mentions) -> List[Mention]
    def get_user_mentions(mentions) -> List[Mention]
    def strip_mentions(content) -> str
    def extract_message_for_ai(content, target_username) -> str
```

---

## TaskService

**Файл:** `src/engine/services/task_service.py`

```python
class TaskService:
    # CRUD
    async def create(org_id, title, assignee_user_id, description?, deadline?,
                     created_by_user_id?, project_id?) -> Task
    async def get(task_id) -> Task?
    async def list_by_assignee(user_id, status?) -> List[Task]
    async def list_by_org(org_id, status?) -> List[Task]
    async def list_my_tasks(user_id, include_done?) -> List[dict]  # with priority + creator
    async def list_tasks_created_by(user_id, include_done?) -> List[dict]
    async def list_archived(user_id, org_id, limit?) -> List[dict]  # done | cancelled
    async def update(task_id, title?, description?, assignee_user_id?,
                     deadline?, project_id=_UNSET, actor_user_id?) -> Task?
    async def deactivate(task_id, user?) -> bool

    # Status transitions (item 9)
    async def take_task(task_id, user: User) -> Task          # created -> in_progress
    async def mark_done(task_id, user: User) -> Task          # in_progress -> awaiting_review
    async def accept_task(task_id, user: User) -> Task        # awaiting_review -> done
    async def reject_task(task_id, user, comment?) -> Task    # awaiting_review -> in_progress

    # Deadline negotiation (item 9)
    async def set_deadline(task_id, user, deadline) -> Task
    async def propose_deadline(task_id, user, proposed) -> Task
    async def accept_proposed_deadline(task_id, user) -> Task
    async def reject_proposed_deadline(task_id, user) -> Task

    # Scheduler
    async def check_overdue() -> List[Task]  # Помечает просроченные
```

**Статусы:** `created | in_progress | awaiting_review | done | overdue`

**Owners:** `created_by_user_id` — кто поставил (item 9), `assignee_user_id` —
исполнитель. Admin может действовать от лица creator'а.

**Boundaries на переходы (item 9):**
- Assignee: `take_task`, `mark_done`, `propose_deadline`
- Creator: `accept_task`, `reject_task`, `set_deadline`, `accept/reject_proposed_deadline`

**Hook-ки (items 9/11):**
- После каждого перехода — `_record_event(...)` пишет в `task_events` (item 11)
- После каждого перехода — bell-уведомления через
  `in_app_notification_service.create(type="task_status_change", ...)`:
  - `_bell_to_recipients(task, actor, ...)` — fan-out всем причастным
    (creator + assignee + participants), кроме actor. Список собирает
    `_resolve_recipients` (только `is_active=true`)
  - `_bell_to_user(...)` — одному адресату (add/remove participant)
- `create` — auto-create task chat + link to project chat + bell новым
  участникам + `type="new_task"` ассайни (item 11)
- `deactivate` — archive task chat + archive project chat если это была
  последняя активная задача проекта (item 11)

Никакого PM-агента / direct-chat / Kafka-публикации по задачам нет —
все уведомления идут только через колокольчик (`in_app_notifications`).

**В TaskService DI инжектится:** `in_app_notification_service` (обязательный),
плюс опциональные `chat_service`, `task_event_service`, `project_service`,
`user_storage`, `task_participant_storage` (`Optional[...] = None` для
обратной совместимости с тестами). `TaskNotificationService` НЕ существует.

---

## TaskPollService

**Файл:** `src/engine/services/task_poll_service.py`

```python
class TaskPollService:
    async def create_daily_poll(org_id, assignee_user_id) -> TaskPoll?
    # Идемпотентный: проверяет существование опроса на сегодня

    async def submit_responses(poll_id, responses) -> TaskPoll
    async def get_poll(poll_id) -> TaskPoll?
    async def get_today_poll(user_id) -> TaskPoll?
    async def list_polls(user_id, limit=30) -> List[TaskPoll]
    async def expire_stale_polls(poll_expire_hours=10) -> int
```

При создании опроса отправляется in-app уведомление `poll`.

---

## TaskReportService

**Файл:** `src/engine/services/task_report_service.py`

```python
class TaskReportService:
    async def generate_report(org_id, manager_user_id) -> TaskReport?
    # Пропускает если нет опросов за сегодня

    async def get_report(report_id) -> TaskReport?
    async def list_reports(user_id, limit=30) -> List[TaskReport]
```

При генерации отчёта отправляется in-app уведомление `report` руководителю.

---

## InAppNotificationService

**Файл:** `src/engine/services/in_app_notification_service.py`

```python
class InAppNotificationService:
    async def create(user_id, org_id, type, title, content, reference_type?, reference_id?) -> InAppNotification
    async def list_by_user(user_id, limit=50) -> List[InAppNotification]
    async def get_unread_count(user_id) -> int
    async def mark_read(notification_id) -> bool
    async def mark_all_read(user_id) -> int
```

**Типы:** new_task, poll, report, mention, task_status_change, system

---

## FileService

**Файл:** `src/engine/services/file_service.py`

```python
class FileService:
    def __init__(self, file_storage: UserFileStorage, storage_adapter: StorageAdapter, ...)

    async def upload(org_id, user_id, uploaded_by_user_id, filename, data, is_public=False) -> UserFile
    async def get(file_id) -> UserFile?
    async def download(file_id) -> (bytes, UserFile)
    async def list_by_user(user_id) -> List[UserFile]
    async def list_by_org(org_id) -> List[UserFile]
    async def delete(file_id) -> bool       # Soft-delete + удаление бинарных данных
    async def change_public(file_id, is_public) -> UserFile?
```

**При upload:**
1. Валидация типа (ALLOWED_FILE_TYPES) и размера (MAX_FILE_SIZE)
2. SHA-256 хеш для детекции дубликатов
3. Определение is_table по расширению (xlsx, csv)
4. StorageAdapter.save() -- бинарные данные на диск
5. Запись metadata в PostgreSQL

---

## RAGService

**Файл:** `src/engine/services/rag_service.py`

Индексация документов и поиск. Подробнее см. `docs/rag-info.md`.

```python
class RAGService:
    def __init__(self, store, embedding_model, llm_base_url, llm_api_key,
                 chunk_size, chunk_overlap, summary_input_max_tokens, file_storage=None)

    async def ingest(org_id, user_id, filename, data, file_id) -> dict
    async def try_ingest(..., max_retries=3) -> dict   # С retry
    async def find_docs(org_id, user_id, query, top_k) -> List[RelatedDoc]
    async def search_abstract_in_doc(file_id, query, top_k) -> List[ChunkSearchResult]
    async def search_concrete_in_doc(file_id, query, top_k, tsv_weight) -> List[ChunkSearchResult]
    async def set_status(file_id, status) -> None
```

---

## CorrectionRuleService

**Файл:** `src/engine/services/correction_rule_service.py`

Правила коррекции AI на основе обратной связи пользователей.

```python
class CorrectionRuleService:
    async def create_from_rejection(role_id, org_id, original_message_id, ai_message_id,
                                     chat_id, user_question, ai_answer, correction_text,
                                     created_by_user_id) -> CorrectionRule
    async def get_rules_for_role(role_id) -> List[CorrectionRule]
```

При отклонении AI-ответа:
1. Сохраняет correction rule с user_question + ai_answer + correction_text
2. Запускает `rule_generator` LangGraph graph для генерации rule_text через LLM
3. TODO: правила будут инжектироваться в system prompt через RAG

---

## SignatureService (Zero Trust)

**Файл:** `src/engine/services/signature_service.py`

Единая точка проверки подписи запроса. Держится на синглтоне как
`signature_service`. Вызывается из `WebSignatureMiddleware` — **отдельного
эндпоинта `/auth/verify-signature` НЕТ**.

```python
class SignatureService:
    def __init__(self, device_storage, nonce_store, timestamp_tolerance: int)

    async def verify_request_signature(
        user_id, payload, signature, nonce, timestamp,
    ) -> Tuple[bool, Dict[str, Any]]   # (True, {}) | (False, {"error": code})

    async def has_device_key(user_id) -> bool
```

**Флоу `verify_request_signature`:**
1. `check_timestamp(timestamp, tolerance)` — окно `SIG_TIMESTAMP_TOLERANCE_SECONDS`
2. `device_storage.get_all_public_keys(user_id)` → если пусто, отказ
3. ECDSA: `any(verify_device_signature(pk, payload, signature) ...)`
4. nonce-replay **последним**: `nonce_store.check_and_store(user_id, nonce)`
   (после успешной подписи; `NonceStoreUnavailable` пробрасывается → 503)

### crypto_service.py (module-level функции, НЕ класс)

**Файл:** `src/engine/services/crypto_service.py`

```python
def verify_device_signature(device_public_key_pem, payload, signature_b64) -> bool
# ECDSA P-256 (cryptography lib). Web Crypto отдаёт IEEE P1363 (r||s, 64 байта)
# → конвертация в DER через encode_dss_signature. Любая ошибка → False.

def check_timestamp(timestamp: int, tolerance: int) -> bool
# True если |now - timestamp| <= tolerance (секунды)
```

### NonceStore (защита от replay)

**Файл:** `src/engine/services/nonce_store.py`

Redis-хранилище одноразовых nonce. **Fail-closed**: Redis недоступен →
`NonceStoreUnavailable` → middleware отдаёт **503**.

```python
class NonceStore:
    def __init__(self, redis_url, ttl_seconds)
    async def init()
    async def close()
    async def check_and_store(user_id, nonce) -> bool
    # True если nonce новый (атомарный SET nx ex). False если уже использован.
    # Raise NonceStoreUnavailable при недоступности Redis.

class NonceStoreUnavailable(Exception): ...
```

### WebSignatureMiddleware

**Файл:** `src/engine/middleware/web_signature.py`

Перехватывает `/api/v1/web/*`, срезает `/web` (→ внутренний `/api/v1/...`),
для мутаций (POST/PUT/PATCH/DELETE) извлекает заголовки `X-User-Id`,
`X-Signature`, `X-Nonce`, `X-Timestamp`, `X-Signature-Payload` и делегирует
в `signature_service.verify_request_signature`. GET/HEAD/OPTIONS проходят
без проверки. Нет заголовков → 401; `NonceStoreUnavailable` → 503; отказ → 401.

Подробный Zero-Trust флоу и threat matrix — `docs/networking.md`.

---

## RoleSubagentService

**Файл:** `src/engine/services/role_subagent_service.py`

CRUD-обёртка над `role_subagents` (маппинг supervisor-роль → роли-сабагенты
для multi-agent оркестрации).

```python
class RoleSubagentService:
    def __init__(self, role_subagent_storage: RoleSubagentStorage)
    async def get_available_subagent_roles(role_id) -> List[Role]
    # активные роли, которые role_id может вызвать как сабагенты
```

---

## SupportTicketService

**Файл:** `src/engine/services/support_ticket_service.py`

Бизнес-логика тикетов техподдержки RuGPT. Тикет = спец-чат пользователя с
AI-агентом поддержки (`HOW_TO`) либо живым оператором (`BUG`/`REQUEST`),
с эскалацией `HOW_TO` → человек.

```python
class SupportTicketService:
    def __init__(self, ticket_storage, event_storage, chat_storage,
                 message_storage, user_storage,
                 notification_service=None, kafka_producer=None)
    # self.ai_service проводится после конструирования (chicken-egg с AIService):
    # create_ticket(HOW_TO) запускает первый AI-ответ
```

```python
    async def create_ticket(requester, category, initial_message) -> (SupportTicket, Chat)
    async def escalate(ticket_id, by_user) -> SupportTicket          # AI → человек
    async def take_ticket(ticket_id, operator) -> SupportTicket      # атомарный CAS
    async def close_ticket(ticket_id, by_user) -> SupportTicket      # любая сторона
    async def reopen_if_within_window(ticket_id, by_user?) -> SupportTicket?
    async def handle_incoming_message(chat, sender_id) -> None       # pre-send hook
```

Категории: `HOW_TO | BUG | OTHER`. `HOW_TO` → AI в участниках чата, очередь не
уведомляется; `BUG/OTHER` → `ai_handoff_at` ставится сразу, очередь операторов
уведомляется. Статусные переходы (take/close/reopen) пишут в
`support_ticket_events` (audit) и шлют in-app уведомление — НЕ системные
сообщения в чат. `_publish_ticket_update` шлёт `ticket_update` в Kafka
`chat.events` для live-обновления через WS.

---

## SupportNotificationService

**Файл:** `src/engine/services/support_notification_service.py`

Фан-аут in-app уведомлений операторам поддержки.

Тип уведомлений `support_*` не входит в allowlist `InAppNotificationService`,
поэтому маршрутизация идёт через `type="system"` +
`reference_type="support_ticket"` + `reference_id=ticket.id` (фронт разбирает
по `reference_type`).

```python
class SupportNotificationService:
    def __init__(self, in_app_notification_service, user_storage)
    async def notify_new_in_queue(ticket)   # fan-out всем операторам RUGPT_SUPPORT_ORG_ID
    async def notify_taken(ticket)           # → requester
    async def notify_closed(ticket)          # → второй стороне (по closed_by_role)
    async def notify_reopened(ticket)        # → очередь или assignee
```

---

## InvoiceService

**Файл:** `src/engine/services/invoice_service.py`

Бизнес-логика счетов. Бинарь переиспользует `user_files`, RAG-summary оседает
в `user_files.summary`; InvoiceService хранит структурные поля.

```python
class InvoiceService:
    def __init__(self, invoice_storage: InvoiceStorage, file_service)
    async def upload(org_id, uploader_user_id, filename, data, due_date) -> Invoice
    # бинарь через FileService + явный index_for_rag (summary → user_files.summary)
    async def approve(invoice_id, actor_user_id) -> Invoice          # created → approved
    async def reject(invoice_id, actor_user_id, reason?) -> Invoice  # created → rejected
    async def mark_processed(invoice_id, actor_user_id) -> Invoice   # approved → processed
```

Граф статусов: `created → approved | rejected`; `approved → processed`.

---

## MemoryService

**Файл:** `src/engine/services/memory_service.py`

Сжатие истории чата в memory-snapshot для длинных диалогов: старые сообщения
суммируются LLM-агентом, snapshot инжектится в контекст вместо сырой истории.

```python
class MemoryService:
    def __init__(self, agent_executor, chat_storage, message_storage,
                 memory_snapshot_storage)
    async def get_summary_for_chat(chat_id) -> Optional[str]   # snapshot по chat.mem_id
    async def generate_summary(messages, previous_summary?) -> str   # LLM-суммаризация
    async def check_resummary_needed(chat_id) -> bool         # ≥10 из 15 последних с текущим mem_id
    async def update_summary(chat_id, history) -> None        # fire-and-forget, пишет snapshot + mem_id
```

`AgentExecutor` дёргает `get_summary_for_chat` + `check_resummary_needed` и
запускает `update_summary` в фоне через `asyncio.create_task`.

---

## ProjectService (item 11)

**Файл:** `src/engine/services/project_service.py`

CRUD проектов с head/admin-guard и multi-tenancy через `org_id`.

```python
class ProjectService:
    async def create(name, user: User, description?) -> Project   # head/admin only
    async def get(project_id, user) -> Project?                    # cross-org скрыт
    async def list_by_org(org_id, include_archived?) -> List[Project]
    async def update(project_id, user, name?, description?) -> Project  # head/admin
    async def delete(project_id, user) -> bool  # soft delete + archive_project_chat
```

---

## TaskEventService (item 11)

**Файл:** `src/engine/services/task_event_service.py`

Тонкая обёртка для записи audit trail задач. `TaskService` вызывает
`_record_event` после каждого перехода.

```python
class TaskEventService:
    async def record(task_id, actor_user_id, event_type, payload?) -> TaskEvent
    async def list_for_task(task_id, limit?) -> List[TaskEvent]
```

**Event types:** `created, took, marked_done, accepted, rejected, deadline_set,
deadline_proposed, deadline_proposal_accepted, deadline_proposal_rejected,
assignee_changed, project_changed, cancelled, overdue`.

---

## ReferenceService (item 11)

**Файл:** `src/engine/services/reference_service.py`

Parse + per-viewer batch-resolve `!<task-uuid>` / `!!<project-uuid>` в
тексте сообщений. Интегрируется в `GET /chats/{id}/messages` — возвращает
поле `references: [{type, id, title, accessible, position}]` в каждом
сообщении.

```python
class ReferenceService:
    def parse(content: str) -> List[Tuple[ref_type, uuid, position)]
    async def resolve_batch(
        contents: List[Tuple[message_id, content]],
        actor: User,
    ) -> Dict[message_id, List[dict]]
```

Visibility:
- Task: creator OR assignee OR same-org admin -> accessible, иначе gray pill
- Project: same org + is_active -> accessible

---

## Kafka services (item 10)

### KafkaProducerService

**Файл:** `src/engine/kafka/producer.py`

Тонкая обёртка над `aiokafka.AIOKafkaProducer` с idempotent producer
semantics (`enable_idempotence=True`, `acks='all'`), JSON-сериализацией
с поддержкой UUID/datetime. Single instance в `EngineService`, start/stop
в `initialize()`/`close()`.

```python
class KafkaProducerService:
    async def start()
    async def stop()
    async def send(topic, value: dict, key: Optional[str] = None)
    # No-op when Config.KAFKA_ENABLED=false
```

### KafkaConsumerLoop

**Файл:** `src/engine/kafka/consumer.py`

Background asyncio task, запускается через `asyncio.create_task` в
`EngineService.initialize()`. At-least-once delivery: handler вызывается
для каждого сообщения, offset коммитится только после успешного handle.
При exception offset не коммитится → Kafka redeliver.

```python
class KafkaConsumerLoop:
    def __init__(topic, group_id, handler, bootstrap_servers?)
    async def start()
    async def stop()
```

### AgentRequestHandler

**Файл:** `src/engine/kafka/agent_handler.py`

Callable для `KafkaConsumerLoop`, обслуживает топик `agent.requests`.
Идемпотентный через атомарный CAS в `agent_runs` таблице.

Pipeline:
1. `mark_running` (atomic `UPDATE WHERE status='pending' RETURNING`)
2. Load user message by id
3. `AIService.generate_response()` — существующий sync pipeline
4. `mark_done(result_message_id)` + publish в `chat.events`
5. Any exception → `mark_failed(error)` + re-raise (Kafka redelivery skip)

---

## Зависимости сервисов

```
EngineService (singleton)
    +-- Storages (~29):
    |   +-- org_storage, user_storage, role_storage, role_subagent_storage
    |   +-- chat_storage, message_storage, message_attachment_storage,
    |   |   chat_read_state_storage
    |   +-- calendar_storage
    |   +-- notification_channel_storage, notification_log_storage,
    |   |   in_app_notification_storage
    |   +-- task_storage, task_participant_storage, task_poll_storage,
    |   |   task_report_storage, task_event_storage (item 11)
    |   +-- user_file_storage, user_file_folder_storage
    |   +-- correction_rule_storage, device_storage
    |   +-- department_storage (item 8), project_storage (item 11),
    |   |   agent_run_storage (item 10)
    |   +-- support_ticket_storage, support_ticket_event_storage
    |   +-- memory_snapshot_storage, invoice_storage
    |   +-- rag_store, nonce_store (Redis)
    |
    +-- Agents:
    |   +-- storage_adapter (LocalStorageAdapter)
    |   +-- prompt_cache
    |   +-- action_registry (ActionRegistry → show_modal)
    |   +-- tool_registry (19 tools)
    |   +-- agent_executor (base_url, api_key, default_model, prompt_cache,
    |   |                   tool_registry; memory_service + correction_rule_service
    |   |                   проводятся сеттерами)
    |
    +-- Kafka (item 10):
    |   +-- kafka_producer (KafkaProducerService)
    |   +-- agent_request_consumer (KafkaConsumerLoop on agent.requests)
    |   +-- _agent_request_handler (AgentRequestHandler)
    |
    +-- Security (Zero Trust):
    |   +-- nonce_store (NonceStore: redis_url, ttl)
    |   +-- signature_service (device_storage, nonce_store, timestamp_tolerance)
    |
    +-- Services (order matters for DI):
    |   +-- calendar_service (calendar_storage)
    |   +-- role_subagent_service (role_subagent_storage)
    |   +-- department_service (department_storage, user_storage)
    |   +-- in_app_notification_service (in_app_notification_storage, kafka_producer)
    |   +-- support_notification_service (in_app_notification_service, user_storage)
    |   +-- chat_service (chat_storage, message_storage, chat_read_state_storage,
    |   |                 user_file_storage, message_attachment_storage)
    |   +-- support_ticket_service (ticket/event/chat/message/user storages,
    |   |                           notification_service, kafka_producer; ai_service сеттером)
    |   +-- task_event_service (task_event_storage)                    item 11
    |   +-- project_service (project_storage, chat_service)            item 11
    |   +-- task_service (task_storage, in_app_notification_service,
    |   |                 chat_service, task_event_service, project_service,
    |   |                 user_storage, task_participant_storage)
    |   +-- reference_service (task_storage, project_storage)          item 11
    |   +-- task_poll_service (task_poll_storage, task_service, in_app_notification_service;
    |   |                      chat_service/ai_service/user_storage сеттерами)
    |   +-- task_report_service (task_report_storage, task_poll_service,
    |   |                        in_app_notification_service, role_storage,
    |   |                        task_storage, user_storage; agent_executor +
    |   |                        chat/message_storage сеттерами)
    |   +-- file_service (user_file_storage, storage_adapter, max_size, allowed_types)
    |   +-- invoice_service (invoice_storage, file_service)
    |   +-- rag_service (rag_store, embedding_model, llm_base_url, llm_api_key,
    |   |                chunk_size, chunk_overlap, summary_input_max_tokens, file_storage)
    |   +-- folder_service (user_file_folder_storage, user_file_storage,
    |   |                   rag_service, storage_adapter)
    |   +-- notification_service (channel_storage, log_storage; senders регистрируются)
    |   +-- agent_executor → memory_service (agent_executor, chat_storage,
    |   |                    message_storage, memory_snapshot_storage)
    |   +-- scheduler_service (calendar_service, notification_service, agent_executor,
    |   |                     role/user/org_storage, task_service, task_poll_service,
    |   |                     task_report_service, ...; AI/chat/invoice deps сеттерами)
    |   +-- mention_service (user_storage)
    |   +-- ai_service (role/user/chat/message_storage, prompt_cache, agent_executor,
    |   |               agent_run_storage, kafka_producer, support_ticket_storage,
    |   |               support_ticket_event_storage, task_poll_storage, task_storage,
    |   |               storage_adapter)                               item 10 async mode
    |   +-- correction_rule_service (correction_rule_storage, message_storage,
    |   |                            role_storage, user_storage, chat_service,
    |   |                            memory_snapshot_storage, agent_executor,
    |   |                            embedding_model, llm_base_url, llm_api_key,
    |   |                            kafka_producer)
```

---

## FolderService

**Файл:** `src/engine/services/folder_service.py`

Бизнес-логика личных папок файлов. Валидация владения, циклов, глубины, имени.

```python
class FolderService:
    MAX_DEPTH = 10
    MAX_NAME_LEN = 255

    async def create(user_id, org_id, parent_folder_id, name) -> UserFileFolder
    async def rename(folder_id, new_name, actor) -> UserFileFolder
    async def move(folder_id, new_parent_id, actor) -> UserFileFolder      # cycle + depth check
    async def delete(folder_id, actor) -> dict                              # cascade soft-delete, returns counts
    async def get_tree(user_id, org_id) -> List[dict]                       # nested children
    async def list_children(user_id, parent_folder_id) -> List[UserFileFolder]
    async def list_in_folder(user_id, folder_id) -> List[UserFile]
    async def verify_folder_owner(folder_id, user_id, org_id) -> UserFileFolder  # used by file routes
```

**Typed errors** (все наследуются от `FolderError`):

| Класс | code | HTTP |
|---|---|---|
| FolderInvalidName | EMPTY_NAME / NAME_TOO_LONG | 400 |
| FolderMaxDepthExceeded | MAX_DEPTH_EXCEEDED | 400 |
| FolderCyclicMove | CYCLIC_MOVE | 400 |
| FolderInvalidParentOwner | INVALID_PARENT_OWNER | 400 |
| FolderForbidden | FORBIDDEN | 403 |
| FolderNotFound | FOLDER_NOT_FOUND | 404 |
| FolderParentNotFound | PARENT_NOT_FOUND | 404 |
| FolderNameConflict | DUPLICATE_NAME | 409 |

**Cascade delete (без cross-pool tx — sequential, idempotent on retry):**
1. `subtree_ids = folder_storage.list_subtree_ids(folder_id)`
2. `file_storage.deactivate_by_folder_ids(subtree_ids)` (FIRST)
3. `folder_storage.deactivate_subtree(folder_id)` (SECOND)
4. Best-effort post: `rag_service.delete_document` для indexed файлов + `adapter.delete` для байтов

**Изменения в `FileService`:**
- `upload(..., folder_id: Optional[UUID] = None)` — записывает folder_id (валидация в route)
- `move_to_folder(file_id, new_folder_id, actor_*)` — проверка владения + UPDATE
- `clone()` — клон всегда в корень получателя (`folder_id=None`)

