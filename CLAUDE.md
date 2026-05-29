# rugpt — корпоративный AI-ассистент (движок)

Многоагентная backend-система: чат с ИИ, RAG, задачи/календарь, ролевые агенты.

## Стек

- **Python 3.x + FastAPI** (async), ASGI через uvicorn
- **LangGraph + LangChain** — оркестрация агентов
- **LiteLLM** — OpenAI-совместимый прокси (`http://192.168.1.80:4000/v1`), вызов через `langchain_openai.ChatOpenAI`; модель по умолчанию `google/gemma-4-31B-it`, embeddings `Qwen/Qwen3-Embedding-0.6B` (1024-dim)
- **PostgreSQL + asyncpg** — пул соединений (2–10)
- **pgvector** — векторный поиск (RAG)
- **Redis** — nonce-store движка (replay-protection, fail-closed); `REDIS_URL`
- **Kafka** (опционально) — события (`chat.events`)
- **JWT (ECDSA)** — подпись запросов от вебклиента (Zero Trust)

## Структура проекта

```
src/engine/
├── app.py              # FastAPI app, регистрация роутеров, middleware
├── config.py           # конфиг (env)
├── logging_context.py  # correlation_id / user_id (contextvars), CorrelationIDMiddleware
├── middleware/
│   └── web_signature.py  # WebSignatureMiddleware (Zero Trust: ECDSA + nonce)
├── security/           # route_match.py, signature_payload.py (canonical payload, route-id)
├── actions/            # registry.py, bootstrap.py, invoice_actions.py (UI-actions для агентов)
├── tasks/              # ingest_queue.py (фоновая RAG-индексация)
├── kafka/              # producer.py / consumer.py / agent_handler.py (chat.events, agent.requests)
├── routes/             # HTTP-эндпоинты (по сущности)
│   ├── auth.py, users.py, roles.py, organizations.py, chats.py
│   ├── calendar.py, notifications.py, in_app_notifications.py
│   ├── tasks.py, task_polls.py, task_reports.py, projects.py, departments.py
│   ├── files.py, folders.py, rag.py
│   ├── actions.py, invoices.py, corrections.py, support.py
│   └── health.py
├── services/           # бизнес-логика
│   ├── engine_service.py  # композитный singleton (storage + сервисы + tool_registry)
│   ├── ai_service.py, chat_service.py, task_service.py, calendar_service.py
│   ├── roles_service.py, role_subagent_service.py, users_service.py, org_service.py
│   ├── rag_service.py, file_service.py, folder_service.py
│   ├── invoice_service.py, correction_rule_service.py, department_service.py
│   ├── support_ticket_service.py, support_notification_service.py, memory_service.py
│   ├── in_app_notification_service.py, notification_service.py (Telegram/Email, не активен)
│   ├── nonce_store.py      # Redis NonceStore (replay-protection, fail-closed)
│   ├── signature_service.py, crypto_service.py  # ECDSA-проверка подписи
│   ├── scheduler_service.py, mention_service.py, reference_service.py
│   └── task_event_service.py, project_service.py, task_poll_service.py, task_report_service.py
├── storage/            # доступ к БД (asyncpg-пул в base.py + engine_service.py), один файл на сущность:
│                       #   chat/message/message_attachment/chat_read_state/task/task_participant/
│                       #   task_event/calendar/role/role_subagent/user/user_file/user_file_folder/
│                       #   invoice/correction_rule/department/support_ticket/support_ticket_event/
│                       #   memory_snapshot/project/org/device/agent_run_storage.py, rag_store.py
├── models/             # dataclass/Pydantic-схемы (по сущности)
├── agents/             # агентная система
│   ├── executor.py     # AgentExecutor, маршрутизация по role.agent_type
│   ├── graphs/         # LangGraph-графы: simple.py, supervisor.py, rule_generator.py
│   ├── tools/          # registry.py (ToolRegistry); сами тулзы регистрируются в engine_service.py
│   └── metadata.py, middleware.py, runtime.py, result.py
├── llm/                # без исходников (только .pyc в __pycache__); LLM идёт через ChatOpenAI
└── migrations/         # SQL-миграции (001-046)
```

## Ключевые команды

```bash
# Запуск движка (dev), порт по умолчанию 8100
/root/rugpt/venv/bin/python -m uvicorn src.engine.app:app --reload --host 127.0.0.1 --port 8100

# Применить миграции
/root/rugpt/venv/bin/python -m src.engine.migrations.migrate

# Тесты
/root/rugpt/venv/bin/python -m pytest
```

## API

Базовый префикс `/api/v1` (см. `app.py`). Все роутеры под `/api/v1`, кроме `chats_router` (без префикса). Вебклиент ходит через `/api/v1/web/*` (`WEB_PREFIX`), middleware срезает `/web` и проверяет ECDSA-подпись. Группы:

