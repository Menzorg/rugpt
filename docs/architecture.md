# RuGPT Engine Architecture

> Актуальный источник истины по физической инфраструктуре — `architecture-full-2026-04-22.md` + визуальная диаграмма `architecture-2026-04-22.drawio` (multi-page). Этот файл — логический обзор Engine.

## Обзор

**RuGPT** — корпоративный AI-ассистент с агентной системой, календарём, уведомлениями и multi-tenancy.

**Engine** отвечает за:
- Управление организациями, пользователями, ролями (агентами)
- Хранение чатов и сообщений
- Систему упоминаний (@ и @@)
- Агентную систему (LangChain/LangGraph): simple, chain, multi_agent
- Задачи сотрудников: утренние опросы, вечерние отчёты, контроль дедлайнов
- Календарь + фоновый планировщик (cron, рекуррентные события, task jobs)
- RAG: загрузка документов, индексация, гибридный поиск (pgvector + TSV)
- Файлы: загрузка, хранение, дедупликация, RAG-индексация
- Уведомления: Telegram-бот, Email, in-app (колокольчик)
- Коррекция AI: правила на основе обратной связи пользователей
- Zero Trust: верификация устройств (ECDSA P-256)
- Проактивный запуск агентов при срабатывании событий
- Аутентификацию (JWT)
- **Kafka event bus** (`agent.requests`, `chat.events`) — асинхронный
  инференс агентов + проактивная доставка сообщений в WebClient
- **PM-агент** — автоматические уведомления пользователей о событиях в
  задачах через личный direct-чат (item 10)
- **Проекты и чаты задач** — группировка задач, автоматические чаты,
  audit trail, ссылки `!<task>` / `!!<project>` (item 11)

**WebClient** отвечает за:
- Real-time (WebSocket)
- UI
- Redis (сессии, Socket.IO adapter, rate-limit, message outbox)

## Инфраструктура

RuGPT размещён на 7 узлах. Подробности по железу, CPU pinning, RAM/дискам, Kafka-конфигу, Zero-Trust и 25 security-пунктам — в `architecture-full-2026-04-22.md` и `docs/networking.md`.

### Узлы

