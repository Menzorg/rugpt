# RuGPT API Reference

## Base URL

| Env | URL |
|---|---|
| Local dev | `http://127.0.0.1:8100/api/v1` |
| Prod (from webclient через WG) | `http://10.0.0.2/api/v1/web/*` — nginx срезает `/web` и проксирует на FastAPI `:8100` |

Engine слушает только на loopback (`127.0.0.1:8100`). Внешний доступ — только через nginx на rugpt-container (B.I.1) по WireGuard. Подробности — `docs/networking.md`.

## Аутентификация

Двухслойная:

1. **JWT HMAC-SHA256** (`Authorization: Bearer <token>`) — обязательна для всех endpoints кроме публичных.
2. **ECDSA P-256 device signature** — обязательна для всех mutation-endpoints через webclient; проверяется в NestJS `SignatureGuard` → engine `POST /auth/verify-signature`. Детали ECDSA-флоу — `docs/networking.md` раздел «App-layer auth».

Query-параметры (или body) для подписанных запросов:

| Параметр | Описание |
|---|---|
| `signature` | ECDSA-подпись payload (base64) |
| `nonce` | 16-байтный random, защита от replay |
| `sig_timestamp` | Unix timestamp (валидное окно ±5 min) |
| `user_id` | UUID пользователя (для резолва `user_devices.public_key`) |

### Публичные endpoints (без JWT и без signature — декоратор `@SkipSignature`)

- `POST /auth/login`, `POST /auth/register`, `POST /auth/verify-signature`
- `GET /config` (maintenance status)
- `GET /health*`
- `POST /notifications/telegram/webhook`

> `POST /files/upload` и `GET /files/:id/download` **переведены на signed** (2026-05) — больше не `@SkipSignature`. Пункт 9 в `tech-debt.md` закрыт.

### Rate-limit

**Engine rate-limit не имеет** — открыт в WireGuard без throttling (security-пункт 14 из `tech-debt.md`). Rate-limit — только на webclient (Redis-backed): HTTP default 100/min, strict 10/min; WS 100/min/event/user.

### Известные замечания

- **Telegram webhook** (`POST /notifications/telegram/webhook`) — engine за VPN, с публичного интернета **недоступен**. Исходящие вызовы `Engine → api.telegram.org` работают.
- **WebSocket** — на уровне engine отсутствует (WS живёт на webclient). Engine шлёт события в webclient через Kafka `chat.events`.
- **CORS** Engine = `allow_origins=["*"]` — dev-настройка, ослабляет Zero-Trust.

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

### POST /auth/verify-signature
Верификация подписи устройства (используется WebClient backend).

**Request:**
```json
{
  "user_id": "uuid",
  "payload": "string",
  "signature": "base64"
}
```

### GET /auth/devices
Список устройств текущего пользователя.

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
  "description": "Description",
  "timezone": "Europe/Moscow"
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

### GET /users/system
Системные AI-пользователи.

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
    "model_name": "hosted_vllm/google/gemma-4-31B-it",
    "rag_collection": null,
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

### POST /chats/direct
Создать/получить direct chat с пользователем.

**Request:**
```json
{
  "other_user_id": "uuid"
}
```

### POST /chats/group
Создать групповой чат.

**Request:**
```json
{
  "name": "Chat Name",
  "participant_ids": ["uuid1", "uuid2"]
}
```

### GET /chats/{chat_id}
Получить чат.

### DELETE /chats/{chat_id}
Архивировать чат.

### POST /chats/{chat_id}/participants/{participant_id}
Добавить участника в чат.

### DELETE /chats/{chat_id}/participants/{participant_id}
Удалить участника из чата.

### GET /chats/{chat_id}/messages
Получить сообщения чата (cursor-based pagination).

