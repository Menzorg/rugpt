# RuGPT API Reference

## Base URL

| Env | URL |
|---|---|
| Local dev | `http://127.0.0.1:8100/api/v1` |
| Prod (from webclient через WG) | `http://10.0.0.2/api/v1/web/*` — nginx проксирует на FastAPI `:8100` без изменений; срез `/web` делает движок (`WebSignatureMiddleware`) |

Engine слушает только на loopback (`127.0.0.1:8100`). Внешний доступ — только через nginx на rugpt-container по WireGuard. Подробности — `docs/networking.md`.

Все роутеры монтируются под `/api/v1` в `app.py`, КРОМЕ `chats` — он сам себя префиксует `/api/v1/chats`. Endpoint `GET /` смонтирован в корне приложения (без `/api/v1`).

## Аутентификация

Двухслойная:

1. **JWT HMAC-SHA256** (`Authorization: Bearer <token>`) — обязательна для всех endpoints кроме публичных.
2. **ECDSA P-256 device signature** — обязательна для всех endpoints кроме публичных (включая GET-чтения и `/files/upload`, `/files/{id}/download`); для `/api/v1/web/*` подпись проверяет сам движок через `WebSignatureMiddleware` (делегирует в `SignatureService`), выставляя `request.state.zt_user_id`. `get_current_user` требует совпадения `zt_user_id` с `user_id` из JWT (Zero Trust Plan A, fail-closed). Детали ECDSA-флоу — `docs/networking.md` раздел «App-layer auth».

Query-параметры (или body) для подписанных запросов:

| Параметр | Описание |
|---|---|
| `signature` | ECDSA-подпись payload (base64) |
| `nonce` | 16-байтный random, защита от replay |
| `sig_timestamp` | Unix timestamp (валидное окно ±5 min) |
| `user_id` | UUID пользователя (для резолва `user_devices.public_key`) |

### Публичные endpoints (без подписи — `WEB_NO_SIGNATURE_ROUTES` в `Config`, матч по суффиксу после среза `/api/v1/web`)

- `/auth/login`
- `/auth/engine-public-key`
- `/config`
- `/health`
- `/notifications/telegram/webhook`

Всё остальное (включая `/auth/register`, `/files/upload`, `/files/{id}/download`) требует валидной подписи.

### Rate-limit

**Engine rate-limit не имеет** — открыт в WireGuard без throttling. Rate-limit — только на webclient (Redis-backed): HTTP default 100/min, strict 10/min; WS 100/min/event/user.

### Известные замечания

- **Telegram webhook** (`POST /notifications/telegram/webhook`) — engine за VPN, с публичного интернета **недоступен**. Исходящие вызовы `Engine → api.telegram.org` работают.
- **WebSocket** — на уровне engine отсутствует (WS живёт на webclient). Engine шлёт события в webclient через Kafka `chat.events`.
- **CORS** Engine = `allow_origins=["*"]` — dev-настройка, ослабляет Zero-Trust.

---

## Health

### GET /health
Liveness/health probe.

**Response:**
```json
{
  "status": "healthy",
  "service": "rugpt-engine",
  "timestamp": "2025-01-28T10:00:00"
}
```

### GET /health/ready
Readiness probe: пингует LiteLLM proxy.

**Response:**
```json
{
  "ready": true,
  "litellm": "ok",
  "timestamp": "2025-01-28T10:00:00"
}
```
`litellm` = `"ok"` | `"fail"`; `ready` = true только при успехе.

### GET /health/live
Liveness probe.

**Response:**
```json
{
  "alive": true,
  "timestamp": "2025-01-28T10:00:00"
}
```

---

## Auth

### POST /auth/login
Аутентификация по email/password. Публичный. Опционально регистрирует устройство.

**Request:**
```json
{
  "email": "user@example.com",
  "password": "password123",
  "device_public_key": "base64-ecdsa-pubkey (опционально)",
  "device_name": "MacBook (опционально)"
}
```

