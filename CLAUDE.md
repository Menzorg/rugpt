# RuGPT Engine

Корпоративный AI-ассистент с агентной системой, календарём, уведомлениями и multi-tenancy.

## Стек

- Python 3.10+, FastAPI, Uvicorn
- PostgreSQL (asyncpg, пул 2-10 соединений)
- LLM: Ollama / vLLM (модель по умолчанию: qwen2.5:7b)
- LangChain + LangGraph (агентный фреймворк)
- JWT (PyJWT), bcrypt для паролей
- croniter (рекуррентные события)
- aiosmtplib (email-уведомления — код есть, в проде НЕ настроено, на будущее)

## Структура проекта

```
src/engine/
├── app.py              # FastAPI приложение, entry point
├── config.py           # Конфигурация из .env (Kafka, Redis, DB, LLM)
├── run.py              # CLI runner (uvicorn)
│
├── models/             # Dataclass-модели
│   ├── organization.py, user.py, role.py, chat.py, message.py
│   ├── calendar_event.py, notification.py
│   ├── task.py, task_poll.py, task_report.py
│   ├── project.py              # item 11
│   ├── task_event.py           # item 11 audit trail
│   ├── agent_run.py            # item 10 async idempotency
│   ├── department.py, user_file.py, correction_rule.py, rag.py
│   └── ...
│
├── storage/            # PostgreSQL CRUD (asyncpg)
│   ├── base.py
│   ├── org_storage.py, user_storage.py, role_storage.py
│   ├── chat_storage.py, message_storage.py
│   ├── calendar_storage.py, notification_*_storage.py
│   ├── task_storage.py (+ list_archived, count_active_in_project, get_many_by_ids)
│   ├── task_poll_storage.py, task_report_storage.py
│   ├── project_storage.py        # item 11
│   ├── task_event_storage.py     # item 11
│   ├── agent_run_storage.py      # item 10 (mark_running CAS)
│   └── ...
│
├── services/           # Бизнес-логика
│   ├── engine_service.py             # Композитный singleton
│   ├── org_service.py, users_service.py
│   ├── roles_service.py              # Только чтение
│   ├── chat_service.py               # + create_task_chat / ensure_project_chat (item 11)
│   ├── mention_service.py
│   ├── reference_service.py          # item 11: !/!! резолв
│   ├── ai_service.py                 # + async mode (Kafka publish to agent.requests)
│   ├── prompt_cache.py
│   ├── calendar_service.py, scheduler_service.py
│   ├── notification_service.py       # Telegram/Email (код есть, в проде не настроено)
│   ├── task_notification_service.py  # item 10: PM-агент уведомления
│   ├── task_service.py               # + hook'и PM notify + events
│   ├── task_event_service.py         # item 11: audit trail
│   ├── project_service.py            # item 11: CRUD проектов
│   ├── task_poll_service.py, task_report_service.py
│   └── ...
│
├── kafka/              # Kafka event bus (item 10)
│   ├── producer.py         # KafkaProducerService (aiokafka)
│   ├── consumer.py         # KafkaConsumerLoop (background asyncio)
│   └── agent_handler.py    # AgentRequestHandler (CAS + generate + publish)
│
├── routes/             # FastAPI роутеры
│   ├── health.py, auth.py
│   ├── organizations.py, users.py, roles.py
│   ├── chats.py            # + references в /messages ответе
│   ├── calendar.py, notifications.py, in_app_notifications.py
│   ├── tasks.py            # + /{id}/chat, /{id}/events, /archive (items 9/11)
│   ├── task_polls.py, task_reports.py
│   ├── projects.py         # item 11
│   ├── files.py, rag.py, departments.py
│
├── agents/             # LangChain/LangGraph
│   ├── executor.py, result.py
│   ├── graphs/ (simple, chain, multi_agent, rule_generator)
│   └── tools/ (registry, calendar_tool, task_tool, rag_tool, web_tool, role_call_tool)
│
├── notifications/      # Каналы доставки: telegram_sender, email_sender (опционально, в проде не активны)
│
├── prompts/            # Системные промпты
│   ├── lawyer.md, accountant.md, hr.md, chu.md
│   ├── admin_assistant.md, humorist.md
│   └── pm.md                 # item 10
│
├── llm/providers/      # LLM (legacy): base.py, ollama.py
│
├── migrations/         # SQL миграции 001-018
│   ├── 001_initial ... 014_org_timezone
│   ├── 015_departments              # item 8
│   ├── 016_task_ownership           # item 9
│   ├── 017_projects_and_task_chats  # item 11
│   └── 018_pm_role_and_agent_runs   # item 10
│
└── utils/

docker-compose.kafka.yml     # Apache Kafka 3.7 KRaft single-node (item 10)
scripts/
├── kafka_up.sh              # docker compose up + init topics
├── kafka_down.sh
└── kafka_init.sh            # agent.requests + chat.events topics
```