- `/auth`, `/users`, `/roles`, `/organizations`, `/chats`
- `/calendar`, `/notifications`, `/in-app-notifications`
- `/tasks`, `/task-polls`, `/task-reports`, `/projects`, `/departments`
- `/files`, `/folders`, `/rag` (RAG-загрузка/поиск)
- `/actions`, `/invoices`, `/corrections`, `/support`
- `/health`

## Сеть

- Движок: `127.0.0.1:8100` (только localhost; перед ним Nginx)
- PostgreSQL: `localhost:5432`, БД `rugpt`
- Redis: `REDIS_URL` (по умолч. `redis://localhost:6379/0`) — nonce-store, обязателен (fail-closed)
- Kafka: (опционально, `KAFKA_ENABLED`) — топики `agent.requests`, `chat.events`

## Архитектура / Агентная система

- **agent_type**: `simple` / `supervisor` — тип графа на роль
- **Графы**: `simple.py`, `supervisor.py`, `rule_generator.py`; supervisor маршрутизирует подзапросы по агентам
- **ToolRegistry** (`agents/tools/registry.py`) — ровно 19 инструментов:
  `calendar_create`, `calendar_query`, `task_create`, `task_query`, `task_update`,
  `task_deadline_proposal`, `get_own_tasks`, `rag_search`, `expand_chunk`,
  `table_rows_search`, `web_search`, `role_call` (только заглушка), `list_documents`,
  `list_own_documents`, `analyze_image`, `user_search`, `show_modal`,
  `list_invoices`, `get_invoice`
- **RAG**: pgvector + чанкование, `rag_search` / `expand_chunk`
- **Роли**: ролевые агенты с системными промптами, вызов через `role_call` (стаб)

## Уведомления

Единого `TaskNotificationService` нет. По задачам `TaskService` (`services/task_service.py`) шлёт только **in-app bell**-уведомления через `InAppNotificationService` (`self.notification_service.create(...)`): `_bell_to_recipients` (всем участникам задачи) и `_bell_to_user` (одному адресату). Статусные переходы пишутся отдельно в `task_events` (audit trail), не как сообщения.

Другие механизмы уведомлений в системе (вне TaskService):

- **InAppNotificationService** — колокольчик (типы: new_task / mention / status_change / system и т.п.)
- **SupportNotificationService** — уведомления по тикетам поддержки
- **NotificationService** — внешние каналы (Telegram/Email): код есть, в проде НЕ активирован
- **Kafka** `chat.events` — доставка сообщений/агентных ответов в WS через NestJS (если `KAFKA_ENABLED`)

## БД

- Базовые таблицы: `users`, `roles`, `chats`, `messages`, `tasks`, `calendar_events`, `documents`, `chunks`, `table_rows`
- Доп. таблицы: `invoices`, `memory_snapshots`, `support_tickets`, `support_ticket_events`,
  `task_participants`, `message_attachments`, `chat_read_state`, `user_file_folders`,
  `role_subagents`, `departments`, `department_visibility`, `corrections`
- Миграции: `migrations/` (001-046)
- pgvector для эмбеддингов (1024-dim, `Qwen/Qwen3-Embedding-0.6B`)

## LLM

- **Провайдер**: LiteLLM (OpenAI-совместимый прокси), `LLM_BASE_URL=http://192.168.1.80:4000/v1`
- **Интеграция**: `langchain_openai.ChatOpenAI` (НЕ Ollama/ChatOllama — `llm/` без исходников, только .pyc)
- **Модель**: `google/gemma-4-31B-it` (`DEFAULT_MODEL`)
- **Embeddings**: `Qwen/Qwen3-Embedding-0.6B` (`EMBEDDING_MODEL`), размерность 1024 (`RAG_VECTOR_DIM`)
- Контекст: история чата + RAG-результаты

## Логи

- Формат: структурированные, с `correlation_id`
- `correlation_id`: сначала query-параметр `correlation_id`, затем заголовок `X-Correlation-Id` (см. `logging_context.get_correlation_id`)
- `user_id`: проставляется в `WebSignatureMiddleware` из ECDSA-подписи в `request.state.zt_user_id` (НЕ через `routes/auth.py`)
- Оба значения пробрасываются через contextvars (`correlation_id_var`, `user_id_var`)
- Логи пишутся в `/root/rugpt/logs/`

## Связанные проекты

- Вебклиент: `/root/webclient_rugpt/` (NestJS), шлёт ECDSA-подписанные запросы
- Документация движка: `/root/rugpt/docs/`, вебклиента: `/root/webclient_rugpt/doc/`