**Response (`LoginResponse`):**
```json
{
  "success": true,
  "token": "eyJhbGciOiJIUzI1NiIs...",
  "user_id": "uuid",
  "org_id": "uuid",
  "name": "User Name",
  "username": "username",
  "is_admin": false,
  "role_id": "uuid | null",
  "department_id": "uuid | null",
  "is_head": false,
  "device_id": "uuid | null (если передан device_public_key)",
  "message": null
}
```

При неудаче — `{"success": false, "message": "..."}` (HTTP 200, не 4xx).

### POST /auth/register
Регистрация нового пользователя. Возвращает полный `LoginResponse` с токеном (как login, но без `device_id`/`department_id`/`is_head`).

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
Текущий пользователь (полный `user.to_dict()`).

### POST /auth/refresh
Обновить JWT токен. **Response:** `{"token": "..."}`.

### GET /auth/devices
Список устройств текущего пользователя. **Response:** `{"devices": [...]}`.

---

## Organizations

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
Получить организацию по ID. Юзер видит только свою org (иначе 404). **`OrgResponse`** включает `org_context`, `accountant_user_id`.

### PATCH /organizations/{org_id}
Обновить организацию (admin only).

**Request (все поля опциональны):**
```json
{
  "name": "...",
  "slug": "...",
  "description": "...",
  "timezone": "...",
  "org_context": "...",
  "accountant_user_id": "uuid"
}
```

### DELETE /organizations/{org_id}
Деактивировать организацию (admin only).

### POST /organizations/{org_id}/context/upload
Загрузить файл с описанием структуры организации (admin only, только своя org). Текст извлекается через Tika, пишется в `org_context`, инжектится в system prompt всех ролей.

**Request:** multipart/form-data, поле `file`.
**Response:** `{"status": "ok", "org_context_length": <int>}`.

---

## Users

### GET /users
Список пользователей в org. Фильтрация по видимости отделов (head видит свои). Возвращает `UserResponse[]` с обогащением `role_name`.

### POST /users
Создать пользователя (admin only). `is_admin` всегда форсируется в `False` (руководители — только через CLI/SQL).

**Request:**
```json
{
  "name": "John Doe",
  "username": "john_doe",
  "email": "john@example.com",
  "password": "password123",
  "is_admin": false,
  "role_id": "uuid (опц.)",
  "department_id": "uuid (опц.)"
}
```

**`UserResponse`:**
```json
{
  "id": "uuid",
  "org_id": "uuid",
  "name": "...",
  "username": "...",
  "email": "...",
  "role_id": "uuid | null",
  "role_name": "Юрист | null",
  "is_admin": false,
  "is_system": false,
  "department_id": "uuid | null",
  "department_name": "... | null",
  "is_head": false,
  "is_active": true,
  "avatar_url": "... | null",
  "created_at": "ISO8601",
  "updated_at": "ISO8601",
  "last_seen_at": "ISO8601 | null"
}
```

### GET /users/system
Системные AI-пользователи (`is_system=true`).

### GET /users/{user_id}
Получить пользователя (в пределах своей org, проверка видимости).

### GET /users/username/{username}
Получить пользователя по username (проверка видимости).

### PATCH /users/{user_id}
Обновить пользователя. Admin — любого в org; пользователь — только себя. `is_admin`/`role_id`/`department_id` меняет только admin.

**Request (все поля опциональны):**
```json
{
  "name": "...",
  "username": "...",
  "email": "...",
  "avatar_url": "...",
  "is_admin": true,
  "role_id": "uuid",
  "department_id": "uuid"
}
```

### POST /users/{user_id}/password
Сменить пароль (только свой).

**Request:**
```json
{ "current_password": "old", "new_password": "new" }
```

### POST /users/{user_id}/role
Назначить роль пользователю (admin only). **Request:** `{ "role_id": "uuid | null" }` (null = снять).

### DELETE /users/{user_id}
Деактивировать пользователя (admin only). Нельзя себя.

---

## Roles

> **Роли предсозданы** через миграции/seed. CRUD через API отсутствует. Доступно только чтение и управление кешем промптов.

### GET /roles
Список ролей в org. **Response (`RoleResponse[]`)** включает `as_subagent_description`.

