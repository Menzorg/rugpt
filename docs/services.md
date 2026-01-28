# RuGPT Services

Сервисы содержат бизнес-логику приложения.

## EngineService

**Файл:** `src/engine/services/engine_service.py`

Композитный сервис-синглтон. Управляет всеми хранилищами.

```python
class EngineService:
    org_storage: OrgStorage
    user_storage: UserStorage
    role_storage: RoleStorage
    chat_storage: ChatStorage
    message_storage: MessageStorage

# Использование
engine = get_engine_service()
await engine.initialize()
```

**Методы:**
- `initialize()` — инициализация всех хранилищ
- `close()` — закрытие соединений

---

## OrgService

**Файл:** `src/engine/services/org_service.py`

Управление организациями.

```python
class OrgService:
    async def create_organization(name, slug?, description?) -> Organization
    async def get_organization(org_id) -> Organization?
    async def get_organization_by_slug(slug) -> Organization?
    async def list_organizations(active_only=True) -> List[Organization]
    async def update_organization(org_id, name?, slug?, description?) -> Organization?
    async def deactivate_organization(org_id) -> bool
```

**Особенности:**
- Автогенерация `slug` из `name` если не указан
- Валидация формата slug (lowercase, hyphens)
- Проверка уникальности slug

---

## UsersService

**Файл:** `src/engine/services/users_service.py`

Управление пользователями.

```python
class UsersService:
    async def create_user(org_id, name, username, email, password, ...) -> User
    async def get_user(user_id) -> User?
    async def get_user_by_email(email) -> User?
    async def get_user_by_username(username, org_id) -> User?
    async def list_users(org_id, active_only=True) -> List[User]
    async def update_user(user_id, name?, username?, email?, ...) -> User?
    async def change_password(user_id, new_password) -> bool
    async def verify_password(user_id, password) -> bool
    async def assign_role(user_id, role_id?) -> bool
    async def deactivate_user(user_id) -> bool
    async def update_last_seen(user_id) -> None
```

**Особенности:**
- При создании пользователя автоматически создаётся Main Chat
- Пароли хешируются через bcrypt (12 раундов)
- `username` — уникален в пределах организации
- `email` — глобально уникален

---

## RolesService

**Файл:** `src/engine/services/roles_service.py`

Управление AI-ролями.

```python
class RolesService:
    async def create_role(org_id, name, code, system_prompt, ...) -> Role
    async def get_role(role_id) -> Role?
    async def get_role_by_code(code, org_id) -> Role?
    async def list_roles(org_id, active_only=True) -> List[Role]
    async def update_role(role_id, name?, code?, system_prompt?, ...) -> Role?
    async def deactivate_role(role_id) -> bool
    async def get_users_with_role(role_id) -> List[User]
```

**Особенности:**
- При деактивации роли — автоматически снимается у всех пользователей
- `code` — уникален в пределах организации
- `system_prompt` — определяет поведение AI

---

## ChatService

**Файл:** `src/engine/services/chat_service.py`

Управление чатами и сообщениями.

```python
class ChatService:
    # Чаты
    async def get_chat(chat_id) -> Chat?
    async def get_main_chat(user_id) -> Chat?
    async def get_or_create_direct_chat(user1_id, user2_id, org_id) -> Chat
    async def create_group_chat(org_id, name, participants, created_by) -> Chat
    async def list_user_chats(user_id) -> List[Chat]

    # Сообщения
    async def send_message(chat_id, sender_id, content, reply_to_id?) -> (Message, List[Message])
    async def get_messages(chat_id, limit=50, offset=0) -> List[Message]
    async def validate_ai_response(message_id, user_id) -> bool
    async def edit_message(message_id, user_id, new_content) -> bool
    async def delete_message(message_id, user_id) -> bool
    async def get_unvalidated_ai_messages(user_id) -> List[Message]
```

**Workflow отправки сообщения:**
1. Парсинг mentions через MentionService
2. Сохранение сообщения
3. Для каждого @@ mention:
   - Получение User → Role
   - Генерация AI-ответа (через LLM)
   - Сохранение AI-ответа с `ai_validated=false`
4. Возврат сообщения + список AI-ответов

---

## MentionService

**Файл:** `src/engine/services/mention_service.py`

Парсинг @ и @@ упоминаний.

```python
class MentionService:
    async def parse_mentions(content, org_id) -> List[Mention]
    def extract_usernames(content) -> (List[str], List[str])  # (user, ai_role)
    def has_ai_mentions(content) -> bool
    def has_user_mentions(content) -> bool
    def replace_mentions_with_names(content, mentions, name_map) -> str
```

**Паттерны:**
- `@@username` — AI-роль (MentionType.AI_ROLE)
- `@username` — пользователь (MentionType.USER)

**Примеры:**
```python
# Входной текст
"Привет @admin, посмотри что @@lawyer ответил"

# Результат parse_mentions()
[
    Mention(type=USER, user_id=..., username="admin", position=7),
    Mention(type=AI_ROLE, user_id=..., username="lawyer", position=32)
]
```

---

## Зависимости сервисов

```
EngineService (singleton)
    ├── org_storage
    ├── user_storage
    ├── role_storage
    ├── chat_storage
    └── message_storage

OrgService(org_storage)
UsersService(user_storage, chat_storage)
RolesService(role_storage, user_storage)
ChatService(chat_storage, message_storage, user_storage, role_storage, mention_service)
MentionService(user_storage)
```
