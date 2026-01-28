# RuGPT Models

Модели данных для RuGPT Engine.

## Organization (Организация)

**Файл:** `src/engine/models/organization.py`

Представляет организацию (tenant) в multi-tenant системе.

```python
@dataclass
class Organization:
    id: UUID                        # Уникальный ID
    name: str                       # "Acme Corp"
    slug: str                       # "acme-corp" (URL-safe)
    description: Optional[str]      # Описание
    is_active: bool                 # Активна/деактивирована
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

Пользователь системы.

```python
@dataclass
class User:
    id: UUID
    org_id: UUID                    # Организация
    name: str                       # "Роман Петрович"
    username: str                   # "roman_petrovich" (для @ mentions)
    email: str                      # Уникальный email
    password_hash: Optional[str]    # bcrypt hash
    role_id: Optional[UUID]         # Назначенная AI-роль
    is_admin: bool                  # Администратор организации
    is_active: bool
    avatar_url: Optional[str]
    created_at: datetime
    updated_at: datetime
    last_seen_at: Optional[datetime]
```

**Особенности:**
- `username` — уникален в пределах организации
- `email` — глобально уникален
- `role_id` — ссылка на AI-роль (может быть None)
- `is_admin` — может управлять организацией

**Mentions:**
- `@roman_petrovich` — нотификация пользователю (MentionType.USER)
- `@@roman_petrovich` — вызов AI-роли пользователя (MentionType.AI_ROLE)

---

## Role (AI-Роль)

**Файл:** `src/engine/models/role.py`

AI-агент с определённым поведением.

```python
@dataclass
class Role:
    id: UUID
    org_id: UUID                    # Организация
    name: str                       # "Юрист"
    code: str                       # "lawyer" (уникален в org)
    description: Optional[str]
    system_prompt: str              # Системный промпт
    rag_collection: Optional[str]   # Коллекция RAG (будущее)
    model_name: str                 # "qwen2.5:7b"
    is_active: bool
    created_at: datetime
    updated_at: datetime
```

**Примеры ролей:**
- **Юрист** (`lawyer`) — помощь с договорами, правовыми вопросами
- **Бухгалтер** (`accountant`) — финансовые вопросы, отчётность
- **HR** (`hr`) — кадровые вопросы, политики компании

**Workflow @@ упоминания:**
1. Пользователь пишет `@@roman_petrovich, проверь договор`
2. Система находит User → Role (Юрист)
3. LLM генерирует ответ с system_prompt роли
4. Ответ сохраняется с `ai_validated=false`
5. Роман Петрович может подтвердить/исправить ответ

---

## Chat (Чат)

**Файл:** `src/engine/models/chat.py`

Чат/беседа.

```python
class ChatType(str, Enum):
    MAIN = "main"       # Личный чат пользователя с AI
    DIRECT = "direct"   # Прямой чат между 2 пользователями
    GROUP = "group"     # Групповой чат

@dataclass
class Chat:
    id: UUID
    org_id: UUID
    type: ChatType
    name: Optional[str]             # Название (для GROUP)
    participants: List[UUID]        # Участники
    created_by: Optional[UUID]
    is_active: bool
    created_at: datetime
    updated_at: datetime
    last_message_at: Optional[datetime]
```

**Типы чатов:**
- **MAIN** — создаётся автоматически при создании пользователя. Пользователь + его AI-роль.
- **DIRECT** — между двумя пользователями
- **GROUP** — несколько участников

---

## Message (Сообщение)

**Файл:** `src/engine/models/message.py`

Сообщение в чате.

```python
class SenderType(str, Enum):
    USER = "user"           # Сообщение от человека
    AI_ROLE = "ai_role"     # Ответ AI-роли

class MentionType(str, Enum):
    USER = "user"           # @ — нотификация
    AI_ROLE = "ai_role"     # @@ — вызов AI

@dataclass
class Mention:
    type: MentionType
    user_id: UUID           # Упомянутый пользователь
    username: str           # Username в момент упоминания
    position: int           # Позиция в тексте

@dataclass
class Message:
    id: UUID
    chat_id: UUID
    sender_type: SenderType
    sender_id: UUID             # User ID (для AI — владелец роли)
    content: str
    mentions: List[Mention]
    reply_to_id: Optional[UUID]
    ai_validated: bool          # AI-ответ подтверждён пользователем
    ai_edited: bool             # AI-ответ был отредактирован
    is_deleted: bool
    created_at: datetime
    updated_at: datetime
```

**AI Response Flow:**
1. Сообщение с `sender_type=AI_ROLE` создаётся с `ai_validated=false`
2. Пользователь (владелец роли) видит ответ
3. Может подтвердить → `ai_validated=true`
4. Может отредактировать → `ai_edited=true`

---

## Связи между моделями

```
Organization (1)
    │
    ├── Role (*)        # Роли принадлежат организации
    │
    ├── User (*)        # Пользователи принадлежат организации
    │       │
    │       └── Role (0..1)  # Пользователь может иметь роль
    │
    └── Chat (*)        # Чаты принадлежат организации
            │
            └── Message (*)  # Сообщения в чате
```