```json
[
  {
    "id": "uuid",
    "org_id": "uuid",
    "name": "Юрист",
    "code": "lawyer",
    "description": "...",
    "as_subagent_description": "...",
    "system_prompt": "...",
    "rag_collection": null,
    "model_name": "...",
    "agent_type": "simple",
    "agent_config": {},
    "tools": ["calendar_create", "calendar_query"],
    "prompt_file": "lawyer.md",
    "is_active": true,
    "created_at": "ISO8601",
    "updated_at": "ISO8601"
  }
]
```

### GET /roles/{role_id}
Получить роль.

### GET /roles/code/{code}
Получить роль по коду.

### GET /roles/{role_id}/users
Пользователи с этой ролью, отфильтрованные по видимости отделов.

**Response:**
```json
{
  "role": { ... },
  "users": [ { "id": "uuid", "name": "...", "username": "...", ... } ]
}
```

### POST /roles/admin/cache/prompts/clear
Сбросить кеш всех промптов (admin only). **Response:** `{"success": true, "message": "All prompt caches cleared"}`.

### POST /roles/admin/cache/prompts/clear/{role_code}
Сбросить кеш промпта конкретной роли (admin only).

---

## Chats

Роутер самопрефиксован `/api/v1/chats`.

### GET /chats/my
Чаты текущего пользователя. **Query:** `type` (опц., `direct | task | project`).

### GET /chats/unread-counts
Bulk-счётчики непрочитанного. **Response:** `{ "<chat_id>": <count>, ... }` (только count > 0).

### POST /chats/direct
Создать/получить direct chat с пользователем (проверка видимости отделов).

**Request:** `{ "other_user_id": "uuid" }`. **Response:** `ChatResponse`.

> Группового создания чата нет — `/chats/group` не существует. Task- и project-чаты создаются автоматически движком.

### GET /chats/pending-review
AI-ответы ожидающие проверки текущим пользователем (`ai_is_valid IS NULL`).

### GET /chats/reviewed
AI-ответы уже проверенные (`ai_is_valid IS NOT NULL`). **Query:** `limit` (default 50).

### GET /chats/unvalidated
Deprecated: используйте `/pending-review`.

### GET /chats/{chat_id}
Получить чат. **`ChatResponse`** включает `task_id`, `project_id`.

### POST /chats/{chat_id}/participants/{participant_id}
Добавить участника (проверка видимости). **Response:** `{"success": true}`.

### DELETE /chats/{chat_id}/participants/{participant_id}
Удалить участника.

### DELETE /chats/{chat_id}
Архивировать чат.

### GET /chats/{chat_id}/messages
Сообщения чата (cursor-based). Каждое сообщение содержит `references` (per-viewer резолв `!<task>` / `!!<project>`) и `attachments` (с per-viewer полями `cloned_by_me_id` / `cloned_by_me_rag_status`).

**Query:** `limit` (default 50), `before_id` (UUID, пагинация назад).

**`MessageResponse`:**
```json
{
  "id": "uuid",
  "chat_id": "uuid",
  "sender_type": "user | ai_role | ...",
  "sender_id": "uuid",
  "content": "...",
  "mentions": [ { "type": "user|ai_role", "user_id": "uuid", "username": "...", "position": 0 } ],
  "references": [ { "type": "task|project", "id": "uuid", "title": "...", "accessible": true, "position": 0 } ],
  "reply_to_id": "uuid | null",
  "ai_is_valid": null,
  "ai_edited": false,
  "is_deleted": false,
  "created_at": "ISO8601",
  "updated_at": "ISO8601",
  "attachments": [ { "id": "uuid", "position": 0, "original_filename": "...", "file_size": 0, "file_type": "...", "is_deleted": false, "cloned_by_me_id": "uuid | null", "cloned_by_me_rag_status": "indexed | null" } ]
}
```

### POST /chats/{chat_id}/messages
Отправить сообщение. `file_ids` (≤5) к AI-direct-чату триггерит синхронную RAG-индексацию вложений перед сохранением.

**Request:**
```json
{
  "content": "Привет @@lawyer, проверь договор",
  "reply_to_id": "uuid (опц.)",
  "file_ids": ["uuid", "..."]
}
```

