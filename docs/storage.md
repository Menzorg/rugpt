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
    async def list_by_org(org_id: UUID, active_only: bool) -> List[User]
    async def list_by_role(role_id: UUID) -> List[User]
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

---

## ChatStorage

**Файл:** `src/engine/storage/chat_storage.py`

```python
class ChatStorage(BaseStorage):
    async def create(chat: Chat) -> Chat
    async def get_by_id(chat_id: UUID) -> Chat?
    async def get_main_chat(user_id: UUID) -> Chat?
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
    async def list_by_chat(chat_id, limit=50, offset=0, include_deleted=False) -> List[Message]
    async def list_after(chat_id, after_id, limit=50) -> List[Message]
    async def list_before(chat_id, before_id, limit=50) -> List[Message]
    async def update(message: Message) -> Message
    async def validate_ai_response(message_id: UUID, validated: bool) -> bool
    async def edit_content(message_id: UUID, new_content: str, user_id: UUID) -> bool
    async def delete(message_id: UUID) -> bool
    async def count_by_chat(chat_id, include_deleted=False) -> int
    async def get_unvalidated_ai_messages(user_id, limit=10) -> List[Message]
```

**Особенности:**
- `mentions` хранится как JSONB
- Пагинация через limit/offset и cursor (after_id/before_id)
- Сортировка по created_at DESC

---

## Схема базы данных

```sql
-- Организации
CREATE TABLE organizations (
    id UUID PRIMARY KEY,
    name VARCHAR(255) NOT NULL,
    slug VARCHAR(100) NOT NULL UNIQUE,
    description TEXT,
    is_active BOOLEAN DEFAULT true,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

-- Роли
CREATE TABLE roles (
    id UUID PRIMARY KEY,
    org_id UUID REFERENCES organizations(id),
    name VARCHAR(255) NOT NULL,
    code VARCHAR(50) NOT NULL,
    description TEXT,
    system_prompt TEXT NOT NULL,
    rag_collection VARCHAR(255),
    model_name VARCHAR(100) DEFAULT 'qwen2.5:7b',
    is_active BOOLEAN DEFAULT true,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    UNIQUE(org_id, code)
);

-- Пользователи
CREATE TABLE users (
    id UUID PRIMARY KEY,
    org_id UUID REFERENCES organizations(id),
    name VARCHAR(255) NOT NULL,
    username VARCHAR(50) NOT NULL,
    email VARCHAR(255) NOT NULL UNIQUE,
    password_hash VARCHAR(255),
    role_id UUID REFERENCES roles(id),
    is_admin BOOLEAN DEFAULT false,
    is_active BOOLEAN DEFAULT true,
    avatar_url TEXT,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    last_seen_at TIMESTAMP WITH TIME ZONE,
    UNIQUE(org_id, username)
);

-- Чаты
CREATE TABLE chats (
    id UUID PRIMARY KEY,
    org_id UUID REFERENCES organizations(id),
    type VARCHAR(20) DEFAULT 'main',
    name VARCHAR(255),
    participants TEXT[] DEFAULT '{}',
    created_by UUID REFERENCES users(id),
    is_active BOOLEAN DEFAULT true,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    last_message_at TIMESTAMP WITH TIME ZONE
);

-- Сообщения
CREATE TABLE messages (
    id UUID PRIMARY KEY,
    chat_id UUID REFERENCES chats(id),
    sender_type VARCHAR(20) DEFAULT 'user',
    sender_id UUID NOT NULL,
    content TEXT NOT NULL,
    mentions JSONB,
    reply_to_id UUID REFERENCES messages(id),
    ai_validated BOOLEAN DEFAULT false,
    ai_edited BOOLEAN DEFAULT false,
    is_deleted BOOLEAN DEFAULT false,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);
```

---

## Индексы

```sql
-- Organizations
CREATE INDEX idx_organizations_slug ON organizations(slug);
CREATE INDEX idx_organizations_active ON organizations(is_active) WHERE is_active = true;

-- Roles
CREATE INDEX idx_roles_org ON roles(org_id);
CREATE INDEX idx_roles_code ON roles(org_id, code);

-- Users
CREATE INDEX idx_users_org ON users(org_id);
CREATE INDEX idx_users_email ON users(email);
CREATE INDEX idx_users_username ON users(org_id, username);
CREATE INDEX idx_users_role ON users(role_id);

-- Chats
CREATE INDEX idx_chats_org ON chats(org_id);
CREATE INDEX idx_chats_participants ON chats USING GIN(participants);
CREATE INDEX idx_chats_last_message ON chats(last_message_at DESC NULLS LAST);

-- Messages
CREATE INDEX idx_messages_chat ON messages(chat_id);
CREATE INDEX idx_messages_created ON messages(chat_id, created_at DESC);
CREATE INDEX idx_messages_ai_pending ON messages(sender_id, ai_validated)
    WHERE sender_type = 'ai_role' AND ai_validated = false;
```
