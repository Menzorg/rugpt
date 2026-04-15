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
    chat_storage: ChatStorage
    message_storage: MessageStorage
    calendar_storage: CalendarStorage
    notification_channel_storage: NotificationChannelStorage
    notification_log_storage: NotificationLogStorage

    # Storages (alpha)
    task_storage: TaskStorage
    task_poll_storage: TaskPollStorage
    task_report_storage: TaskReportStorage
    in_app_notification_storage: InAppNotificationStorage
    user_file_storage: UserFileStorage
    correction_rule_storage: CorrectionRuleStorage
    device_storage: DeviceStorage
    rag_store: RAG_store

    # Services (core)
    chat_service: ChatService
    mention_service: MentionService
    ai_service: AIService
    calendar_service: CalendarService
    scheduler_service: SchedulerService
    notification_service: NotificationService

    # Services (alpha)
    task_service: TaskService
    task_poll_service: TaskPollService
    task_report_service: TaskReportService
    in_app_notification_service: InAppNotificationService
    file_service: FileService
    rag_service: RAGService
    correction_rule_service: CorrectionRuleService
    crypto_service: CryptoService

    # Agents
    prompt_cache: PromptCache
    tool_registry: ToolRegistry
    agent_executor: AgentExecutor

    # File storage
    storage_adapter: LocalStorageAdapter

    # LLM (legacy)
    llm_provider: OllamaProvider

# Использование
engine = get_engine_service()
await engine.initialize()
```

**Методы:**
- `initialize()` -- инициализация всех хранилищ, запуск планировщика
- `close()` -- остановка планировщика, закрытие соединений

**Порядок инициализации:**
1. Storages (все 17)
2. LLM Provider, PromptCache
3. CalendarService, InAppNotificationService
4. TaskService, TaskPollService, TaskReportService
5. FileService (+ LocalStorageAdapter)
6. RAGService
7. NotificationService (+ TelegramSender, EmailSender)
8. ToolRegistry, AgentExecutor
9. SchedulerService (с calendar, notifications, agent_executor, tasks, per-org timezone)
10. ChatService, MentionService, AIService
11. CorrectionRuleService

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
    def __init__(self, base_url, default_model, prompt_cache, tool_registry, timeout=300.0)

    async def execute(role, messages, user_id=None, temperature=0.7, max_tokens=2048) -> AgentResult
```

`user_id` передается в `RunnableConfig(configurable={"org_id": ..., "user_id": ...})` для scope-aware инструментов (rag_search, task tools).

**Маршрутизация по agent_type:**
- `simple` без tools -> прямой вызов ChatOllama
- `simple` с tools -> LangGraph ReAct agent
- `chain` -> последовательные шаги из `agent_config["steps"]`
- `multi_agent` -> LangGraph StateGraph из `agent_config["graph"]`

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
    def resolve(names: List[str]) -> List[BaseTool]
    @property available_tools -> List[str]
```

**Зарегистрированные инструменты:**
- `calendar_create` -- создание календарного события (работает)
- `calendar_query` -- запрос событий (работает)
- `task_create` -- создание задачи (работает)
- `task_query` -- запрос задач (работает)
- `task_update` -- обновление задачи (работает)
- `rag_search` -- гибридный поиск по документам (работает, pgvector + TSV)
- `web_search` -- веб-поиск (stub)
- `role_call` -- вызов другой роли (stub)

Calendar и task tools используют factory pattern:
```python
def create_calendar_tools(calendar_service) -> (create_tool, query_tool)
def create_task_tools(task_service) -> (create_tool, query_tool, update_tool)
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
    def __init__(self, chat_storage: ChatStorage, message_storage: MessageStorage)

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

**Hook-ки (items 10/11):**
- После каждого перехода — `_record_event(...)` пишет в `task_events` (item 11)
- После каждого перехода — `_notify('notify_<name>', task, user)` в PM-агент
  через `TaskNotificationService` (item 10). Silent no-op если Kafka disabled
- `create` — auto-create task chat + link to project chat (item 11)
- `deactivate` — archive task chat + archive project chat если это была
  последняя активная задача проекта (item 11)

**В TaskService DI инжектится:** `chat_service`, `task_event_service`,
`project_service`, `task_notification_service` — все опциональные
`Optional[...] = None` для обратной совместимости с тестами.

---

## TaskNotificationService (item 10)

**Файл:** `src/engine/services/task_notification_service.py`