**Query params:**
- `limit` — количество (default: 50)
- `before_id` — UUID сообщения для пагинации (вернуть сообщения до этого)

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
  "user_message": { },
  "ai_responses": [ ]
}
```

### GET /chats/messages/{message_id}
Получить сообщение по ID.

### DELETE /chats/messages/{message_id}
Удалить сообщение (soft delete).

### POST /chats/messages/{message_id}/validate
Подтвердить AI-ответ (ai_is_valid = true). Можно передать отредактированный текст.

**Request (optional):**
```json
{
  "edited_content": "Исправленный текст ответа"
}
```

### POST /chats/messages/{message_id}/reject
Отклонить AI-ответ и создать правило коррекции.

**Request:**
```json
{
  "correction_text": "Неверный срок исковой давности, для трудовых споров — 3 месяца"
}
```

**Response:** CorrectionRule object. Также отправляет correction_text в чат от имени пользователя и генерирует rule_text через LLM.

### GET /chats/pending-review
Получить AI-ответы ожидающие проверки (ai_is_valid IS NULL).

### GET /chats/unvalidated
Deprecated: используйте /pending-review.

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

## Departments (admin only)

### POST /departments
Создать отдел.

### GET /departments
Список отделов организации.

### GET /departments/{id}
Получить отдел.

### PATCH /departments/{id}
Обновить отдел.

### DELETE /departments/{id}
Удалить отдел (users -> NULL, rules -> CASCADE).

### POST /departments/{id}/head/{user_id}
Назначить руководителя отдела.

### DELETE /departments/{id}/head
Снять руководителя.

### POST /departments/visibility
Создать правило видимости (симметричное).

**Request:**
```json
{
  "department_a_id": "uuid",
  "department_b_id": "uuid"
}
```

### DELETE /departments/visibility/{id}
Удалить правило видимости.

### GET /departments/visibility
Список правил видимости организации.

### POST /organizations/{id}/context/upload
Загрузить файл с описанием структуры организации. Текст извлекается через Tika и записывается в org_context. Добавляется в system prompt всех ролей.

---

## In-App Notifications

### GET /in-app-notifications
Список in-app уведомлений текущего пользователя.

### GET /in-app-notifications/unread-count
Счётчик непрочитанных уведомлений.

**Response:**
```json
{
  "count": 5
}
```

### PATCH /in-app-notifications/{id}/read
Отметить уведомление как прочитанное.

### POST /in-app-notifications/read-all
Отметить все уведомления как прочитанные.

---

## Tasks

### GET /tasks
Список задач.

**Query params:**
- `status` — фильтр по статусу (created, in_progress, done, overdue)
- `assignee_user_id` — фильтр по исполнителю

Админ видит все задачи организации, сотрудник — только свои.

### POST /tasks
Создать задачу.

**Request:**
```json
{
  "title": "Подготовить отчёт",
  "description": "Квартальный финансовый отчёт",
  "assignee_user_id": "uuid",
  "deadline": "2026-04-15T18:00:00"
}
```

### GET /tasks/{task_id}
Получить задачу.

### PATCH /tasks/{task_id}
Обновить задачу.

**Request:**
```json
{
  "status": "in_progress",
  "title": "Новое название"
}
```

### DELETE /tasks/{task_id}
Деактивировать задачу.

### GET /tasks/my?include_done=&project_id=
Задачи где юзер — assignee, отсортированы по приоритету (item 9).
Optional `project_id` фильтр (item 11).

### GET /tasks/created-by-me?include_done=&project_id=
Задачи где юзер — creator (item 9). Optional `project_id` фильтр.

### GET /tasks/archive?limit=
Архив задач: done или cancelled, где юзер был creator/assignee (item 11).

### POST /tasks/{task_id}/take
Assignee берёт в работу: `created` → `in_progress` (item 9).

### POST /tasks/{task_id}/mark-done
Assignee отмечает готово: `in_progress` → `awaiting_review` (item 9).

### POST /tasks/{task_id}/accept
Creator принимает: `awaiting_review` → `done` (item 9).

### POST /tasks/{task_id}/reject
Creator возвращает с комментарием: `awaiting_review` → `in_progress` (item 9).
Body: `{ comment?: string }`.

### PATCH /tasks/{task_id}/deadline
Creator напрямую меняет срок (item 9). Body: `{ deadline: ISO8601 }`.

### POST /tasks/{task_id}/deadline-proposal
Assignee предлагает новый срок (item 9). Body: `{ proposed_deadline: ISO8601 }`.

### POST /tasks/{task_id}/deadline-proposal/accept
Creator принимает предложение (item 9).

### POST /tasks/{task_id}/deadline-proposal/reject
Creator отклоняет предложение (item 9).

### GET /tasks/{task_id}/chat
Чат задачи (item 11). 404 если не видим через `can_see_task`.

### GET /tasks/{task_id}/events?limit=
Audit trail задачи (item 11). Список `task_events` по убыванию времени.

---

## Projects (item 11)

Группировка задач. Head/admin могут создавать/редактировать/удалять.

### GET /projects?include_archived=
Список проектов текущей org.

### POST /projects
Создать проект (head/admin). Body: `{ name: string, description?: string }`.

### GET /projects/{project_id}
Один проект. 404 если cross-org.

### PATCH /projects/{project_id}
Обновить проект (head/admin). Body: `{ name?: string, description?: string }`.

### DELETE /projects/{project_id}
Soft-delete (head/admin). Архивирует чат проекта.

### GET /projects/{project_id}/chat
Резолв чата проекта.

---

## Chats (item 11 + item 10 updates)

### GET /chats/my?type=direct|task|project
Опциональный фильтр по типу чата (item 11).

### GET /chats/{chat_id}/messages
Теперь каждое сообщение содержит поле `references: MessageReference[]` —
per-viewer резолв `!<task>` / `!!<project>` ссылок (item 11).

### POST /chats/{chat_id}/messages
Response формат изменён (item 10):
```json
{
  "user_message": { ... },
  "ai_responses": [],        // пустой при async mode (Kafka enabled)
  "agent_pending": true      // true если есть @@mentions и Kafka работает
}
```
В async режиме AI-ответы прилетают позже через WS `message` event
(доставляются через Kafka `chat.events`). В sync fallback режиме (Kafka
disabled) `ai_responses` содержит ответы inline как раньше.

---

## Task Polls

### GET /task-polls/today
Сегодняшний утренний опрос текущего пользователя.

### GET /task-polls
История опросов.

**Query params:**
- `limit` — количество (default: 30, max: 100)

### GET /task-polls/{poll_id}
Получить опрос.

### POST /task-polls/{poll_id}/submit
Отправить ответы на опрос.

**Request:**
```json
{
  "responses": [
    {
      "task_id": "uuid",
      "new_status": "in_progress",
      "comment": "Работаю над этим"
    }
  ]
}
```

---

## Task Reports

### GET /task-reports
Список вечерних отчётов.

### GET /task-reports/{report_id}
Получить отчёт.

---

## Files

### POST /files/upload
Загрузить файл.

**Request:** multipart/form-data с полями file, user_id, is_public.

### GET /files
Список файлов.

### GET /files/{file_id}
Метаданные файла.

### GET /files/{file_id}/download
Скачать бинарное содержимое файла.

### DELETE /files/{file_id}
Soft-delete файла.

### GET /files/{file_id}/rag-status
Статус RAG-индексации файла.

### PATCH /files/{file_id}/public
Переключить публичность файла (is_public).

---

## RAG

### POST /rag/docs/ingest
Индексировать документ (загрузка + RAG pipeline).

**Request:** multipart/form-data с полем file.

### DELETE /rag/docs/{file_id}
Удалить документ из RAG-индекса.

### POST /rag/docs/{file_id}/retry
Повторить индексацию (через IngestQueue).

### GET /rag/docs/find
Найти релевантные документы.

**Query params:**
- `query` — поисковый запрос (обязательный)
- `top_k` — количество результатов (default: 5)

### GET /rag/docs/{file_id}/search/abstract
Vector-first поиск внутри документа.

**Query params:**
- `query` — поисковый запрос
- `top_k` — количество результатов (default: 5)

### GET /rag/docs/{file_id}/search/concrete
TSV-first поиск внутри документа.

**Query params:**
- `query` — поисковый запрос
- `top_k` — количество результатов (default: 5)
- `tsv_weight` — вес полнотекстового скора (default: 1.0)

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
