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
    is_active: bool
    created_at: datetime
    updated_at: datetime
```

**Особенности:**
- Все данные (пользователи, роли, чаты) изолированы по организации
- `slug` — уникальный URL-safe идентификатор
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
    is_active: bool
    avatar_url: Optional[str]
    created_at: datetime
    updated_at: datetime
    last_seen_at: Optional[datetime]
```

**Mentions:**
- `@roman_petrovich` — нотификация пользователю (MentionType.USER)
- `@@roman_petrovich` — вызов AI-роли пользователя (MentionType.AI_ROLE)

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
    model_name: str                 # "qwen2.5:7b"
    agent_type: str                 # "simple" | "chain" | "multi_agent"
    agent_config: dict              # Конфигурация графа (JSONB)
    tools: List[str]                # ["calendar_create", "rag_search", ...]
    prompt_file: Optional[str]      # "lawyer.md" — путь к файлу промпта
    is_active: bool
    created_at: datetime
    updated_at: datetime
```

**Роли предсозданы** через миграции/seed. CRUD через API убран.

**agent_type:**
- `simple` — прямой вызов LLM (без tools) или ReAct agent (с tools)
- `chain` — последовательные шаги из `agent_config["steps"]`
- `multi_agent` — LangGraph StateGraph из `agent_config["graph"]`

**prompt_file:**
- Путь к файлу в `src/engine/prompts/` (напр. `lawyer.md`)
- Приоритет: prompt_file > system_prompt (fallback)
- Файлы кешируются через PromptCache, сброс через admin API

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
- `one_time` — срабатывает один раз в `scheduled_at`, затем деактивируется
- `recurring` — cron-выражение, после срабатывания пересчитывается `next_trigger_at` через croniter

---

## NotificationChannel (Канал уведомлений)

**Файл:** `src/engine/models/notification.py`

```python
@dataclass
class NotificationChannel:
    id: UUID
    user_id: UUID
    org_id: UUID
    channel_type: str               # "telegram" | "email"
    config: dict                    # {"chat_id": "..."} или {"email": "..."}
    is_enabled: bool
    is_verified: bool               # Подтверждён ли канал
    priority: int                   # Выше = пробуется первым
    created_at: datetime
    updated_at: datetime
```

**Каналы:**
- `telegram` — config: `{"chat_id": "123456"}`, привязка через /start
- `email` — config: `{"email": "user@company.com"}`
- UNIQUE(user_id, channel_type) — один канал каждого типа на пользователя

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

## Chat (Чат)

**Файл:** `src/engine/models/chat.py`

```python
class ChatType(str, Enum):
    MAIN = "main"
    DIRECT = "direct"
    GROUP = "group"

@dataclass
class Chat:
    id: UUID
    org_id: UUID
    type: ChatType
    name: Optional[str]
    participants: List[UUID]
    created_by: Optional[UUID]
    is_active: bool
    created_at: datetime
    updated_at: datetime
    last_message_at: Optional[datetime]
```

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
    ai_validated: bool
    ai_edited: bool
    is_deleted: bool
    created_at: datetime
    updated_at: datetime
```

---

## Связи между моделями

```
Organization (1)
    │
    ├── Role (*)            # AI-агенты организации
    │       │
    │       └── CalendarEvent (*)   # События привязаны к роли
    │
    ├── User (*)            # Пользователи организации
    │       │
    │       ├── Role (0..1)         # Назначенная роль
    │       └── NotificationChannel (*)  # Каналы уведомлений
    │
    └── Chat (*)            # Чаты организации
            │
            └── Message (*)         # Сообщения в чате

NotificationLog — лог доставки (user_id, event_id, role_id)
```