| # | Узел | IP | Роль |
|---|---|---|---|
| A | Prod-VPS (`rugpt.pro`) | public, WG **10.0.0.1** | Публичный хостинг: nginx (TLS Let's Encrypt) + Next.js + NestJS + Redis. Тонкий proxy к Engine |
| B | RAG Proxmox | public `217.113.118.218`, WG **10.0.0.2** | 64c / 512 GB. LXC: rugpt-container (.81), gitlab-container (.118). KVM: postgres-vm (.82), docker-vm (.84), gitlab-runner-vm (.119), dev-vm (TBD) |
| C | Zver — GPU-хост | LAN `192.168.1.80` | LiteLLM `:4000` + vLLM + CUDA. Модели `gemma-4-31B-it`, `Qwen3-Embedding-0.6B`. ~200 GB VRAM |
| D | Omada ER605 | public `217.113.118.218` | Dual-WAN (ISP #1 + ISP #2), WireGuard-сервер 10.0.0.0/24, ACL VPN → только 10.0.0.2, NAT forwards на SSH GitLab/runner |
| E | NAS Synology | LAN `192.168.1.38` | DSM 7.3.2, Btrfs RAID 5, WriteOnce. Бэкапы: Proxmox vzdump + pg_dump |
| F | Dev-VPS | public (отдельный IP) | Копия кода (без `.git`, `.env`, `venv`). Minimize blast-radius |
| G | Компьютер Александра | — | Рабочая станция второго разработчика (push в GitHub/GitLab) |

### Размещение подсистем по узлам

| Подсистема | Узел | Важное |
|---|---|---|
| FastAPI + LangChain/LangGraph + Kafka consumer + Scheduler + IngestQueue | **rugpt-container** (B.I.1) | Всё в одном uvicorn-процессе. 3 pinned cores (50-52), 10 GB RAM |
| PostgreSQL 16 + pgvector + pgcrypto | **postgres-vm** (B.II.1) | Отдельная KVM-VM, **не** внутри rugpt-container. 368 GB RAM, 35 cores, 200+2000 GB диск. pgvector — одна общая схема, изоляция через `org_id AND (is_public OR user_id = viewer)` |
| Apache Kafka 3.7 (KRaft) | **docker-vm** (B.II.2) | Multi-tenant с Tika/n8n/сторонними. Топики: `agent.requests` (3p, 24h, acks=all, idempotent, key=request_id), `chat.events` (3p, 1h, key=chat_id). Auth **отсутствует** (trust by VPN) |
| Apache Tika | docker-vm (B.II.2) | `:9998`, без auth. CVE-prone (Java) |
| LiteLLM `:4000` + vLLM + CUDA | **Zver** (C) | OpenAI-compatible API. Engine ходит на `192.168.1.80:4000`. Auth = `Bearer sk-dummy` (любой непустой) |
| Webclient (Next.js + NestJS + Redis) | **Prod-VPS** (A) | Доступ к Engine только через WireGuard на `10.0.0.2`. Rate-limit в Redis (100/min default, 10/min strict, WS 100/min/event/user) |
| GitLab CE Omnibus + Runner | B.I.2 + B.II.3 | Миграция из GitHub после запуска. SSH через NAT `:28351` и `:24952` |
| Dev-vm all-in-one | B.II.4 | Отдельная KVM-VM для CI preview-среды, клон prod-данных после анонимизации |
| Бэкапы (vzdump + pg_dump) | **NAS** (E) | NFS к Proxmox, Btrfs WriteOnce. SPOF |

### Сеть и криптография

- **Наружу** (internet → A): TLS 1.2/1.3 (nginx + Let's Encrypt)
- **VPN** (A ↔ B): WireGuard ChaCha20-Poly1305, подсеть `10.0.0.0/24`
- **LAN** (B внутри, B ↔ C): **plaintext** (TLS отсутствует) — trust by VPN/LAN isolation
- **App-layer**: JWT HMAC-SHA256 + ECDSA P-256 device signature (non-extractable в IndexedDB браузера). Validation на engine `POST /auth/verify-signature`, nonce-cache ±5 min. См. `docs/networking.md` + архитектурный отчёт
- **Публичные endpoints без JWT/signature** (`@SkipSignature`): `/auth/login`, `/auth/register`, `/auth/verify-signature`, `/config`, `/health*`, `/notifications/telegram/webhook`, `/files/upload` (legacy), `/files/:id/download` (legacy)
- **Engine без rate-limit** (открыт в VPN) — rate-limit только в webclient

### Хранилище состояния

- **PostgreSQL** (postgres-vm) — persistent, единственный источник истины
- **Kafka** (docker-vm) — транзиентная очередь (24h для `agent.requests`, 1h для `chat.events`)
- **Redis** — **только на webclient** (Prod-VPS); в engine `config.py` объявлены переменные, но не используются
- **Файлы** — `/root/rugpt/uploads/{org_id}/{user_id}/{file_id}.{ext}` на rugpt-container (80 GB диск). Метаданные в PG (`user_files`). SHA-256 дедуп per-user
- **Бэкапы** — `pg_dump` + Proxmox `vzdump` → NAS (Synology, Btrfs RAID 5, WriteOnce)

## Архитектура

```
+-----------------------------------------+
|       WebClient (10.0.0.1)              |
|  (NestJS + Next.js)                     |
|                                         |
|  RuGPTEngineAdapter:                    |
|  +-- POST /api/v1/web/chats/...        |
|  +-- GET  /api/v1/web/calendar/...     |
|  +-- POST /api/v1/web/notifications/.. |
|                                         |
+-------------------+---------------------+
                    | HTTP через VPN
                    v
+-------------------------------------------+
|  Nginx (10.0.0.2:80)                     |
|  +-- /api/v1/web/* -> :8100/api/v1/*     |
|     всё остальное -> 403                  |
+-------------------+-----------------------+
                    |
                    v
+-------------------------------------------+
|           Engine (FastAPI)                |
|                                           |
|  PostgreSQL (23 таблицы):                 |
|  +-- organizations (+ timezone,           |
|  |    org_context)                         |
|  +-- users (+ is_system, department_id,   |
|  |    is_head)                             |
|  +-- departments                          |
|  +-- department_visibility                |
|  +-- roles (agent_type, tools,            |
|  |         prompt_file, rag_collection)   |
|  +-- chats (+ task_id, project_id)        |
|  +-- messages (+ ai_is_valid)             |
|  +-- calendar_events                      |
|  +-- notification_channels                |
|  +-- notification_log                     |
|  +-- tasks (+ project_id, created_by,     |
|  |    awaiting_review_at, proposed_*)     |
|  +-- task_polls                           |
|  +-- task_reports                         |
|  +-- task_events (audit trail, item 11)   |
|  +-- projects (item 11)                   |
|  +-- agent_runs (async idempotency,       |
|  |    item 10)                              |
|  +-- in_app_notifications                 |
|  +-- user_files (+ RAG: summary,          |
|  |    summary_embedding, is_table, tsv,   |
|  |    folder_id)                          |
|  +-- user_file_folders (personal folders, |
|  |    adjacency list, soft-delete)         |
|  +-- chunks (pgvector)                    |
|  +-- tables_rows_chunks (pgvector)        |
|  +-- correction_rules                     |
|  +-- user_devices                         |
|                                           |
|  Kafka event bus (item 10):               |
|  +-- agent.requests (24h, 3 partitions)   |
|  |    producer: AIService.enqueue         |
|  |    consumer: AgentRequestHandler       |
|  +-- chat.events (1h, 3 partitions)       |
|       producer: TaskNotifService +        |
|                 AgentRequestHandler       |
|       consumer: NestJS KafkaConsumerSvc   |
|                 -> Socket.IO broadcast    |
|                                           |
|  Agents (LangChain/LangGraph):            |
|  +-- AgentExecutor (simple/chain/multi)   |
|  +-- ToolRegistry:                        |
|  |    calendar_create/query, task_*,      |
|  |    user_search, list_documents,        |
|  |    rag_search, web_search, role_call   |
|  +-- PromptCache (файлы, git-версии)      |
|  +-- RuleGenerator (коррекция AI)         |
|                                           |
|  RAG:                                     |
|  +-- RAGService (Tika + pgvector)         |
|  +-- IngestQueue (ThreadPoolExecutor)     |
|  +-- rag_search tool (hybrid search)      |
|                                           |
|  Scheduler:                               |
|  +-- Calendar events polling              |
|  +-- Morning polls (tasks)                |
|  +-- Evening reports (tasks)              |
|  +-- Overdue checks                       |
|  +-- Per-org timezone support             |
|                                           |
|  Notifications:                           |
|  +-- TelegramSender (Bot API)             |
|  +-- EmailSender (SMTP)                   |
|  +-- InAppNotificationService             |
|  +-- TaskNotificationService (PM, item 10)|
|       -> Kafka chat.events                |
|                                           |
|  Files:                                   |
|  +-- FileService                          |
|  +-- FolderService (personal folders,     |
|  |    cascade delete, cycle/depth guard)  |
|  +-- LocalStorageAdapter (filesystem)     |
|                                           |
|  Departments:                             |
|  +-- DepartmentService (visibility)       |
|  +-- Flat dept list, symmetric rules      |
|                                           |
|  Security:                                |
|  +-- CryptoService (ECDSA P-256)          |
|  +-- Device verification                  |
|                                           |
|  LLM (LiteLLM proxy → vLLM):              |
|  +-- ChatOpenAI (LangChain + langchain-   |
|  |   openai) → LiteLLM :4000              |
|  +-- Модели (через LiteLLM):              |
|  |   hosted_vllm/google/gemma-4-31B-it    |
|  |   hosted_vllm/Qwen/Qwen3-Embedding-    |
|  |   0.6B (1024-dim для pgvector)         |
|                                           |
+-------------------------------------------+
```

## Структура проекта

```
/root/rugpt/
+-- docs/                              # Документация
+-- src/
|   +-- engine/
|       +-- app.py                     # FastAPI приложение
|       +-- config.py                  # Конфигурация из .env
|       +-- run.py                     # Entry point (uvicorn)
|       +-- constants.py              # Константы (типы файлов, лимиты)
|       |
|       +-- models/                    # Dataclass-модели
|       |   +-- organization.py        # Организация (+ timezone)
|       |   +-- user.py                # Пользователь (+ is_system)
|       |   +-- role.py                # AI-агент (agent_type, tools, prompt_file, rag_collection)
|       |   +-- chat.py                # Чат (DIRECT / TASK / PROJECT)
|       |   +-- message.py            # Сообщение, Mention, SenderType, MentionType
|       |   +-- calendar_event.py      # Календарное событие
|       |   +-- notification.py        # NotificationChannel, NotificationLog
|       |   +-- task.py                # Задача сотрудника (+ project_id)
|       |   +-- task_poll.py           # Утренний опрос
|       |   +-- task_report.py         # Вечерний отчёт
|       |   +-- task_event.py          # Audit trail задач (item 11)
|       |   +-- project.py             # Проект (item 11)
|       |   +-- agent_run.py           # Async agent execution (item 10)
|       |   +-- in_app_notification.py # In-app уведомление
|       |   +-- user_file.py           # Файл пользователя
|       |   +-- correction_rule.py     # Правило коррекции AI
|       |   +-- rag.py                 # RelatedDoc, ChunkSearchResult
|       |   +-- department.py          # Department, DepartmentVisibility
|       |
|       +-- storage/                   # PostgreSQL CRUD (asyncpg)
|       |   +-- base.py                # Базовый класс с пулом
|       |   +-- org_storage.py
|       |   +-- user_storage.py
|       |   +-- role_storage.py
|       |   +-- chat_storage.py
|       |   +-- message_storage.py
|       |   +-- calendar_storage.py
|       |   +-- notification_channel_storage.py
|       |   +-- notification_log_storage.py
|       |   +-- task_storage.py
|       |   +-- task_poll_storage.py
|       |   +-- task_report_storage.py
|       |   +-- in_app_notification_storage.py
|       |   +-- user_file_storage.py
|       |   +-- correction_rule_storage.py
|       |   +-- device_storage.py
|       |   +-- rag_store.py           # RAG: chunks, table rows, hybrid search
|       |   +-- department_storage.py  # Departments + visibility rules
|       |   +-- project_storage.py     # Projects (item 11)
|       |   +-- task_event_storage.py  # task_events audit trail (item 11)
|       |   +-- agent_run_storage.py   # agent_runs idempotency CAS (item 10)
|       |   +-- storage_adapter.py     # StorageAdapter ABC + LocalStorageAdapter
|       |
|       +-- services/                  # Бизнес-логика
|       |   +-- engine_service.py      # Композитный singleton
|       |   +-- org_service.py
|       |   +-- users_service.py
|       |   +-- roles_service.py       # Только чтение + кеш промптов
|       |   +-- chat_service.py        # + task/project chat auto-create (item 11)
|       |   +-- mention_service.py
|       |   +-- reference_service.py   # Резолв !<task>/!!<project> в сообщениях (item 11)
|       |   +-- ai_service.py          # AgentExecutor + async mode (Kafka publish)
|       |   +-- prompt_cache.py        # In-memory кеш промптов из файлов
|       |   +-- calendar_service.py    # Календарные события + croniter
|       |   +-- scheduler_service.py   # Фоновый polling + task jobs + per-org timezone
|       |   +-- notification_service.py # Оркестрация уведомлений по каналам
|       |   +-- task_notification_service.py # PM-агент (item 10) -> Kafka chat.events
|       |   +-- task_service.py        # + hook-и в PM notification service
|       |   +-- task_event_service.py  # Audit trail задач (item 11)
|       |   +-- project_service.py     # CRUD проектов (item 11)
|       |   +-- task_poll_service.py   # Утренние опросы
|       |   +-- task_report_service.py # Вечерние отчёты
|       |   +-- in_app_notification_service.py # In-app уведомления
|       |   +-- file_service.py        # Загрузка / скачивание файлов
|       |   +-- rag_service.py         # Индексация: Tika + chunking + embedding
|       |   +-- correction_rule_service.py # Правила коррекции AI
|       |   +-- department_service.py   # Отделы + видимость (get_visible_user_ids)
|       |   +-- crypto_service.py      # ECDSA P-256 верификация устройств
|       |
|       +-- kafka/                     # Kafka event bus (item 10)
|       |   +-- producer.py            # KafkaProducerService (aiokafka)
|       |   +-- consumer.py            # KafkaConsumerLoop (background asyncio)
|       |   +-- agent_handler.py       # AgentRequestHandler (CAS + generate + publish)
|       |
|       +-- routes/                    # API endpoints
|       |   +-- health.py              # Health checks
|       |   +-- auth.py                # Аутентификация (login, register, refresh, verify-signature)
|       |   +-- organizations.py       # /api/v1/organizations
|       |   +-- users.py               # /api/v1/users
|       |   +-- roles.py               # /api/v1/roles (GET + admin cache)
|       |   +-- chats.py               # /api/v1/chats
|       |   +-- calendar.py            # /api/v1/calendar
|       |   +-- notifications.py       # /api/v1/notifications
|       |   +-- in_app_notifications.py # /api/v1/in-app-notifications
|       |   +-- tasks.py               # /api/v1/tasks (+ /{id}/chat, /{id}/events, /archive)
|       |   +-- task_polls.py          # /api/v1/task-polls
|       |   +-- task_reports.py        # /api/v1/task-reports
|       |   +-- projects.py            # /api/v1/projects (CRUD + /{id}/chat) item 11
|       |   +-- files.py               # /api/v1/files
|       |   +-- rag.py                 # /api/v1/rag
|       |   +-- departments.py        # /api/v1/departments (admin only)
|       |
|       +-- agents/                    # Агентная система
|       |   +-- executor.py            # AgentExecutor -- маршрутизация по agent_type
|       |   +-- result.py              # AgentResult, ToolCall dataclasses
|       |   +-- graphs/
|       |   |   +-- simple.py          # prompt -> LLM (или ReAct с tools)
|       |   |   +-- chain.py           # Последовательные шаги
|       |   |   +-- multi_agent.py     # LangGraph StateGraph
|       |   |   +-- rule_generator.py  # Генерация правил коррекции из обратной связи
|       |   +-- tools/
|       |       +-- registry.py        # ToolRegistry
|       |       +-- calendar_tool.py   # create/query события (factory)
|       |       +-- task_tool.py       # create/query/update задачи (factory)
|       |       +-- rag_tool.py        # Гибридный поиск по документам (pgvector + TSV)
|       |       +-- web_tool.py        # Веб-поиск (stub)
|       |       +-- role_call_tool.py  # Вызов другой роли (stub)
|       |
|       +-- notifications/            # Каналы доставки
|       |   +-- base_sender.py         # Абстрактный интерфейс
|       |   +-- telegram_sender.py     # Telegram Bot API (httpx)
|       |   +-- email_sender.py        # SMTP (aiosmtplib)
|       |
|       +-- prompts/                   # Системные промпты (git-версионирование)
|       |   +-- lawyer.md
|       |   +-- accountant.md
|       |   +-- hr.md
|       |   +-- chu.md
|       |   +-- admin_assistant.md
|       |   +-- humorist.md
|       |   +-- pm.md                  # PM-агент (item 10)
|       |
|       +-- tasks/                     # Фоновые задачи
|       |   +-- ingest_queue.py        # IngestQueue (ThreadPoolExecutor, 3 воркера)
|       |
|       +-- llm/                       # LLM интеграция (legacy, для health checks)
|       |   +-- providers/
|       |       +-- base.py
|       |       +-- ollama.py
|       |
|       +-- migrations/                # SQL миграции (001-018)
|       |   +-- 001_initial.sql            # organizations, users, roles, chats, messages
|       |   +-- 002_role_evolution.sql     # agent_type, agent_config, tools, prompt_file
|       |   +-- 003_system_user.sql        # is_system, системные AI-пользователи
|       |   +-- 004_mirror_user.sql        # mirror user support
|       |   +-- 005_tasks.sql              # tasks
|       |   +-- 006_task_polls.sql         # task_polls (утренние опросы)
|       |   +-- 007_task_reports.sql       # task_reports (вечерние отчёты)
|       |   +-- 008_in_app_notifications.sql # in_app_notifications
|       |   +-- 009_user_files.sql         # user_files (метаданные файлов)
|       |   +-- 010_correction_rules.sql   # correction_rules, ai_is_valid
|       |   +-- 011_user_devices.sql       # user_devices (Zero Trust)
|       |   +-- 012_rag_schema.sql         # pgvector, chunks, tables_rows_chunks
|       |   +-- 013_rag_functions.sql      # SQL-функции гибридного поиска
|       |   +-- 014_org_timezone.sql       # timezone в organizations
|       |   +-- 015_departments.sql        # departments + visibility rules
|       |   +-- 016_task_ownership.sql     # tasks.created_by_user_id + proposed_deadline fields
|       |   +-- 017_projects_and_task_chats.sql  # projects, task_events, chats.task_id/project_id
|       |   +-- 018_pm_role_and_agent_runs.sql   # PM role + system user + agent_runs idempotency
|       |
|       +-- utils/
|
+-- docker-compose.kafka.yml            # Apache Kafka 3.7 KRaft, item 10
+-- scripts/
|   +-- kafka_up.sh                     # docker compose up + init topics
|   +-- kafka_down.sh                   # docker compose down
|   +-- kafka_init.sh                   # create agent.requests + chat.events
|
+-- requirements.txt
+-- setup.sh
+-- migrate.sh
+-- deploy.sh
+-- local_restart.sh
+-- sync.sh
+-- .env.example
+-- .env
```

## API Endpoints

### Core
| Endpoint | Описание |
|----------|----------|
| `GET /health` | Health check |
| `GET /health/ready` | Readiness probe |
| `GET /health/live` | Liveness probe |

### Auth (`/api/v1/auth`)
| Endpoint | Описание |
|----------|----------|
| `POST /login` | Логин (+ device_public_key, device_name) |
| `POST /register` | Регистрация |
| `GET /me` | Текущий пользователь |
| `POST /refresh` | Обновление JWT |
| `POST /verify-signature` | Верификация подписи устройства |
| `GET /devices` | Список устройств пользователя |

### Organizations (`/api/v1/organizations`)
| Endpoint | Описание |
|----------|----------|
| `GET /` | Список организаций |
| `POST /` | Создать (+ timezone) |
| `GET /{id}` | Получить |
| `PATCH /{id}` | Обновить |
| `DELETE /{id}` | Деактивировать |

### Users (`/api/v1/users`)
| Endpoint | Описание |
|----------|----------|
| `GET /` | Список пользователей |
| `POST /` | Создать |
| `GET /system` | Системные AI-пользователи |
| `GET /{id}` | По ID |
| `GET /username/{username}` | По username |
| `PATCH /{id}` | Обновить |
| `POST /{id}/password` | Сменить пароль |
| `POST /{id}/role` | Назначить роль |
| `DELETE /{id}` | Деактивировать |

### Roles (`/api/v1/roles`)
| Endpoint | Описание |
|----------|----------|
| `GET /` | Список ролей (предсозданы, без CRUD) |
| `GET /{id}` | По ID |
| `GET /code/{code}` | По коду |
| `GET /{id}/users` | Пользователи с ролью |
| `POST /admin/cache/prompts/clear` | Сброс всего кеша промптов |
| `POST /admin/cache/prompts/clear/{role_code}` | Сброс кеша роли |

### Chats (`/api/v1/chats`)
| Endpoint | Описание |
|----------|----------|
| `GET /my` | Чаты пользователя |
| `POST /direct` | Создать прямой чат |
| `POST /group` | Создать групповой чат |
| `GET /{chat_id}` | Получить чат |
| `DELETE /{chat_id}` | Архивировать чат |
| `POST /{chat_id}/participants/{participant_id}` | Добавить участника |
| `DELETE /{chat_id}/participants/{participant_id}` | Удалить участника |
| `GET /{chat_id}/messages` | Сообщения (cursor: before_id) |
| `POST /{chat_id}/messages` | Отправить сообщение |
| `GET /messages/{message_id}` | Получить сообщение |
| `DELETE /messages/{message_id}` | Удалить сообщение |
| `POST /messages/{message_id}/validate` | Одобрить AI-ответ (+ edited_content) |
| `POST /messages/{message_id}/reject` | Отклонить AI-ответ |
| `GET /pending-review` | Сообщения на проверке |
| `GET /unvalidated` | Непроверенные сообщения |

### Calendar (`/api/v1/calendar`)
| Endpoint | Описание |
|----------|----------|
| `GET /events` | Список событий |
| `POST /events` | Создать событие |
| `GET /events/{id}` | Получить |
| `PATCH /events/{id}` | Обновить |
| `DELETE /events/{id}` | Деактивировать |
| `GET /roles/{role_id}/events` | События роли |

### Notifications (`/api/v1/notifications`)
| Endpoint | Описание |
|----------|----------|
| `GET /channels` | Каналы уведомлений |
| `POST /channels` | Регистрация канала |
| `DELETE /channels/{type}` | Удалить канал |
| `POST /channels/{type}/verify` | Подтвердить канал |
| `POST /telegram/webhook` | Telegram webhook |
| `GET /log` | Лог уведомлений |

### In-App Notifications (`/api/v1/in-app-notifications`)
| Endpoint | Описание |
|----------|----------|
| `GET /` | Список уведомлений |
| `GET /unread-count` | Счётчик непрочитанных |
| `PATCH /{id}/read` | Отметить прочитанным |
| `POST /read-all` | Отметить все прочитанными |

### Tasks (`/api/v1/tasks`)
| Endpoint | Описание |
|----------|----------|
| `GET /` | Список задач (?status=, ?assignee_user_id=) |
| `POST /` | Создать задачу |
| `GET /{id}` | Получить |
| `PATCH /{id}` | Обновить |
| `DELETE /{id}` | Деактивировать |

### Task Polls (`/api/v1/task-polls`)
| Endpoint | Описание |
|----------|----------|
| `GET /today` | Сегодняшний опрос |
| `GET /` | История опросов |
| `GET /{id}` | Получить опрос |
| `POST /{id}/submit` | Отправить ответы |

### Task Reports (`/api/v1/task-reports`)
| Endpoint | Описание |
|----------|----------|
| `GET /` | Список отчётов |
| `GET /{id}` | Получить отчёт |

### Files (`/api/v1/files`)
| Endpoint | Описание |
|----------|----------|
| `POST /upload` | Загрузить файл |
| `GET /` | Список файлов |
| `GET /{file_id}` | Метаданные файла |
| `GET /{file_id}/download` | Скачать файл |
| `DELETE /{file_id}` | Удалить (soft) |
| `GET /{file_id}/rag-status` | Статус RAG-индексации |
| `PATCH /{file_id}/public` | Переключить публичность |

### RAG (`/api/v1/rag`)
| Endpoint | Описание |
|----------|----------|
| `POST /docs/ingest` | Индексировать документ |
| `DELETE /docs/{file_id}` | Удалить из индекса |
| `POST /docs/{file_id}/retry` | Повторить индексацию |
| `GET /docs/find` | Найти релевантные документы |
| `GET /docs/{file_id}/search/abstract` | Vector-first поиск |
| `GET /docs/{file_id}/search/concrete` | TSV-first поиск |

## Flow сообщений с @@ упоминанием

```
1. WebClient: Пользователь пишет "@@lawyer проверь договор"
2. WebClient -> Engine:
   POST /api/v1/chats/{chat_id}/messages
   {
     "content": "@@lawyer проверь договор"
   }
3. Engine: MentionService парсит @@ -> находит lawyer
4. Engine: Сохраняет сообщение в PostgreSQL
5. Engine: Находит User с role_id -> Role(lawyer)
6. Engine: AgentExecutor выбирает граф по agent_type:
   - simple без tools -> прямой вызов ChatOpenAI (LiteLLM)
   - simple с tools -> ReAct agent (LangGraph)
   - chain -> последовательные шаги
   - multi_agent -> StateGraph
7. Engine: PromptCache читает промпт из файла (или кеша)
8. Engine: RunnableConfig инжектирует org_id, user_id в tools
9. Engine: Сохраняет AI-ответ (ai_is_valid=NULL, pending review)
10. Engine -> WebClient: Response
11. WebClient: Показывает ответ AI
12. Пользователь (владелец роли) -> валидирует / редактирует / отклоняет
13. При отклонении: CorrectionRuleService генерирует правило через rule_generator graph
```

## Проактивный запуск агента (Scheduler)

```
SchedulerService: фоновый asyncio task, polling каждые 30 сек

=== Calendar Events ===
1. SELECT * FROM calendar_events WHERE is_active=true AND next_trigger_at <= NOW()
2. Для каждого события:
   a. mark_triggered() -- инкремент trigger_count, пересчёт next_trigger_at
   b. Загрузка роли -> AgentExecutor.execute() с контекстом события
   c. Агент генерирует уведомление
   d. NotificationService: отправка по каналам (Telegram -> Email -> fallback)
   e. Логирование в notification_log

=== Task Jobs (per-org timezone) ===
Для каждой организации определяется текущий час по org.timezone:

Morning (часы 8, 9, 10):
  - check_overdue() -> помечает просроченные задачи, шлёт уведомления
  - expire_stale_polls() -> закрывает неотвеченные опросы
  - create_daily_poll() -> создаёт утренний опрос для каждого сотрудника с задачами

Evening (часы 18, 19, 20):
  - generate_report() -> генерирует вечерний отчёт для руководителей
```

## Система упоминаний и ссылок

### @ упоминание (USER)
- Формат: `@username`
- Действие: Уведомляет пользователя
- Пример: `@ivan_petrov посмотри документ`

### @@ упоминание (AI_ROLE)
- Формат: `@@username`
- Действие: Вызывает AI-агента через AgentExecutor
- Пример: `@@lawyer проверь договор`
- Ответ AI требует валидации владельцем роли (ai_is_valid: NULL -> true/false)
- **Асинхронный режим (item 10)**: при включённом Kafka агентный вызов не
  блокирует HTTP-запрос. Engine публикует запрос в `agent.requests`, фоновый
  consumer внутри того же процесса обрабатывает, результат прилетает во
  фронт через `chat.events` + Socket.IO

### ! / !! ссылки на задачи и проекты (item 11)
- `!<task-uuid>` — кликабельная pill со ссылкой на чат задачи
- `!!<project-uuid>` — кликабельная pill со ссылкой на чат проекта
- Per-viewer visibility: недоступные (cross-org, archived) рендерятся как
  серый disabled без названия
- Вставляются через autocomplete в `ChatInput` (юзер не печатает UUID руками)

## Типы чатов

1. **DIRECT** — Прямые сообщения между двумя пользователями
2. **TASK** — Чат задачи, автосоздаётся с участниками {creator, assignee} (item 11)
3. **PROJECT** — Чат проекта, автосоздаётся при первой задаче в проекте (item 11)

## Kafka event bus (item 10)

Асинхронная событийная шина между Engine и WebClient, закрывает два
кейса одним механизмом:

**1. Проактивная доставка сообщений от Engine в WS клиентам.**
В обычном потоке `io.emit` делается в том же HTTP-запросе, в котором сообщение
было сохранено (NestJS handleMessage -> Engine -> ответ обратно -> emit).
Сообщения которые Engine создаёт сам (PM-уведомления, scheduler), не
попадают в этот цикл — до появления Kafka они были невидимы до F5.

**2. Асинхронный LLM-инференс.** Юзерский HTTP-запрос не держит соединение
30-300 секунд ожидая ответа агента. HTTP возвращает 202 мгновенно, агентный
run крутится в фоновом consumer'е, результат доставляется через тот же канал.

**Топики:**

| Топик | Producer | Consumer | Partitions | Retention |
|---|---|---|---|---|
| `agent.requests` | Engine HTTP handler (`AIService.enqueue`) | Engine background task (`AgentRequestHandler`) | 3 | 24h |
| `chat.events` | Engine (`TaskNotificationService`, `AgentRequestHandler`) | NestJS `KafkaConsumerService` | 3 | 1h |

**Поток для `@@mention`:**

```
юзер пишет "@@qwen3 привет"
  -> NestJS handleMessage -> Engine POST /chats/{id}/messages
  -> Engine: сохранить user message
  -> Engine: AIService.process_ai_mentions detects async mode:
       1. Create agent_runs row (status=pending)
       2. Publish to 'agent.requests'
       3. Return empty ai_responses + agent_pending=true
  -> Engine HTTP 200 -> NestJS -> io.emit user message
  -> Юзер видит своё сообщение мгновенно

[параллельно, фоновый asyncio task в том же Engine процессе]
Engine KafkaConsumerLoop('agent.requests') -> AgentRequestHandler:
  1. agent_runs CAS pending -> running (atomic SQL UPDATE RETURNING)
     Если уже running/done -> skip (Kafka redelivery идемпотентность)
  2. Load user message from DB
  3. AIService.generate_response() -- существующий synchronous pipeline
     с LangGraph ReAct loop и tools
  4. Persist AI message
  5. agent_runs mark_done(result_message_id)
  6. Publish to 'chat.events' с полным AI message payload

[NestJS side]
KafkaConsumerService consume 'chat.events':
  -> SocketGateway.broadcastToChat(chatId, msg)
  -> io.to('chat:<id>').emit('message', payload)
  -> Фронт видит AI ответ live
```

**Поток для PM-уведомлений:**

```
юзер X берёт задачу -> TaskService.take_task
  -> storage.update(task)
  -> _record_event('took')
  -> _notify('notify_take', task, user)
       -> TaskNotificationService:
          - lazy create direct chat PM<->creator
          - persist message (sender_type=ai_role, ai_is_valid=true)
          - publish to 'chat.events'
  -> NestJS KafkaConsumerService -> io.emit -> creator видит уведомление live
```

**Идемпотентность** — таблица `agent_runs(request_id PK, status, ...)` с
атомарным CAS через `UPDATE WHERE status='pending' RETURNING`. При Kafka
redelivery того же сообщения второй consumer получает False от `mark_running`
и скипает молча. Тесты покрывают concurrent case (`asyncio.gather` с 3
одновременными вызовами → exactly one wins).

**Broadcast pattern в NestJS**: каждый инстанс в своей Kafka consumer group
(`webclient-<hostname>-<pid>`) — все инстансы получают все события. Это
нужно потому что разные инстансы обслуживают разных юзеров через WS, и все
должны узнать о новом сообщении в чате, куда подключены их клиенты.

**Fallback без Kafka.** Оба сервиса (`TaskNotificationService`, `AIService`)
имеют sync-режим: когда `Config.KAFKA_ENABLED=false`, PM-сообщения сохраняются
только в БД (видны после F5), агентные вызовы работают синхронно в HTTP-
цикле как до item 10. Это нужно для тестов без живого Kafka и graceful
degradation если broker упал.

**Инфраструктура:** `docker-compose.kafka.yml` поднимает Apache Kafka 3.7 в
KRaft mode (без ZooKeeper), single-node. `scripts/kafka_up.sh` стартует
брокер и создаёт топики. На prod разворачивается на Engine-машине, NestJS
ходит к нему по VPN через persistent consumer connection.

## Порты

| Сервис | Порт | Хост | Auth |
|---|---|---|---|
| Engine FastAPI | 8100 | rugpt-container (B.I.1) `127.0.0.1` | JWT + ECDSA signature |
| Engine nginx (reverse-proxy) | 80 | rugpt-container | фильтр `/api/v1/web/*` → FastAPI, всё прочее 403 |
| PostgreSQL | 5432 | postgres-vm (B.II.1) `192.168.1.82` | scram-sha-256 |
| LiteLLM proxy | 4000 | Zver (C) `192.168.1.80` | `Bearer sk-dummy` |
| Apache Kafka | 9092 | docker-vm (B.II.2) `192.168.1.84` | **нет** (trust by VPN) |
| Apache Tika | 9998 | docker-vm (B.II.2) `192.168.1.84` | **нет** |
| WebClient nginx | 443 / 80 | Prod-VPS (A) | TLS Let's Encrypt |
| Redis | 6379 | Prod-VPS (loopback) | password (prod) |

## Сетевая архитектура

Подробнее о VPN, Nginx и маршрутизации запросов см. `docs/networking.md`.

## Запуск

```bash
cd /root/rugpt

# Установка
./setup.sh

# Миграции
./migrate.sh

# Запуск
source venv/bin/activate
uvicorn src.engine.app:app --host 127.0.0.1 --port 8100
```
