# Отчёт по альфа-фичам RuGPT

Дата: 2026-03-09
Обновлено: 2026-04-22

---

## 1. Задачи (Tasks + Polls + Reports)

**Статус: 100% engine, 100% webclient**

### Модель данных

3 таблицы (миграции 005, 006, 007):

- **tasks** — id, title, description, status (`created | in_progress | awaiting_review | done | cancelled | overdue`), assignee_user_id, **created_by_user_id** (item 9), **project_id** (item 11), deadline, proposed_deadline + proposed_deadline_by (item 9), awaiting_review_at (item 9), org_id
- **task_polls** — утренний опрос: assignee_user_id, poll_date, status (`pending | completed | expired`), responses (JSONB: `[{task_id, new_status, comment}]`), expires_at
- **task_reports** — вечерний отчёт: generated_for_user_id (менеджер), report_date, content (текст), task_summaries (JSONB: `[{task_id, assignee_name, old_status, new_status, comment, poll_completed}]`)

### Флоу

**Утро 09:00:**
1. SchedulerService создаёт `TaskPoll` для каждого сотрудника с активными задачами
2. InAppNotification в колокольчик: "Утренний опрос по задачам"
3. Сотрудник на странице Tasks/Polls выбирает новый статус для каждой задачи + комментарий
4. `POST /task-polls/{id}/submit` — обновляет статусы задач, помечает опрос как completed

**Вечер 18:00:**
1. Expire неотвеченных опросов (status → expired)
2. Пометка просроченных задач как `overdue`, уведомление исполнителю
3. Генерация `TaskReport` для каждого менеджера
4. InAppNotification: "Вечерний отчёт по задачам"

### API

| Endpoint | Метод | Описание |
|----------|-------|----------|
| `/api/v1/tasks` | GET | Список задач (фильтр status, assignee_user_id) |
| `/api/v1/tasks` | POST | Создать задачу |
| `/api/v1/tasks/{id}` | PATCH | Обновить задачу |
| `/api/v1/tasks/{id}` | DELETE | Деактивировать |
| `/api/v1/task-polls/today` | GET | Опрос на сегодня |
| `/api/v1/task-polls` | GET | История опросов |
| `/api/v1/task-polls/{id}/submit` | POST | Отправить ответы |
| `/api/v1/task-reports` | GET | Список отчётов менеджера |
| `/api/v1/task-reports/{id}` | GET | Конкретный отчёт |

### Агентные инструменты

`task_create`, `task_query`, `task_update` — зарегистрированы в ToolRegistry, доступны ролям через `role.tools`.

### Frontend

Страница `/tasks` с 3 вкладками: Tasks (список + создание), Polls (сегодняшний опрос + история), Reports (список отчётов + детали).

### Не сделано

- ~~Scheduler jobs не подключены~~ -- **ИСПРАВЛЕНО**: `_run_morning_polls_for_org()` и `_run_evening_reports_for_org()` работают в `_process_task_jobs()` с per-org timezone
- AI-генерация текста отчётов -- plain text (не через LLM)
- ~~Конфиги не в .env~~ -- используются morning_hours/evening_hours в конструкторе SchedulerService (не cron)

---

## 2. Колокольчик (In-App Notifications)

**Статус: полностью реализовано**

### Модель данных

Таблица `in_app_notifications` (миграция 008):
- type: `new_task | poll | report | mention | task_status_change | system`
- title, content
- reference_type (`task | task_poll | task_report | message`) + reference_id
- is_read (boolean)

### API

| Endpoint | Метод | Описание |
|----------|-------|----------|
| `/api/v1/in-app-notifications` | GET | Список (limit, offset, unread_only) |
| `/api/v1/in-app-notifications/unread-count` | GET | Счётчик для бейджа |
| `/api/v1/in-app-notifications/{id}/read` | PATCH | Прочитать одно |
| `/api/v1/in-app-notifications/read-all` | POST | Прочитать все |

### Frontend

- `BellIcon` + `NotificationDropdown` в `ChatNavigation`
- Хук `useNotifications` с polling каждые 30 секунд
- Бейдж с unreadCount (max "99+")
- Клик по уведомлению → mark as read

### Кто создаёт уведомления