**Response (`SendMessageResponse`):**
```json
{
  "user_message": { ... },
  "ai_responses": [],
  "agent_pending": true
}
```
В async-режиме (Kafka enabled) `ai_responses` пуст, `agent_pending=true` при `@@mentions`/системном участнике; ответы прилетают позже через WS `message` (Kafka `chat.events`). В sync fallback `ai_responses` содержит ответы inline.

### POST /chats/{chat_id}/read
Отметить сообщения в чате прочитанными до `message_id` включительно. **204.** **Request:** `{ "message_id": "uuid" }`.

### GET /chats/messages/{message_id}
Получить сообщение по ID.

### POST /chats/messages/{message_id}/validate
Подтвердить AI-ответ (`ai_is_valid = true`). Разрешено admin ИЛИ владельцу роли (`sender_id == user_id`). Можно передать отредактированный текст.

**Request (опц.):** `{ "edited_content": "..." }`.

### DELETE /chats/messages/{message_id}
Удалить сообщение (soft delete).

### POST /chats/messages/{message_id}/reject
Отклонить AI-ответ и создать correction rule. Разрешено admin ИЛИ владельцу роли.

**Request:** `{ "correction_text": "..." }`.
**Response:** `CorrectionRuleResponse` (`id, role_id, mem_id, src_user_message_id, src_ai_response_id, user_correction_text, extracted_lesson, is_active`).

### POST /chats/messages/{message_id}/reply
Ответить на упоминающее сообщение, не вступая в чат участником. Гейт: отправитель должен быть в `mentions`. Single-use (409 при повторе). Реплай публикуется в Kafka `chat.events`.

**Request:** `{ "content": "..." }`. **Response:** `MessageResponse`.

---

## Calendar

### GET /calendar/events
Список активных событий в org.

```json
[
  {
    "id": "uuid", "role_id": "uuid", "org_id": "uuid",
    "title": "Проверка договора", "description": "...",
    "event_type": "recurring", "scheduled_at": null,
    "cron_expression": "0 10 1 * *",
    "next_trigger_at": "2026-03-01T10:00:00",
    "last_triggered_at": "2026-02-01T10:00:00",
    "trigger_count": 5,
    "source_chat_id": null, "source_message_id": null,
    "metadata": {}, "created_by_user_id": "uuid",
    "is_active": true,
    "created_at": "...", "updated_at": "..."
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
  "description": "...",
  "event_type": "one_time",
  "scheduled_at": "2026-03-15T09:00:00",
  "cron_expression": null
}
```
Для рекуррентных: `event_type: "recurring"` + `cron_expression`.

### GET /calendar/events/{event_id}
Получить событие.

### PATCH /calendar/events/{event_id}
Обновить событие (`title`, `description`, `scheduled_at`, `cron_expression`, `metadata`).

### DELETE /calendar/events/{event_id}
Деактивировать событие.

### GET /calendar/roles/{role_id}/events
События для конкретной роли.

---

## Notifications

### GET /notifications/channels
Каналы уведомлений текущего пользователя.

### POST /notifications/channels
Зарегистрировать/обновить канал. `channel_type` ∈ {`telegram`, `email`}.

**Request:**
```json
{ "channel_type": "telegram", "config": {"chat_id": "123456789"}, "priority": 10 }
```

### DELETE /notifications/channels/{channel_type}
Удалить канал.

### POST /notifications/channels/{channel_type}/verify
Подтвердить канал.

### POST /notifications/telegram/webhook
Telegram Bot webhook. Публичный (без подписи и JWT). Обрабатывает `/start <user_id>` — привязка Telegram к пользователю (создаёт+верифицирует канал telegram, priority=10). **Response:** `{"ok": true}`.

### GET /notifications/log
Лог доставки уведомлений текущего пользователя. **Query:** `limit` (default 50, max 200).

---

## Departments (admin only, кроме чтений)

### POST /departments
Создать отдел (admin). **Request:** `{ "name": "..." }`.

### GET /departments
Список отделов org (доступно всем аутентифицированным).

### GET /departments/{id}
Получить отдел.

