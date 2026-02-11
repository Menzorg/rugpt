# RuGPT API Reference

## Base URL

```
http://localhost:8100/api/v1
```

## Аутентификация

Все endpoints (кроме `/auth/login`, `/auth/register`, `/health`, `/notifications/telegram/webhook`) требуют JWT токен в заголовке:

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
  "role_id": "uuid"
}
```

### GET /users/{user_id}
Получить пользователя.

### GET /users/username/{username}
Получить пользователя по username.

### PATCH /users/{user_id}
Обновить пользователя.

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
  "role_id": "uuid"
}
```

### DELETE /users/{user_id}
Деактивировать пользователя (admin only).

---

## Roles

> **Роли предсозданы** через миграции/seed. CRUD (создание/обновление/удаление) через API отсутствует. Доступно только чтение и управление кешем промптов.

### GET /roles
Список ролей в организации.

**Response:**
```json
[
  {
    "id": "uuid",
    "name": "Юрист",
    "code": "lawyer",
    "description": "Помощь с правовыми вопросами",
    "system_prompt": "...",
    "agent_type": "simple",
    "agent_config": {},
    "tools": ["calendar_create", "calendar_query"],
    "prompt_file": "lawyer.md",
    "model_name": "qwen2.5:7b",
    "is_active": true
  }
]
```

### GET /roles/{role_id}
Получить роль.

### GET /roles/code/{code}
Получить роль по коду.

### GET /roles/{role_id}/users
Получить пользователей с этой ролью.

### POST /roles/admin/cache/prompts/clear
Сбросить кеш всех промптов.

**Response:**
```json
{
  "success": true,
  "message": "All prompt cache cleared"
}
```

### POST /roles/admin/cache/prompts/clear/{role_code}
Сбросить кеш промпта конкретной роли.

---

## Chats

### GET /chats/my
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
  "reply_to_id": "uuid"
}
```

**Response:**
```json
{
  "message": { },
  "ai_responses": [ ]
}
```

### PATCH /chats/{chat_id}/messages/{message_id}
Редактировать сообщение.

### DELETE /chats/{chat_id}/messages/{message_id}
Удалить сообщение (soft delete).

### POST /chats/{chat_id}/messages/{message_id}/validate
Подтвердить AI-ответ.

### GET /chats/pending-validation
Получить непроверенные AI-ответы для текущего пользователя.

---

## Calendar

### GET /calendar/events
Список событий в организации.

**Response:**
```json
[
  {
    "id": "uuid",
    "role_id": "uuid",
    "org_id": "uuid",
    "title": "Проверка договора",
    "description": "Ежемесячная проверка",
    "event_type": "recurring",
    "scheduled_at": null,
    "cron_expression": "0 10 1 * *",
    "next_trigger_at": "2026-03-01T10:00:00",
    "last_triggered_at": "2026-02-01T10:00:00",
    "trigger_count": 5,
    "source_chat_id": null,
    "source_message_id": null,
    "metadata": {},
    "created_by_user_id": "uuid",
    "is_active": true,
    "created_at": "2026-01-15T10:00:00",
    "updated_at": "2026-02-01T10:00:00"
  }
]
```

### POST /calendar/events
Создать событие.

**Request:**
```json
{
  "role_id": "uuid",
  "title": "Напоминание о дедлайне",
  "description": "Сдача отчёта",
  "event_type": "one_time",
  "scheduled_at": "2026-03-15T09:00:00"
}
```

Для рекуррентных событий:
```json
{
  "role_id": "uuid",
  "title": "Еженедельный обзор",
  "event_type": "recurring",
  "cron_expression": "0 10 * * 1"
}
```

### GET /calendar/events/{event_id}
Получить событие.

### PATCH /calendar/events/{event_id}
Обновить событие.

**Request:**
```json
{
  "title": "Новое название",
  "description": "Новое описание",
  "scheduled_at": "2026-04-01T09:00:00",
  "cron_expression": "0 10 * * 5",
  "metadata": {"key": "value"}
}
```

### DELETE /calendar/events/{event_id}
Деактивировать событие.

### GET /calendar/roles/{role_id}/events
Получить события для конкретной роли.

---

## Notifications

### GET /notifications/channels
Каналы уведомлений текущего пользователя.

**Response:**
```json
[
  {
    "id": "uuid",
    "user_id": "uuid",
    "org_id": "uuid",
    "channel_type": "telegram",
    "config": {"chat_id": "123456789"},
    "is_enabled": true,
    "is_verified": true,
    "priority": 10,
    "created_at": "...",
    "updated_at": "..."
  }
]
```

### POST /notifications/channels
Зарегистрировать канал уведомлений.

**Request:**
```json
{
  "channel_type": "telegram",
  "config": {"chat_id": "123456789"},
  "priority": 10
}
```

Для email:
```json
{
  "channel_type": "email",
  "config": {"email": "user@company.com"},
  "priority": 5
}
```

### DELETE /notifications/channels/{channel_type}
Удалить канал уведомлений.

### POST /notifications/channels/{channel_type}/verify
Подтвердить канал (после привязки).

### POST /notifications/telegram/webhook
Telegram Bot webhook. Не требует авторизации.

Обрабатывает `/start <user_id>` — автоматически привязывает Telegram-аккаунт к пользователю:
1. Пользователь открывает `t.me/rugpt_bot?start=<user_id>`
2. Бот получает chat_id пользователя
3. Создаётся и верифицируется канал telegram с priority=10
4. Пользователю отправляется подтверждение

### GET /notifications/log
Лог доставки уведомлений текущего пользователя.

**Query params:**
- `limit` — количество (default: 50, max: 200)

**Response:**
```json
[
  {
    "id": "uuid",
    "user_id": "uuid",
    "channel_type": "telegram",
    "event_id": "uuid",
    "role_id": "uuid",
    "content": "Reminder: Проверка договора",
    "status": "sent",
    "attempts": 1,
    "error_message": null,
    "created_at": "...",
    "updated_at": "..."
  }
]
```

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