| Сервис | Тип | Когда |
|--------|-----|-------|
| TaskService | `new_task` | Создание задачи для сотрудника |
| TaskService | `task_status_change` | Задача просрочена |
| TaskPollService | `poll` | Утренний опрос |
| TaskReportService | `report` | Вечерний отчёт |

### Не сделано

- WebSocket push (только HTTP polling 30с)
- Навигация к reference при клике (task/poll/report)
- Фильтрация по типу на фронте
- Очистка/TTL старых уведомлений
- Mention-уведомления (type pre-wired, но не создаются)

---

## 3. Загрузка файлов (Files)

**Статус: полностью реализовано (RAG реализован)**

### Модель данных

Таблица `user_files` (миграция 009):
- user_id (владелец — сотрудник), uploaded_by_user_id (загрузил — менеджер)
- storage_key: `{org_id}/{user_id}/{file_id}.{ext}`
- file_type (pdf, docx), file_size (max 50MB)
- rag_status: `pending | indexing | indexed | failed`
- rag_error, indexed_at

### Хранение бинарных файлов

`StorageAdapter` (абстракция) → `LocalStorageAdapter` (файлы в `/root/rugpt/uploads/`).

### API

| Endpoint | Метод | Описание |
|----------|-------|----------|
| `/api/v1/files/upload` | POST | Загрузка (multipart, pdf/docx, max 50MB) |
| `/api/v1/files` | GET | Список файлов (фильтр user_id) |
| `/api/v1/files/{id}` | GET | Метаданные файла |
| `/api/v1/files/{id}/download` | GET | Скачать бинарный файл |
| `/api/v1/files/{id}` | DELETE | Soft-delete |

### Frontend

Страница `/files`:
- Список файлов с RAG-статусом (бейджи: pending/indexing/indexed/error)
- Upload modal (только admin, выбор сотрудника)
- Download/delete кнопки

### Не сделано

- ~~RAG-индексация~~ -- **РЕАЛИЗОВАНО**: полный RAG pipeline (Tika + pgvector + hybrid search). См. `docs/rag-info.md`
- ~~`rag_search` stub~~ -- **РЕАЛИЗОВАНО**: двухуровневый гибридный поиск (docs -> chunks)
- S3 интеграция (S3StorageAdapter) -- не реализована, используется LocalStorageAdapter
- Presigned URLs для прямого скачивания -- не реализовано

---

## 4. Упоминания + Правила коррекции (Mentions + Correction Rules)

**Статус: полностью реализовано (без RAG для правил)**

### Флоу упоминаний

1. Пользователь пишет `@@lawyer проверь договор`
2. `MentionService` парсит `@@` → находит user с username `lawyer`
3. `AIService.process_ai_mentions()` → загружает роль → вызывает LLM через `AgentExecutor`
4. AI-ответ сохраняется с `ai_is_valid = NULL` (pending review)
5. Владелец роли видит ответ на странице `/mentions`

### Жизненный цикл AI-сообщения

```
ai_is_valid = NULL (pending)
├── Утвердить → ai_is_valid = true
│   └── опционально: ai_edited = true (если контент изменён)
└── Отклонить → ai_is_valid = false
    ├── Комментарий отправляется в чат (reply к AI-сообщению)
    ├── Создаётся CorrectionRule
    └── Фоново генерируется rule_text через LLM
```

### Модель данных

Таблица `correction_rules` (миграция 010):
- role_id, org_id
- original_message_id (вопрос), ai_message_id (отклонённый ответ)
- user_question, ai_answer, correction_text (что не так)
- rule_text (nullable — генерируется async через LLM)
- created_by_user_id

### Генерация rule_text

`rule_generator.py` — LangGraph граф, промпт формулирует правило вида:
> "Когда [ситуация], [правильное действие/ответ]."

Вызывается async после создания CorrectionRule, результат сохраняется в `correction_rules.rule_text`.

### API

| Endpoint | Метод | Описание |
|----------|-------|----------|
| `/api/v1/chats/pending-review` | GET | Список AI-ответов на проверку |
| `/api/v1/chats/messages/{id}/validate` | POST | Утвердить AI-ответ |
| `/api/v1/chats/messages/{id}/reject` | POST | Отклонить + создать правило |

### Frontend