PM-агент как автоматический уведомитель по задачам. Постит plain-text
сообщения в личный direct-chat между PM system user (`username='pm'`) и
каждой заинтересованной стороной. Публикует в Kafka `chat.events` для
live-доставки через WS.

```python
class TaskNotificationService:
    async def notify_take(task, actor)                   # assignee взял -> creator
    async def notify_mark_done(task, actor)              # готово -> creator
    async def notify_accept(task, actor)                 # принято -> assignee
    async def notify_reject(task, actor, comment?)       # возврат -> assignee
    async def notify_set_deadline(task, actor)           # новый срок -> assignee
    async def notify_propose_deadline(task, actor)       # предложение -> creator
    async def notify_accept_proposed_deadline(task, actor)   # -> assignee
    async def notify_reject_proposed_deadline(task, actor)   # -> assignee
    async def notify_overdue(task)                       # -> обе стороны
```

**Правило адресации:** уведомляется сторона, которая **не инициировала**
изменение.

**Реализация `_post()`:**
1. Lazy-lookup PM user (`user_storage.get_system_user_by_username('pm')`),
   cached after first call
2. Lazy-create direct chat PM↔recipient через `chat_service.create_direct_chat`
   (идемпотентно — возвращает существующий, если есть)
3. Persist message в БД (`sender_type=ai_role`, `ai_is_valid=true`)
4. Publish в Kafka `chat.events` с полным payload
5. Kafka publish failures не ломают DB persist (best-effort)

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
    def __init__(self, store, *, ollama_model, ollama_embeddings_base_url, ollama_base_url,
                 chunk_size, chunk_overlap, summary_input_max_chars, file_storage?)

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

## CryptoService

**Файл:** `src/engine/services/crypto_service.py`

Верификация устройств (Zero Trust).

```python
class CryptoService:
    def verify_device_signature(public_key_pem, payload, signature) -> bool
    # ECDSA P-256 signature verification
```

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
    +-- Storages:
    |   +-- org_storage, user_storage, role_storage, chat_storage, message_storage
    |   +-- calendar_storage
    |   +-- notification_channel_storage, notification_log_storage
    |   +-- task_storage, task_poll_storage, task_report_storage
    |   +-- in_app_notification_storage
    |   +-- user_file_storage, correction_rule_storage, device_storage
    |   +-- rag_store
    |   +-- department_storage (item 8)
    |   +-- project_storage, task_event_storage (item 11)
    |   +-- agent_run_storage (item 10)
    |
    +-- Agents & LLM:
    |   +-- storage_adapter (LocalStorageAdapter)
    |   +-- prompt_cache
    |   +-- tool_registry (calendar, task, rag, web, role_call)
    |   +-- agent_executor (llm, prompt_cache, tool_registry)
    |
    +-- Kafka (item 10):
    |   +-- kafka_producer (KafkaProducerService)
    |   +-- agent_request_consumer (KafkaConsumerLoop on agent.requests)
    |   +-- _agent_request_handler (AgentRequestHandler)
    |
    +-- Services (order matters for DI):
    |   +-- calendar_service (calendar_storage)
    |   +-- department_service (department_storage, user_storage)
    |   +-- in_app_notification_service (in_app_notification_storage)
    |   +-- chat_service (chat_storage, message_storage)
    |   +-- task_event_service (task_event_storage)                    item 11
    |   +-- project_service (project_storage, chat_service)            item 11
    |   +-- task_notification_service (chat_service, message_storage,  item 10
    |   |                              user_storage, kafka_producer)
    |   +-- task_service (task_storage, in_app_notification_service,
    |   |                 chat_service, task_event_service,
    |   |                 project_service, task_notification_service)
    |   +-- task_poll_service (task_poll_storage, task_service, in_app_notification_service)
    |   +-- task_report_service (task_report_storage, task_poll_storage,
    |   |                        user_storage, in_app_notification_service)
    |   +-- reference_service (task_storage, project_storage)          item 11
    |   +-- mention_service (user_storage)
    |   +-- ai_service (role_storage, user_storage, chat_storage,
    |   |               message_storage, prompt_cache, agent_executor,
    |   |               agent_run_storage, kafka_producer)             item 10 async mode
    |   +-- correction_rule_service (correction_rule_storage, agent_executor)
    |   +-- file_service (user_file_storage, storage_adapter)
    |   +-- rag_service (rag_store, user_file_storage)
    |   +-- notification_service (channel_storage, log_storage, senders)
    |   +-- scheduler_service (calendar, notifications, agent_executor, ...)
```
