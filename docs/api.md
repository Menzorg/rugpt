# RuGPT API Reference

## Base URL

```
http://localhost:8100/api/v1
```

## Аутентификация

Все endpoints (кроме `/auth/login`, `/auth/register`, `/health`) требуют JWT токен в заголовке:

```
Authorization: Bearer <token>
```

---

## Health

### GET /health
Проверка здоровья сервиса.

**Response:**
```json
{
  "status": "healthy",
  "service": "rugpt-engine",
  "timestamp": "2025-01-28T10:00:00Z"
}
```

### GET /health/ready
Готовность к обработке запросов.

### GET /health/live
Проверка что сервис жив.

---

## Auth

### POST /auth/login
Аутентификация по email/password.

**Request:**
```json
{
  "email": "user@example.com",
  "password": "password123"
}
```

**Response:**
```json
{
  "success": true,
  "token": "eyJhbGciOiJIUzI1NiIs...",
  "user_id": "uuid",
  "org_id": "uuid",
  "name": "User Name",
  "username": "username",
  "is_admin": false
}
```

### POST /auth/register
Регистрация нового пользователя.

**Request:**
```json
{
  "org_id": "uuid",
  "name": "User Name",
  "username": "username",
  "email": "user@example.com",
  "password": "password123"
}
```

### GET /auth/me
Получить текущего пользователя.

**Headers:** `Authorization: Bearer <token>`

### POST /auth/refresh
Обновить JWT токен.

---

## Organizations

### GET /organizations
Список организаций (admin only).

### POST /organizations
Создать организацию (admin only).

**Request:**
```json
{
  "name": "Acme Corp",
  "slug": "acme-corp",
  "description": "Description"
}
```

### GET /organizations/{org_id}
Получить организацию по ID.

### PATCH /organizations/{org_id}
Обновить организацию (admin only).

### DELETE /organizations/{org_id}
Деактивировать организацию (admin only).

---

## Users

### GET /users
Список пользователей в организации.

### POST /users
Создать пользователя (admin only).

**Request:**
```json
{
  "name": "John Doe",
  "username": "john_doe",
  "email": "john@example.com",
  "password": "password123",
  "is_admin": false,
  "role_id": "uuid"  // optional
}
```

### GET /users/{user_id}
Получить пользователя.

### GET /users/username/{username}
Получить пользователя по username.

### PATCH /users/{user_id}
Обновить пользователя.

**Request:**
```json
{
  "name": "New Name",
  "username": "new_username",
  "email": "new@example.com",
  "avatar_url": "https://...",
  "is_admin": true
}
```

### POST /users/{user_id}/password
Сменить пароль (только свой).

**Request:**
```json
{
  "current_password": "old",
  "new_password": "new"
}
```

### POST /users/{user_id}/role
Назначить роль пользователю (admin only).

**Request:**
```json
{
  "role_id": "uuid"  // или null для снятия
}
```

### DELETE /users/{user_id}
Деактивировать пользователя (admin only).

---

## Roles

### GET /roles
Список ролей в организации.

### POST /roles
Создать роль (admin only).

**Request:**
```json
{
  "name": "Юрист",
  "code": "lawyer",
  "system_prompt": "Ты корпоративный юрист...",
  "description": "Помощь с правовыми вопросами",
  "model_name": "qwen2.5:7b"
}
```

### GET /roles/{role_id}
Получить роль.

### GET /roles/code/{code}
Получить роль по коду.

### GET /roles/{role_id}/users
Получить пользователей с этой ролью.

### PATCH /roles/{role_id}
Обновить роль (admin only).

### DELETE /roles/{role_id}
Деактивировать роль (admin only).

---

## Chats

### GET /chats
Список чатов текущего пользователя.

### GET /chats/main
Получить main chat текущего пользователя.

### POST /chats/direct
Создать/получить direct chat с пользователем.

**Request:**
```json
{
  "user_id": "uuid"
}
```

### POST /chats/group
Создать групповой чат.

**Request:**
```json
{
  "name": "Chat Name",
  "participants": ["uuid1", "uuid2"]
}
```

### GET /chats/{chat_id}
Получить чат.

### GET /chats/{chat_id}/messages
Получить сообщения чата.

**Query params:**
- `limit` — количество (default: 50)
- `offset` — смещение (default: 0)

### POST /chats/{chat_id}/messages
Отправить сообщение.

**Request:**
```json
{
  "content": "Привет @@lawyer, проверь договор",
  "reply_to_id": "uuid"  // optional
}
```

**Response:**
```json
{
  "message": { /* отправленное сообщение */ },
  "ai_responses": [ /* AI-ответы на @@ mentions */ ]
}
```

### PATCH /chats/{chat_id}/messages/{message_id}
Редактировать сообщение.

**Request:**
```json
{
  "content": "Новый текст"
}
```

### DELETE /chats/{chat_id}/messages/{message_id}
Удалить сообщение (soft delete).

### POST /chats/{chat_id}/messages/{message_id}/validate
Подтвердить AI-ответ.

### GET /chats/pending-validation
Получить непроверенные AI-ответы для текущего пользователя.

---

## Коды ошибок

| HTTP Code | Описание |
|-----------|----------|
| 400 | Неверный запрос |
| 401 | Не авторизован |
| 403 | Доступ запрещён |
| 404 | Не найдено |
| 500 | Внутренняя ошибка |

## Формат ошибки

```json
{
  "detail": "Error message"
}
```