Страница `/mentions` — `MentionReviewList`:
- Список pending AI-ответов (Markdown с подсветкой кода)
- Кнопки "Подтвердить" (зелёная) / "Отклонить" (серая)
- При отклонении — textarea для комментария
- Коммуникация через WebSocket (`message:validate`, `message:reject`, `messages:pending-review`)

### Не сделано

- RAG-поиск релевантных правил при генерации ответа (сейчас `get_rules_for_role()` возвращает ВСЕ активные правила)
- Инъекция правил в system prompt перед вызовом LLM — TODO в `executor.py`
- Семантический поиск по rule_text через vector store

---

## 5. Отделы и права видимости (item 8)

**Статус: 100% engine, 100% webclient (2026-04-13)**

Миграция 015. Модели `Department`, `DepartmentVisibility`. Плоский список отделов, симметричные правила видимости между отделами. `DepartmentService.get_visible_user_ids(viewer)` — единая точка фильтрации. Видимость встроена в users/chats/tasks/roles/mentions/task_tool. Поле `org_context` на организации + загрузка файла через Tika для инъекции описания структуры в system prompt всех ролей.

API: 10 эндпоинтов `/api/v1/departments/*` (admin-only). Frontend: страница `/departments` с 3 табами, колонка "Отдел" в `/users`, группировка по отделам в sidebar.

## 6. Приоритизация и владение задачами (item 9)

**Статус: 100% engine, 100% webclient**

Миграция 016. Поля на `tasks`: `created_by_user_id`, `awaiting_review_at`, `proposed_deadline`, `proposed_deadline_by`.

Новый статусный флоу:
```
created  ─take→  in_progress  ─mark_done→  awaiting_review  ─accept→  done
                                                  │
                                                  └─reject+comment→  in_progress
```

Deadline-negotiation: assignee может предложить (`proposed_deadline`), creator принимает/отклоняет. Только creator может напрямую изменить дедлайн или переназначить.

Приоритет вычисляется по роли создателя (`is_admin > is_head > обычный`) — сортировка в списке `/tasks/my`.

API: `POST /tasks/{id}/take`, `mark-done`, `accept`, `reject`, `deadline-proposal`, `deadline-proposal/accept|reject`, `PATCH /tasks/{id}/deadline`. Новые GET: `/tasks/my`, `/tasks/created-by-me`.

## 7. PM-агент + Kafka event bus (item 10)

**Статус: 100% engine, 100% webclient (2026-04-14, требует manual verification в браузере)**

Миграция 018 (роль `pm` + system user `pm` + таблица `agent_runs`).

**Kafka инфраструктура:**
- Apache Kafka 3.7 (KRaft) на docker-vm (B.II.2, `192.168.1.84:9092`)
- Два топика: `agent.requests` (3p, 24h, idempotent, acks=all, partition_key=request_id) и `chat.events` (3p, 1h, partition_key=chat_id)
- Auth **отсутствует** (trust by VPN; план SASL_PLAINTEXT после Alpha)
- `aiokafka` в engine, `kafkajs` в NestJS
- `Config.KAFKA_ENABLED=false` → весь Kafka-код no-op, sync fallback

**PM-агент (`TaskNotificationService`):**
- Автоматически постит от имени system user `pm` в личный direct-chat PM↔recipient
- 9 методов уведомления (take, mark_done, accept, reject, deadline-related, overdue)
- Правило: уведомляется сторона, которая **не инициировала** изменение
- Hook в `TaskService` через `_notify(method, ...)` после каждого перехода

**Async агентный инференс:**
- Таблица `agent_runs(request_id PK, status, ...)` — идемпотентность Kafka redelivery через атомарный CAS `pending → running`
- `AgentRequestHandler` — Kafka consumer:
  1. CAS pending → running (skip если уже running/done)
  2. Load user message
  3. `AIService.generate_response()` (LangChain ReAct + tools + LiteLLM)
  4. `mark_done` + publish `chat.events`
  5. При exception: `mark_failed` + re-raise (offset не коммитится → redeliver)
- `POST /chats/{id}/messages` возвращает `agent_pending=true` в async-режиме — HTTP возвращается мгновенно, ответ приходит через WS

**WebClient сторона:**
- NestJS `KafkaConsumerService` подписан на `chat.events` с per-instance consumer group (broadcast pattern)
- `SocketGateway.broadcastToChat(chatId, msg)` → `io.to('chat:<id>').emit(...)`

