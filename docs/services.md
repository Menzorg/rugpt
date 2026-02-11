# RuGPT Services

Сервисы содержат бизнес-логику приложения.

## EngineService

**Файл:** `src/engine/services/engine_service.py`

Композитный сервис-синглтон. Управляет всеми хранилищами, сервисами, агентами и планировщиком.

```python
class EngineService:
    # Storages
    org_storage: OrgStorage
    user_storage: UserStorage
    role_storage: RoleStorage
    chat_storage: ChatStorage
    message_storage: MessageStorage
    calendar_storage: CalendarStorage
    notification_channel_storage: NotificationChannelStorage
    notification_log_storage: NotificationLogStorage

    # Services
    chat_service: ChatService
    mention_service: MentionService
    ai_service: AIService
    calendar_service: CalendarService
    scheduler_service: SchedulerService
    notification_service: NotificationService

    # Agents
    prompt_cache: PromptCache
    tool_registry: ToolRegistry
    agent_executor: AgentExecutor

    # LLM (legacy)
    llm_provider: OllamaProvider

# Использование
engine = get_engine_service()
await engine.initialize()
```

**Методы:**
- `initialize()` — инициализация всех хранилищ, запуск планировщика
- `close()` — остановка планировщика, закрытие соединений

**Порядок инициализации:**
1. Storages
2. LLM Provider, PromptCache
3. CalendarService, NotificationService (+ senders)
4. AgentExecutor, ToolRegistry
5. SchedulerService (с notification_service, agent_executor, role_storage)
6. ChatService, AIService

---

## PromptCache

**Файл:** `src/engine/services/prompt_cache.py`

In-memory кеш системных промптов из файлов.

```python
class PromptCache:
    def __init__(self, prompts_dir: str)

    def get_prompt(role) -> str
    # Приоритет: prompt_file (из файла) → system_prompt (из БД)

    def clear(prompt_file?: str)
    # Сбросить конкретный файл или весь кеш
```

**Как работает:**
1. `Role.prompt_file = "lawyer.md"` в БД
2. `get_prompt()` проверяет кеш (dict in-memory)
3. Если кеш пуст — читает файл из `src/engine/prompts/`
4. Возвращает текст промпта
5. Fallback: если `prompt_file` не задан — читает `system_prompt` из БД

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

    async def execute(role, messages, temperature=0.7, max_tokens=2048) -> AgentResult
```

**Маршрутизация по agent_type:**
- `simple` без tools → прямой вызов ChatOllama
- `simple` с tools → LangGraph ReAct agent
- `chain` → последовательные шаги из `agent_config["steps"]`
- `multi_agent` → LangGraph StateGraph из `agent_config["graph"]`

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
- `calendar_create` — создание календарного события
- `calendar_query` — запрос событий
- `rag_search` — поиск по документам (stub)
- `web_search` — веб-поиск (stub)
- `role_call` — вызов другой роли (stub)

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
    def __init__(self, calendar_service, notification_service=None,
                 agent_executor=None, role_storage=None,
                 poll_interval=30, enabled=True)

    async def start()       # asyncio.create_task
    async def stop()        # cancel task
```

**Цикл работы (каждые poll_interval секунд):**
1. `SELECT * FROM calendar_events WHERE is_active=true AND next_trigger_at <= NOW()`
2. Для каждого события:
   a. `mark_triggered()` — обновление счётчика и next_trigger
   b. Загрузка роли → `agent_executor.execute()` с контекстом события
   c. Агент генерирует текст уведомления (или fallback)
   d. `notification_service.send_notification()` — отправка пользователю

**Конфигурация (.env):**
```env
SCHEDULER_POLL_INTERVAL=30
SCHEDULER_ENABLED=true
```

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
```

**Алгоритм send_notification():**
1. Загрузить enabled каналы пользователя (сортировка по priority desc)
2. Пропустить неверифицированные каналы
3. Попробовать отправить через sender первого канала
4. Если успех → логировать `sent`, вернуть True
5. Если ошибка → логировать `failed`, попробовать следующий канал
6. Если все каналы failed → вернуть False

**Senders (каналы доставки):**
- `TelegramSender` — httpx → Telegram Bot API (`/sendMessage`)
- `EmailSender` — aiosmtplib → SMTP

---

## OrgService

**Файл:** `src/engine/services/org_service.py`

```python
class OrgService:
    async def create_organization(name, slug?, description?) -> Organization
    async def get_organization(org_id) -> Organization?
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
    async def list_users(org_id, active_only=True) -> List[User]
    async def update_user(user_id, ...) -> User?
    async def change_password(user_id, new_password) -> bool
    async def verify_password(user_id, password) -> bool
    async def assign_role(user_id, role_id?) -> bool
    async def deactivate_user(user_id) -> bool
```

**Особенности:**
- При создании пользователя автоматически создаётся Main Chat
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
    async def get_chat(chat_id) -> Chat?
    async def get_main_chat(user_id) -> Chat?
    async def get_or_create_direct_chat(user1_id, user2_id, org_id) -> Chat
    async def create_group_chat(org_id, name, participants, created_by) -> Chat
    async def list_user_chats(user_id) -> List[Chat]
    async def send_message(chat_id, sender_id, content, reply_to_id?) -> (Message, List[Message])
    async def get_messages(chat_id, limit=50, offset=0) -> List[Message]
    async def validate_ai_response(message_id, user_id) -> bool
```

**Workflow отправки сообщения:**
1. Парсинг mentions через MentionService
2. Сохранение сообщения
3. Для каждого @@ mention:
   - Получение User → Role
   - Генерация AI-ответа через AgentExecutor (в AIService)
   - Сохранение AI-ответа с `ai_validated=false`
4. Возврат сообщения + список AI-ответов

---

## MentionService

**Файл:** `src/engine/services/mention_service.py`

```python
class MentionService:
    async def parse_mentions(content, org_id) -> List[Mention]
    def extract_usernames(content) -> (List[str], List[str])
    def has_ai_mentions(content) -> bool
```

---

## Зависимости сервисов

```
EngineService (singleton)
    ├── org_storage
    ├── user_storage
    ├── role_storage
    ├── chat_storage
    ├── message_storage
    ├── calendar_storage
    ├── notification_channel_storage
    ├── notification_log_storage
    │
    ├── prompt_cache
    ├── tool_registry (calendar_tools, rag, web, role_call)
    ├── agent_executor (llm, prompt_cache, tool_registry)
    │
    ├── calendar_service (calendar_storage)
    ├── notification_service (channel_storage, log_storage, senders)
    ├── scheduler_service (calendar, notifications, agent_executor, role_storage)
    │
    ├── chat_service (chat_storage, message_storage)
    ├── mention_service (user_storage)
    └── ai_service (role_storage, user_storage, chat_storage, message_storage,
                     llm_provider, prompt_cache, agent_executor)
```