## Ключевые команды

```bash
# Установка
./setup.sh

# Миграции БД
./migrate.sh

# Запуск
source venv/bin/activate
uvicorn src.engine.app:app --host 127.0.0.1 --port 8100 --reload

# Тестовые данные
./test-init.sh    # создать
./test-del.sh     # удалить
```

## API

Base URL: `http://127.0.0.1:8100/api/v1`

Основные роуты: `/auth/*`, `/users/*`, `/roles/*`, `/chats/*`, `/organizations/*`, `/calendar/*`, `/notifications/*`, `/in-app-notifications/*`, `/tasks/*`, `/task-polls/*`, `/task-reports/*`, `/files/*`, `/health`

## Сеть

- Engine слушает на `127.0.0.1:8100` (только localhost)
- Перед Engine стоит Nginx, который принимает запросы от WebClient по VPN на роут `/api/v1/web/*`, проверяет prefix `/web` и проксирует на FastAPI, убирая `/web`
- VPN: Engine = 10.0.0.2, WebClient = 10.0.0.1
- Подробнее: `docs/networking.md`

## Архитектура

- Слои: Routes → Services → Storage
- EngineService — singleton, композит всех storage и сервисов
- Multi-tenancy через org_id на всех таблицах
- Система упоминаний: `@username` (уведомление), `@@username` (вызов AI-роли)
- AI-ответы требуют валидации пользователем (ai_validated=false по умолчанию)

### Агентная система

- Роли — предсозданы через миграции/seed, CRUD через API убран
- Промпты в файлах (`src/engine/prompts/*.md`), не в БД — git-версионирование
- PromptCache — in-memory кеш, сброс через admin API без рестарта
- AgentExecutor — маршрутизация по `role.agent_type` (simple/supervisor)
- Supervisor graph — multiagent-оркестрация через роли-сабагенты и handoff-инструменты
- ToolRegistry — реестр инструментов (calendar, task, rag, web, role_call, list_documents, expand_chunk, table_rows, user, analyze_image, list_roles)
- LangChain (ChatOpenAI → LiteLLM proxy) + LangGraph (StateGraph) для оркестрации

### Календарь + Планировщик

- CalendarService: события (one_time / recurring с cron)
- SchedulerService: фоновый asyncio task, polling каждые 30 сек
- При срабатывании: проактивный запуск агента → уведомление

### Уведомления

- **NotificationService**: оркестрация внешних каналов (Telegram/Email). Код есть, в текущем проде НЕ активирован — на будущее. Сейчас всё уведомление = колокольчик.
- **InAppNotificationService**: колокольчик, типы `new_task | poll | report | mention | task_status_change | system`
- **TaskNotificationService** (item 10): PM-агент автоматически пишет в личные direct-чаты юзеров при изменениях задач. Сообщения публикуются в Kafka `chat.events` → NestJS broadcast через WS

### Проекты и чаты задач (item 11)

- `projects` таблица, группировка задач, head/admin может создавать
- Автосоздание чатов при создании задачи: TASK chat (creator+assignee), PROJECT chat (все участники всех задач проекта)
- `task_events` — audit trail статусных переходов (отдельно от сообщений)
- `ReferenceService` — парсит `!<task-uuid>` / `!!<project-uuid>` в сообщениях, резолвит per-viewer, рендерится как clickable pills на фронте

### Kafka event bus (item 10)