**Тесты:** 131 passed engine, 22 новых (async idempotency, kafka e2e).

## 8. Проекты и чаты задач (item 11)

**Статус: 100% engine, 100% webclient (2026-04-13, требует manual verification)**

Миграция 017. Модели `Project`, `TaskEvent`. Колонки `chats.task_id`, `chats.project_id`, `tasks.project_id`.

**Проекты:**
- Отдельная таблица `projects` (не text-поле на tasks — можно soft-delete + created_by + description + переименовать без апдейта всех задач)
- CRUD только head/admin. Multi-tenancy по `org_id`.

**Auto-создание чатов:**
- При `TaskService.create()` → создаётся `TASK` чат с {creator, assignee}
- Если `project_id` передан → `ensure_project_chat_membership()` лениво создаёт `PROJECT` чат или мерджит участников в существующий
- При смене assignee/project_id — добавляются участники в новые чаты. Старый project-chat остаётся как наблюдатель
- При `deactivate` → archive task chat + архивация project chat если это была последняя активная задача

**Audit trail (`task_events`):**
- Отдельная таблица, каждый статусный переход пишется отдельным событием
- Event types: `created, took, marked_done, accepted, rejected, deadline_set, deadline_proposed, deadline_proposal_accepted|rejected, assignee_changed, project_changed, cancelled, overdue`
- В чатах задач/проектов **системные сообщения не пишутся** — всё в `task_events`, UI рендерит timeline отдельно

**Ссылки `!<task-uuid>` / `!!<project-uuid>`:**
- Параллельная подсистема к `@`/`@@` (не переиспользует MentionService чтобы избежать type confusion в `Mention.user_id`)
- `ReferenceService.resolve_batch()` делает per-viewer visibility-check: task — creator/assignee/same-org-admin; project — same org + is_active
- Недоступные — `accessible=false, title=null`, cross-org не утекает
- UI вставляет UUID через autocomplete (юзер печатает `!`/`!!`, выбирает из списка)

API: `/projects` CRUD + `/projects/{id}/chat`, `/tasks/{id}/chat`, `/tasks/{id}/events`, `?project_id` фильтр в `/tasks/my`, `/chats/my?type=direct|task|project`.

Frontend: `/chat/task/[id]`, `/chat/project/[id]` страницы с pinned card + polling chat, секции "Задачи"/"Проекты" в sidebar, `ChatInput` autocomplete для `!`/`!!`, `MessageBubble` рендерит pills.

Тесты: 68 новых, всего 97 passed engine.

**Follow-up:** WebSocket-интеграция вместо polling на task/project chat pages, колонка "Проект" в tasks-table.

---

## Сводная таблица готовности

| Фича | Engine | WebClient Backend | WebClient Frontend | Блокеры |
|-------|--------|-------------------|-------------------|---------|
| Задачи | 100% | 100% | 100% | AI-генерация текста отчётов (plain text) |
| Колокольчик | 100% | 100% | 100% | Нет WebSocket push, нет навигации по клику |
| Файлы + RAG | 100% | 100% | 100% | S3 не реализован (local storage) |
| Упоминания/Коррекция | 100% | 100% | 100% | RAG для правил не подключён |
| Отделы + видимость (item 8) | 100% | 100% | 100% | — |
| Ownership + deadline negotiation (item 9) | 100% | 100% | 100% | — |
| PM-агент + Kafka (item 10) | 100% | 100% | 100% | manual verification в браузере |
| Проекты + task/project chats (item 11) | 100% | 100% | 100% | WS вместо polling, колонка "Проект" в таблице |

### Реализовано вне отчёта

- **Zero Trust / устройства** -- миграция 011, CryptoService (ECDSA P-256), DeviceStorage
- **Organization timezone** -- миграция 014, per-org timezone в SchedulerService
- **RAG pipeline** -- миграции 012-013, RAGService, IngestQueue, rag_search tool, 7 SQL-функций
- **Переход LLM на LiteLLM + vLLM** — см. `docs/llm.md`, инфра C.Zver (`192.168.1.80:4000`), модели `gemma-4-31B-it` + `Qwen3-Embedding-0.6B` (1024 dim)