### PATCH /departments/{id}
Обновить отдел (admin). **Request:** `{ "name": "..." }`.

### DELETE /departments/{id}
Удалить отдел (admin; users → NULL, rules → CASCADE).

### POST /departments/{id}/head/{user_id}
Назначить руководителя (admin).

### DELETE /departments/{id}/head
Снять руководителя (admin).

### POST /departments/visibility
Создать правило видимости (симметричное, admin). **Request:** `{ "department_a_id": "uuid", "department_b_id": "uuid" }`.

### DELETE /departments/visibility/{id}
Удалить правило видимости (admin).

### GET /departments/visibility
Список правил видимости org.

---

## In-App Notifications

### GET /in-app-notifications
Список уведомлений текущего пользователя.

**Query:** `limit` (1..200, default 50), `offset` (default 0), `unread_only` (bool), `type` (фильтр по типу), `replied` (bool: отвеченные/неотвеченные mention'ы).
Каждый элемент включает `replied`, `reference_type`, `reference_id`.

### GET /in-app-notifications/unread-count
Счётчик непрочитанных. **Response:** `{ "count": 5 }`.

### PATCH /in-app-notifications/{id}/read
Отметить прочитанным.

### POST /in-app-notifications/read-all
Отметить все прочитанными. **Response:** `{"success": true, "marked_count": <int>}`.

---

## Tasks

### GET /tasks
Legacy общий список. Admin видит все задачи org, сотрудник — только свои. **Query:** `status`, `assignee_user_id`. Head (не admin) фильтруется по видимости отделов.

### POST /tasks
Создать задачу. `created_by_user_id` — текущий пользователь. Проверка видимости assignee.

**Request:**
```json
{
  "title": "Подготовить отчёт",
  "description": "...",
  "assignee_user_id": "uuid",
  "deadline": "2026-04-15T18:00:00",
  "project_id": "uuid (опц.)",
  "priority": 2,
  "participant_user_ids": ["uuid", "..."]
}
```
`priority` — 1..3 (иначе 400); по умолчанию выводится из роли создателя.

### GET /tasks/my
Задачи где юзер — assignee, `status != done`, сортировка по приоритету+дедлайну. **Query:** `project_id` (опц.).

### GET /tasks/created-by-me
Задачи где юзер — creator, `status != done`. **Query:** `project_id` (опц.).

### GET /tasks/participating
Задачи где юзер в `task_participants`, `status != done`. **Query:** `project_id` (опц.).

### GET /tasks/done
Все задачи `status='done'` где юзер — creator, assignee или participant.

### GET /tasks/archive
Архив: done/cancelled (`is_active=false`), где юзер был creator или assignee. **Query:** `limit` (1..1000, default 200).

### GET /tasks/{task_id}
Получить задачу. Видна creator/assignee/participant/admin. Включает `participants`.

### PATCH /tasks/{task_id}
Обновить задачу. Только creator. Статус НЕ меняется здесь (через `/take`, `/mark-done`, `/accept`, `/reject`). Заблокировано на терминальных статусах (done/cancelled → 409).

**Request (все опц.):**
```json
{
  "title": "...",
  "description": "...",
  "assignee_user_id": "uuid",
  "deadline": "ISO8601",
  "project_id": "uuid",
  "detach_project": false,
  "priority": 2
}
```
Семантика проекта: `detach_project=true` → отвязать; `project_id` задан → переназначить; оба пусты → без изменений.

### DELETE /tasks/{task_id}
Soft-delete (только creator).

### POST /tasks/{task_id}/take
Assignee берёт в работу: `created → in_progress`.

### POST /tasks/{task_id}/mark-done
Assignee отмечает готово: `in_progress → awaiting_review`.

### POST /tasks/{task_id}/accept
Creator принимает: `awaiting_review → done`.

### POST /tasks/{task_id}/reject
Creator возвращает: `awaiting_review → in_progress`. **Request:** `{ "comment": "..." (опц.) }`.

### PATCH /tasks/{task_id}/deadline
Creator меняет срок напрямую. **Request:** `{ "deadline": "ISO8601" }`.

### POST /tasks/{task_id}/deadline-proposal
Assignee предлагает новый срок. **Request:** `{ "proposed_deadline": "ISO8601" }`.

### POST /tasks/{task_id}/deadline-proposal/accept
Creator принимает предложение.

### POST /tasks/{task_id}/deadline-proposal/reject
Creator отклоняет предложение.

### GET /tasks/{task_id}/chat
Чат задачи. 404 если задача/чат не видимы (`can_see_task`).

### GET /tasks/{task_id}/events
Audit trail задачи. **Query:** `limit` (1..500, default 100).

### POST /tasks/{task_id}/participants
Добавить участника. **201** + `{id, name}`. 409 на дубликат, 403 на права. **Request:** `{ "target_user_id": "uuid" }`.

### DELETE /tasks/{task_id}/participants/{target_user_id}
Удалить участника. **204.** 404 если нет, 403 на права.

---

## Projects

Группировка задач.

### GET /projects
Список проектов, видимых пользователю (per-viewer scope). **Query:** `include_archived` (bool, default false).

### POST /projects
Создать проект. **Доступно любому аутентифицированному** — сохраняет `department_id` создателя. **Request:** `{ "name": "...", "description": "..." }`.

### GET /projects/{project_id}
Один проект. 404 если не найден или cross-org.

### PATCH /projects/{project_id}
Обновить проект (creator, admin, или head отдела проекта — enforced в сервисе). **Request:** `{ "name": "...", "description": "..." }`.

### DELETE /projects/{project_id}
Soft-delete + архивация чата проекта (creator/admin/head). Задачи сохраняют `project_id`.

### GET /projects/{project_id}/chat
Резолв чата проекта. 404 если проект/чат отсутствует.

---

## Task Polls

### GET /task-polls/today
Сегодняшний утренний опрос текущего пользователя. `null` если нет.

### GET /task-polls/today/chat
Чат сегодняшнего опроса (опрос ведётся как диалог в чате). 404 если опроса нет, 500 при отсутствии чата у существующего опроса. **Response:** `{chat_id, poll_id, status}`.

### GET /task-polls
История опросов. **Query:** `include_completed` (bool, default false — только pending), `limit` (1..100, default 30).

### GET /task-polls/{poll_id}
Получить опрос (только свой).

### POST /task-polls/{poll_id}/submit
Завершить опрос — ставит генерацию summary в очередь Kafka. **Тело не принимается. Возвращает 202.**
Валидация: 404 (нет опроса), 403 (не assignee), 409 (статус не `pending` / нет сообщений от assignee), 500 (data-integrity), 503 (Kafka publish failed).

---

## Task Reports

### GET /task-reports
Вечерние отчёты текущего пользователя. Не-admin получает `[]`. **Query:** `limit` (1..100, default 30).

### GET /task-reports/{report_id}
Получить отчёт. Виден только admin и только владельцу отчёта (`generated_for_user_id`), в пределах своей org.

---

## Files

### POST /files/upload
Загрузить файл.

**Request:** multipart/form-data:
- `file` — файл
- `target_user_id` — UUID владельца; **admin-only override**, иначе только свой (опц.)
- `is_public` — bool (default false)
- `folder_id` — UUID папки (опц.; проверяется владение)

**Response:** `FileResponse` (`id, user_id, org_id, uploaded_by_user_id, storage_key, original_filename, file_type, file_size, rag_status, rag_error, indexed_at, is_table, is_public, is_active, created_at, updated_at, cloned_from_file_id, folder_id`).

### GET /files
Список файлов. **Query:** `folder_id` = `null` (только корень текущего юзера) | UUID | опустить (admin → все по org, иначе все свои).

### GET /files/{file_id}
Метаданные файла (в пределах своей org).

### GET /files/{file_id}/download
Скачать бинарь. Доступ: в пределах своей org. Имя файла кодируется RFC 6266/5987.

### DELETE /files/{file_id}
Soft-delete файла (в пределах своей org).

### POST /files/{file_id}/index
Запустить RAG-индексацию файла. Идемпотентно. **Response:** `FileResponse`. 403/404 при отсутствии доступа.

### POST /files/{file_id}/clone
«В мои файлы»: metadata-клон файла, приложенного к чату. Гейт: юзер — участник хотя бы одного чата с этим вложением. **Response:** `FileResponse` клона.

### GET /files/{file_id}/rag-status
Статус RAG-индексации. **Response:** `{file_id, rag_status, rag_error, indexed_at}`.

### PATCH /files/{file_id}/public
Переключить публичность. **Admin И владелец файла.** **Request:** `{ "is_public": bool }`.

### PATCH /files/{file_id}/folder
Переместить файл в папку (или в корень при `folder_id: null`). **Request:** `{ "folder_id": "uuid | null" }`.

---

## Folders (`/api/v1/folders`)

Admin может передать `?user_id=<uuid>` чтобы работать с папками другого юзера своей org (на `POST` / `GET` / `GET /tree`).

| Endpoint | Описание |
|----------|----------|
| `POST /folders` | Создать папку. Body `{name, parent_folder_id?}` |
| `GET /folders` | Плоский список папок юзера |
| `GET /folders/tree` | Дерево с `children: []` |
| `GET /folders/{id}` | Метаданные папки |
| `PATCH /folders/{id}` | Rename и/или move. Body `{name?, parent_folder_id?}` (`null` → корень) |
| `DELETE /folders/{id}` | Каскадный soft-delete. Response `{success, deleted_folders, deleted_files}` |
| `GET /folders/{id}/files` | Файлы непосредственно в папке |

### Folder errors

Все 4xx folder-операций возвращают `detail: {code, message}`:

| code | status | сценарий |
|---|---|---|
| EMPTY_NAME | 400 | пустое/whitespace имя |
| NAME_TOO_LONG | 400 | имя > 255 символов |
| MAX_DEPTH_EXCEEDED | 400 | глубина > 10 уровней |
| CYCLIC_MOVE | 400 | new_parent ∈ subtree(folder) или new_parent == folder_id |
| INVALID_PARENT_OWNER | 400 | parent.user_id != owner |
| FORBIDDEN | 403 | actor не owner и не admin той же org |
| FOLDER_NOT_FOUND | 404 | папки нет или is_active=false |
| PARENT_NOT_FOUND | 404 | parent_folder_id указан, но папки нет |
| DUPLICATE_NAME | 409 | имя дублируется в одном parent (case-insensitive) |

---

## RAG

### POST /rag/docs/ingest
Индексировать документ (upload + RAG pipeline). **Request:** multipart/form-data, поле `file`.

### DELETE /rag/docs/{file_id}
Удалить документ из RAG-индекса (только владелец).

### POST /rag/docs/{file_id}/retry
Повторить индексацию (через IngestQueue). **Response:** `{status: "queued", file_id}`.

### GET /rag/docs/find
Найти релевантные документы.

**Query:**
- `query` — поисковый запрос (обязательный, min_length=1)
- `top_k` — default 5 (>0)
- `user_id` — UUID, искать по файлам другого юзера (опц.; default — текущий)
- `search_mode` — `abstract` (vector-first, default) | `concrete` (TSV-first)

### GET /rag/docs/{file_id}/search/abstract
Vector-first поиск внутри документа. **Query:** `query`, `top_k` (default 5).

### GET /rag/docs/{file_id}/search/concrete
TSV-first поиск внутри документа. **Query:** `query`, `top_k` (default 5), `tsv_weight` (default 1.0).

---

## Corrections (`/api/v1/corrections`, admin only)

CRUD правил коррекции — связь некорректного AI-ответа с правкой пользователя и извлечённым уроком.

### GET /corrections
Список правил. **Query:** `role_id` (опц., фильтр). **Response:** `CorrectionRuleResponse[]` (`id, role_id, src_ai_response_id, user_correction_text, extracted_lesson`).

### GET /corrections/{correction_id}
Получить правило по ID.

### POST /corrections
Создать правило из AI-сообщения и текста правки. **201.** `extracted_lesson` выводится автоматически. **Request:** `{ "corrected_message_id": "uuid", "user_correction_text": "..." }`.

### PATCH /corrections/{correction_id}
Обновить `corrected_message_id` и/или `user_correction_text`. **Request:** `{ "corrected_message_id": "uuid", "user_correction_text": "..." }`.

### DELETE /corrections/{correction_id}
Деактивировать правило. **204.**

---

## Actions (`/api/v1/actions`)

### POST /actions/{action_type}
Универсальный диспетчер «AI готовит — человек решает». Единая точка для action-флоу (фронт POST'ит выбранный `action_type` + параметры). Авторизация/валидация/побочные эффекты — в хендлерах `src/engine/actions/`.

**Request body:** произвольный JSON-объект `params`.
**Errors:** 404 (`UnknownActionError`), 403 (`PermissionDeniedError`), 400 (`ActionError`).
**Response:** результат хендлера (форма зависит от `action_type`).

> Action-хендлеры invoice-флоу (approve/reject/mark_processed) живут здесь, не под `/invoices`.

---

## Invoices (`/api/v1/invoices`)

### POST /invoices/
Загрузить инвойс. Любой активный пользователь org. **Request:** multipart/form-data:
- `file` — файл (обязателен, пустой → 400)
- `due_date` — `YYYY-MM-DD` (опц.; невалидный формат → 400)

**Response:** `invoice.to_dict()`.

### GET /invoices/
Список инвойсов. Admin видит все в org, остальные — только свои. **Query:** `status` (опц.; невалидный → 400).

### GET /invoices/{invoice_id}
Получить инвойс (владелец или admin той же org). 404 если нет/cross-org/нет доступа.

---

## Support (`/api/v1/support`)

Жизненный цикл тикетов техподдержки. Операторские роуты доступны только членам org RuGPT Support (`Config.RUGPT_SUPPORT_ORG_ID`).

### POST /support/tickets
Создать тикет + выделенный чат. Роутинг внутри сервиса: `how_to` → AI вступает в чат, очередь не уведомляется; `bug`/`other` → handoff + уведомление очереди.

**Request:** `{ "category": "how_to | bug | other", "initial_message": "..." }`.
**Response:** `{ "ticket": {...}, "chat_id": "uuid" }`.

### GET /support/tickets/my
Свои тикеты (DESC по created_at). **Query:** `status` (`open|in_progress|closed`), `limit` (1..200, default 50).

### GET /support/tickets/{ticket_id}
Один тикет. Доступ: requester или оператор Support. Оператору добавляются cross-org поля `requester_name`, `requester_email`, `requester_org_name`, `requester_org_id`. Иначе 403.

### POST /support/tickets/{ticket_id}/escalate
Клиент вызывает оператора — ставит `ai_handoff_at` + уведомляет очередь.

### POST /support/tickets/{ticket_id}/close
Закрыть тикет (любая сторона).

### GET /support/queue
Неназначенные открытые тикеты (operator only). **Query:** `limit` (1..500, default 100).

### POST /support/tickets/{ticket_id}/take
Взять тикет из очереди (atomic CAS, operator only). 409 если уже взят.

### GET /support/operator/my
Тикеты, назначенные текущему оператору (operator only). **Query:** `limit` (1..500, default 100).

### GET /support/tickets/{ticket_id}/chat
Резолв `chat_id` тикета. Доступ: requester или оператор. **Response:** `{ "chat_id": "uuid" }`.

### GET /support/tickets/{ticket_id}/events
Audit trail тикета (DESC). Доступ: requester или оператор. **Query:** `limit` (1..500, default 200).

---

## Коды ошибок

| HTTP Code | Описание |
|-----------|----------|
| 400 | Неверный запрос |
| 401 | Не авторизован / unsigned / identity mismatch |
| 403 | Доступ запрещён |
| 404 | Не найдено |
| 409 | Конфликт (дубликат, уже взято, терминальный статус) |
| 500 | Внутренняя ошибка |
| 503 | Сервис временно недоступен (Kafka publish failed) |

## Формат ошибки

```json
{ "detail": "Error message" }
```

Folder-операции возвращают структурированный detail: `{ "detail": {"code": "...", "message": "..."} }`.