- Apache Kafka 3.7 KRaft mode в Docker, `docker-compose.kafka.yml`
- Два топика: `agent.requests` (внутренняя очередь инференса), `chat.events` (доставка сообщений в WS через NestJS)
- Engine: `KafkaProducerService` + background `KafkaConsumerLoop(agent.requests)` в том же uvicorn-процессе через `asyncio.create_task`
- NestJS: `KafkaConsumerService` подписан на `chat.events`, broadcast через `SocketGateway.broadcastToChat`
- Per-instance consumer group в NestJS для broadcast pattern
- `agent_runs` таблица для идемпотентности при Kafka redelivery (атомарный CAS `pending → running`)
- **Async агентные вызовы**: `@@mention` и `try_auto_respond` публикуют в `agent.requests` вместо блокировки HTTP handler'а. Agent response прилетает через `chat.events` позже
- `Config.KAFKA_ENABLED=false` → весь Kafka код становится no-op, система работает в sync fallback режиме

Скрипты: `scripts/kafka_up.sh`, `scripts/kafka_down.sh`, `scripts/kafka_init.sh`.

## БД

PostgreSQL, база `rugpt`. Таблицы:
- **Core**: organizations, users, roles, chats, messages
- **Calendar/Notifications**: calendar_events, notification_channels, notification_log, in_app_notifications
- **Tasks** (item 9): tasks (+ created_by_user_id, proposed_deadline/_by, awaiting_review_at), task_polls, task_reports
- **Projects** (item 11): projects, task_events, chats.task_id/project_id, tasks.project_id
- **Async agents** (item 10): agent_runs (idempotency для Kafka)
- **RAG**: user_files, chunks, tables_rows_chunks
- **Security**: correction_rules, user_devices, departments, department_visibility

Миграции в `src/engine/migrations/` (001-018). Soft delete через is_active/is_deleted.

## LLM

- AgentExecutor: LangChain ChatOllama → ReAct agent (с tools) или прямой вызов
- OllamaProvider: legacy, для health checks и model listing
- Каждая Role имеет `agent_type`, `tools`, `prompt_file`
- Таймаут: 120-300 секунд (CPU-инференс)
- **Async режим** (item 10): агентные вызовы идут через Kafka `agent.requests`, результат через `chat.events`. HTTP POST /chats/{id}/messages возвращает `agent_pending=true` мгновенно, не ждёт инференса

## Документация

`docs/`: architecture.md, api.md, storage.md, services.md, llm.md, models.md, networking.md, lang-ecosystem.md

## Логи

`logs/engine/<YYYY-MM-DD>/<component>.jsonl` — JSON Lines, один файл на компонент, автоматическая ротация по дням. Компоненты: `routes`, `services`, `storage`, `agents`, `kafka`, `notifications`, `tasks`, `app`, `request`.

Использование:
```python
from src.engine.unified_logger import get_logger
logger = get_logger("services")
logger.info("user logged in", user_id=42)   # **kwargs → metadata в JSON
```

- `correlation_id` — ставится `CorrelationIDMiddleware` на каждый HTTP-запрос (header `X-Correlation-ID`, query `correlation_id`, или generated UUID), читается через `get_correlation_id()`
- `user_id` — ставится в `routes/auth.py:get_current_user` после JWT-валидации, читается через `get_user_id()`
- `SafeJSONEncoder` — сериализует UUID/datetime/Decimal/set, fallback на `str(obj)`
- Ротация — thread-safe через `threading.Lock` + double-checked locking

Архитектура:
- `unified_logger/base.py`: `BaseUnifiedLogger` (ABC), `StructuredFormatter`, `SafeJSONEncoder`, `_check_and_rotate_handlers`
- `unified_logger/eng.py`: `EngUnifiedLogger` — путь `logs/engine/<date>/`, префикс `rugpt.<component>`
- `unified_logger/__init__.py`: фабрика `get_logger(component)` с кэшем экземпляров
- `logging_context.py`: `correlation_id_var` / `user_id_var` (ContextVar) + middleware (`CorrelationIDMiddleware`, `RequestLoggingMiddleware`)

Сторонние логи (uvicorn, asyncpg, httpx, aiokafka) — в stdout через `logging.basicConfig` в `app.py`/`run.py`, в файлы не пишутся.

## Связанные проекты

- WebClient: `/root/webclient_rugpt` — тонкий proxy-клиент (NestJS + Next.js)
