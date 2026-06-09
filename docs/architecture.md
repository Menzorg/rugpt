# RuGPT Engine Architecture

> Актуальный источник истины по физической инфраструктуре — `architecture-full-2026-04-22.md` + визуальная диаграмма `architecture-2026-04-22.drawio` (multi-page). Этот файл — логический обзор Engine. Детали API — в `docs/api.md`, модели — `docs/models.md`, storage — `docs/storage.md`, сервисы — `docs/services.md`, LLM — `docs/llm.md`.

## Обзор

**RuGPT** — корпоративный AI-ассистент с агентной системой, календарём, уведомлениями и multi-tenancy.

**Engine** отвечает за:
- Управление организациями, пользователями, ролями (агентами), отделами и видимостью
- Хранение чатов и сообщений (+ вложения, read-state)
- Систему упоминаний (@ и @@)
- Агентную систему (LangChain/LangGraph): `simple` и `supervisor` (multi-agent)
- Задачи сотрудников: участники, утренние опросы, вечерние отчёты, контроль дедлайнов, приоритеты
- Календарь + фоновый планировщик (cron, рекуррентные события, task jobs)
- RAG: загрузка документов, индексация, гибридный поиск (pgvector + TSV)
- Файлы и личные папки: загрузка, хранение, дедупликация, RAG-индексация
- Уведомления: Telegram-бот, Email (код есть, в проде не настроено), in-app (колокольчик)
- Коррекция AI: правила на основе обратной связи + долговременная память (memory_snapshots)
- Тех-поддержку: тикеты, события тикетов, AI-первая линия (support_ai)
- Счета (invoices): извлечение из документов, AI-инструменты, напоминания о сроках
- Zero Trust: верификация device-подписей (ECDSA P-256) самим движком
- Проактивный запуск агентов при срабатывании событий
- Аутентификацию (JWT)
- **Kafka event bus** (`agent.requests`, `chat.events`) — асинхронный
  инференс агентов + проактивная доставка сообщений в WebClient
- **Проекты и чаты задач** — группировка задач, автоматические чаты,
  audit trail, ссылки `!<task>` / `!!<project>`

**WebClient** отвечает за:
- Real-time (WebSocket)
- UI
- Redis (сессии, Socket.IO adapter, rate-limit, message outbox)

## Инфраструктура

RuGPT размещён на 7 узлах. Подробности по железу, CPU pinning, RAM/дискам, Kafka-конфигу, Zero-Trust и security-пунктам — в `architecture-full-2026-04-22.md` и `docs/networking.md`.

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
| FastAPI + LangChain/LangGraph + Kafka consumer + Scheduler + IngestQueue | **rugpt-container** (B.I.1) | Всё в одном uvicorn-процессе. 3 pinned cores (50-52), 10 GB RAM. Зависит от Redis (nonce-store) |
| PostgreSQL 16 + pgvector + pgcrypto | **postgres-vm** (B.II.1) | Отдельная KVM-VM, **не** внутри rugpt-container. 368 GB RAM, 35 cores, 200+2000 GB диск. pgvector — одна общая схема, изоляция через `org_id AND (is_public OR user_id = viewer)` |
| Apache Kafka 3.7 (KRaft) | **docker-vm** (B.II.2) | Multi-tenant с Tika/n8n/сторонними. Топики: `agent.requests` (3p, 24h, acks=all, idempotent, key=request_id), `chat.events` (3p, 1h, key=chat_id). Auth **отсутствует** (trust by VPN) |
| Apache Tika | docker-vm (B.II.2) | `:9998`, без auth. CVE-prone (Java) |
| LiteLLM `:4000` + vLLM + CUDA | **Zver** (C) | OpenAI-compatible API. Engine ходит на `192.168.1.80:4000/v1`. Auth = `Bearer sk-dummy` (любой непустой) |
| Webclient (Next.js + NestJS + Redis) | **Prod-VPS** (A) | Доступ к Engine только через WireGuard на `10.0.0.2`. Rate-limit в Redis (100/min default, 10/min strict, WS 100/min/event/user) |
| GitLab CE Omnibus + Runner | B.I.2 + B.II.3 | Миграция из GitHub после запуска. SSH через NAT `:28351` и `:24952` |
| Dev-vm all-in-one | B.II.4 | Отдельная KVM-VM для CI preview-среды, клон prod-данных после анонимизации |
| Бэкапы (vzdump + pg_dump) | **NAS** (E) | NFS к Proxmox, Btrfs WriteOnce. SPOF |

### Сеть и криптография

- **Наружу** (internet → A): TLS 1.2/1.3 (nginx + Let's Encrypt)
- **VPN** (A ↔ B): WireGuard ChaCha20-Poly1305, подсеть `10.0.0.0/24`
- **LAN** (B внутри, B ↔ C): **plaintext** (TLS отсутствует) — trust by VPN/LAN isolation
- **App-layer**: JWT HMAC-SHA256 + ECDSA P-256 device signature (non-extractable в IndexedDB браузера). Подпись для `/api/v1/web/*` проверяет сам движок через `WebSignatureMiddleware` (`src/engine/middleware/web_signature.py`), делегируя в `SignatureService.verify_request_signature`: timestamp-окно ±5 min → device-ключи юзера → ECDSA verify → nonce-replay в Redis (`NonceStore`, fail-closed → 503 если Redis недоступен). Отдельного verify-эндпоинта нет. См. `docs/networking.md` + архитектурный отчёт
- **Публичные endpoints без signature** (`WEB_NO_SIGNATURE_ROUTES`): `/auth/login`, `/auth/engine-public-key`, `/config`, `/health`, `/notifications/telegram/webhook`
- **Engine без rate-limit** (открыт в VPN) — rate-limit только в webclient

### Хранилище состояния

- **PostgreSQL** (postgres-vm) — persistent, единственный источник истины
- **Kafka** (docker-vm) — транзиентная очередь (24h для `agent.requests`, 1h для `chat.events`)
- **Redis** — на webclient (Prod-VPS) для сессий/Socket.IO/rate-limit; **и на стороне Engine** — обязательная зависимость для `NonceStore` (anti-replay подписей, fail-closed). `Config.REDIS_URL`
- **Файлы** — `/root/rugpt/uploads/{org_id}/{user_id}/{file_id}.{ext}` на rugpt-container. Метаданные в PG (`user_files`). SHA-256 дедуп per-user
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
|  PostgreSQL (~30 таблиц):                 |
|  +-- organizations (+ timezone,           |
|  |    org_context)                         |
|  +-- users (+ is_system, department_id,   |
|  |    is_head)                             |
|  +-- departments                          |
|  +-- department_visibility                |
|  +-- roles (agent_type, tools,            |
|  |         prompt_file, rag_collection)   |
|  +-- role_subagents (supervisor -> sub)   |
|  +-- chats (type CHECK:                    |
|  |    direct/group/task/project/          |
|  |    support/poll; + task_id, project_id)|
|  +-- messages (+ ai_is_valid, metadata)   |
|  +-- message_attachments                  |
|  +-- chat_read_state                      |
|  +-- calendar_events                      |
|  +-- notification_channels                |
|  +-- notification_log                     |
|  +-- in_app_notifications                 |
|  +-- tasks (+ project_id, created_by,     |
|  |    awaiting_review_at, proposed_*,      |
|  |    priority, tsv)                        |
|  +-- task_participants                    |
|  +-- task_polls                           |
|  +-- task_reports                         |
|  +-- task_events (audit trail)            |
|  +-- projects (+ department_id)           |
|  +-- agent_runs (async idempotency)       |
|  +-- user_files (+ RAG: summary,          |
|  |    summary_embedding, is_table, tsv,   |
|  |    folder_id, user_content_hash)        |
|  +-- user_file_folders (personal folders) |
|  +-- chunks (pgvector)                    |
|  +-- tables_rows_chunks (pgvector)        |
|  +-- correction_rules (+ embedding,        |
|  |    is_active, scope)                     |
|  +-- memory_snapshots (долгая память)     |
|  +-- support_tickets                      |
|  +-- support_ticket_events                |
|  +-- invoices                             |
|  +-- user_devices                         |
|                                           |
|  Kafka event bus:                         |
|  +-- agent.requests (24h, 3 partitions)   |
|  |    producer: AIService                 |
|  |    consumer: AgentRequestHandler        |
|  +-- chat.events (1h, 3 partitions)       |
|       producers: InAppNotificationService,|
|         AgentRequestHandler, AIService,    |
|         SupportTicketService,              |
|         CorrectionRuleService             |
|       consumer: NestJS KafkaConsumerSvc   |
|                 -> Socket.IO broadcast    |
|                                           |
|  Agents (LangChain/LangGraph):            |
|  +-- AgentExecutor (simple / supervisor)  |
|  +-- simple: create_agent (ReAct, с tools)|
|  |    или прямой ChatOpenAI (без tools)   |
|  +-- supervisor: langgraph_supervisor     |
|  |    .create_supervisor + role_subagents |
|  |    + handoff                            |
|  +-- ToolRegistry (19 инструментов)       |
|  +-- PromptCache (файлы, git-версии)      |
|  +-- rule_generator (коррекция AI)        |
|  +-- MemoryService (долгая память)        |
|  +-- ActionRegistry (show_modal actions)  |
|                                           |
|  RAG:                                     |
|  +-- RAGService (Tika + pgvector)         |
|  +-- IngestQueue (ThreadPoolExecutor)     |
|  +-- rag_search / table_rows / expand     |
|                                           |
|  Scheduler:                               |
|  +-- Calendar events polling              |
|  +-- Morning polls / Evening reports      |
|  +-- Overdue checks + invoice-due remind  |
|  +-- Per-org timezone support             |
|                                           |
|  Notifications:                           |
|  +-- TelegramSender (Bot API)             |
|  +-- EmailSender (SMTP)                   |
|  +-- InAppNotificationService             |
|  |    (-> Kafka chat.events)              |
|  +-- SupportNotificationService           |
|                                           |
|  Files / Support / Invoices:              |
|  +-- FileService / FolderService          |
|  +-- LocalStorageAdapter (filesystem)     |
|  +-- SupportTicketService                 |
|  +-- InvoiceService                       |
|                                           |
|  Security (Zero Trust):                   |
|  +-- WebSignatureMiddleware               |
|  +-- SignatureService (ECDSA P-256)       |
|  +-- crypto-функции + NonceStore (Redis)  |
|                                           |
|  LLM (LiteLLM proxy -> vLLM):             |
|  +-- ChatOpenAI (langchain-openai)        |
|  |    -> LiteLLM 192.168.1.80:4000/v1     |
|  +-- DEFAULT_MODEL google/gemma-4-31B-it  |
|  +-- EMBEDDING Qwen/Qwen3-Embedding-0.6B  |
|  |    (1024-dim для pgvector)             |
|                                           |
+-------------------------------------------+
```

## Структура проекта

```
/root/rugpt/
+-- docs/                              # Документация
+-- src/
|   +-- engine/
|       +-- app.py                     # FastAPI приложение (middleware, routers, lifecycle)
|       +-- config.py                  # Конфигурация из .env
|       +-- run.py                     # Entry point (uvicorn)
|       +-- constants.py               # Константы (типы файлов, лимиты)
|       +-- logging_context.py         # CorrelationID / RequestLogging middleware + ContextVar
|       +-- unified_logger/            # JSONL-логгер по компонентам
|       |
|       +-- models/                    # Dataclass-модели
|       |   +-- organization.py        # Организация (+ timezone, org_context)
|       |   +-- user.py                # Пользователь (+ is_system, department_id, is_head)
|       |   +-- role.py                # AI-агент (agent_type simple/supervisor, tools, prompt_file)
|       |   +-- chat.py                # Чат (direct/group/task/project/support/poll)
|       |   +-- message.py             # Сообщение, Mention, SenderType, MentionType (+ metadata)
|       |   +-- message_attachment.py  # Вложения сообщений
|       |   +-- calendar_event.py      # Календарное событие
|       |   +-- notification.py        # NotificationChannel, NotificationLog
|       |   +-- in_app_notification.py # In-app уведомление (колокольчик)
|       |   +-- task.py                # Задача (+ project_id, priority, proposed_*)
|       |   +-- task_participant.py    # Участники задачи
|       |   +-- task_poll.py           # Утренний опрос
|       |   +-- task_report.py         # Вечерний отчёт
|       |   +-- task_event.py          # Audit trail задач
|       |   +-- project.py             # Проект (+ department_id)
|       |   +-- agent_run.py           # Async agent execution (idempotency)
|       |   +-- user_file.py           # Файл пользователя
|       |   +-- user_file_folder.py    # Личные папки
|       |   +-- correction_rule.py     # Правило коррекции AI
|       |   +-- memory_snapshot.py     # Долговременная память агента
|       |   +-- support_ticket.py      # Тикет тех-поддержки
|       |   +-- support_ticket_event.py# События тикета
|       |   +-- invoice.py             # Счёт
|       |   +-- department.py          # Department, DepartmentVisibility
|       |   +-- rag.py                 # RelatedDoc, ChunkSearchResult
|       |
|       +-- storage/                   # PostgreSQL CRUD (asyncpg)
|       |   +-- base.py                # Базовый класс с пулом
|       |   +-- org_storage.py
|       |   +-- user_storage.py
|       |   +-- role_storage.py
|       |   +-- role_subagent_storage.py
|       |   +-- chat_storage.py
|       |   +-- message_storage.py
|       |   +-- message_attachment_storage.py
|       |   +-- chat_read_state_storage.py
|       |   +-- calendar_storage.py
|       |   +-- notification_channel_storage.py
|       |   +-- notification_log_storage.py
|       |   +-- in_app_notification_storage.py
|       |   +-- task_storage.py
|       |   +-- task_participant_storage.py
|       |   +-- task_poll_storage.py
|       |   +-- task_report_storage.py
|       |   +-- task_event_storage.py
|       |   +-- project_storage.py
|       |   +-- agent_run_storage.py   # idempotency CAS
|       |   +-- user_file_storage.py
|       |   +-- user_file_folder_storage.py
|       |   +-- correction_rule_storage.py
|       |   +-- memory_snapshot_storage.py
|       |   +-- support_ticket_storage.py
|       |   +-- support_ticket_event_storage.py
|       |   +-- invoice_storage.py
|       |   +-- device_storage.py
|       |   +-- department_storage.py
|       |   +-- rag_store.py           # RAG: chunks, table rows, hybrid search
|       |   +-- storage_adapter.py     # StorageAdapter ABC + LocalStorageAdapter
|       |
|       +-- services/                  # Бизнес-логика
|       |   +-- engine_service.py      # Композитный singleton (все storage + сервисы)
|       |   +-- org_service.py
|       |   +-- users_service.py
|       |   +-- roles_service.py       # Только чтение + кеш промптов
|       |   +-- role_subagent_service.py # Маппинг supervisor -> сабагенты
|       |   +-- chat_service.py        # + task/project chat auto-create
|       |   +-- mention_service.py
|       |   +-- reference_service.py   # Резолв !<task>/!!<project> в сообщениях
|       |   +-- ai_service.py          # AgentExecutor + async mode (Kafka publish)
|       |   +-- prompt_cache.py        # In-memory кеш промптов из файлов
|       |   +-- calendar_service.py    # Календарные события + croniter
|       |   +-- scheduler_service.py   # Фоновый polling + task jobs + per-org timezone
|       |   +-- notification_service.py # Оркестрация внешних каналов (Telegram/Email)
|       |   +-- in_app_notification_service.py # Колокольчик -> Kafka chat.events
|       |   +-- task_service.py        # Задачи + bell-уведомления + events + участники
|       |   +-- task_event_service.py  # Audit trail задач
|       |   +-- project_service.py     # CRUD проектов
|       |   +-- task_poll_service.py   # Утренние опросы
|       |   +-- task_report_service.py # Вечерние отчёты
|       |   +-- file_service.py        # Загрузка / скачивание файлов
|       |   +-- folder_service.py      # Личные папки (cascade delete, cycle/depth guard)
|       |   +-- rag_service.py         # Индексация: Tika + chunking + embedding
|       |   +-- correction_rule_service.py # Правила коррекции AI (+ embedding-поиск)
|       |   +-- memory_service.py      # Долговременная память (summary snapshots)
|       |   +-- support_ticket_service.py # Тикеты тех-поддержки
|       |   +-- support_notification_service.py # Fan-out операторам Support
|       |   +-- invoice_service.py     # Счета (binary в user_files)
|       |   +-- department_service.py  # Отделы + видимость (get_visible_user_ids)
|       |   +-- signature_service.py   # ECDSA P-256 верификация web-подписей
|       |   +-- nonce_store.py         # NonceStore (Redis, anti-replay, fail-closed)
|       |   +-- crypto_service.py      # crypto-функции (sign/verify, key parsing)
|       |
|       +-- security/                  # Хелперы Zero Trust
|       |   +-- route_match.py         # Матчинг web-роутов под подпись
|       |   +-- signature_payload.py   # Каноничный payload подписи
|       |
|       +-- middleware/
|       |   +-- web_signature.py       # WebSignatureMiddleware (/api/v1/web/*)
|       |
|       +-- actions/                   # Структурированные действия (show_modal)
|       |   +-- registry.py            # ActionRegistry
|       |   +-- bootstrap.py           # register_all (типы действий)
|       |   +-- invoice_actions.py     # Действия по счетам
|       |
|       +-- kafka/                     # Kafka event bus
|       |   +-- producer.py            # KafkaProducerService (aiokafka, no-op при disabled)
|       |   +-- consumer.py            # KafkaConsumerLoop (background asyncio)
|       |   +-- agent_handler.py       # AgentRequestHandler (CAS + generate + publish)
|       |
|       +-- routes/                    # API endpoints (детали см. docs/api.md)
|       |   +-- health.py              # Health checks
|       |   +-- auth.py                # Аутентификация (login, register, refresh, devices)
|       |   +-- organizations.py
|       |   +-- users.py
|       |   +-- roles.py               # GET + admin cache
|       |   +-- chats.py               # self-prefixed router (см. ниже)
|       |   +-- calendar.py
|       |   +-- notifications.py
|       |   +-- in_app_notifications.py
|       |   +-- tasks.py               # + /{id}/chat, /{id}/events, /archive, participants
|       |   +-- task_polls.py
|       |   +-- task_reports.py
|       |   +-- projects.py            # CRUD + /{id}/chat
|       |   +-- files.py
|       |   +-- folders.py             # Личные папки
|       |   +-- rag.py
|       |   +-- departments.py         # admin only
|       |   +-- support.py             # Тикеты тех-поддержки
|       |   +-- corrections.py         # Правила коррекции AI
|       |   +-- actions.py             # Подтверждение/исполнение action'ов (show_modal)
|       |   +-- invoices.py            # Счета
|       |
|       +-- agents/                    # Агентная система
|       |   +-- executor.py            # AgentExecutor -- маршрутизация по agent_type
|       |   +-- result.py              # AgentResult, ToolCall dataclasses
|       |   +-- metadata.py            # Метаданные агентного запуска
|       |   +-- middleware.py          # Middleware агентного пайплайна
|       |   +-- runtime.py             # Runtime-контекст (org_id/user_id инъекция)
|       |   +-- graphs/
|       |   |   +-- simple.py          # langchain.agents.create_agent (ReAct, с tools) / прямой ChatOpenAI
|       |   |   +-- supervisor.py      # langgraph_supervisor.create_supervisor + handoff
|       |   |   +-- rule_generator.py  # Генерация правил коррекции из обратной связи
|       |   +-- tools/                 # 19 инструментов (см. ниже)
|       |       +-- registry.py        # ToolRegistry
|       |       +-- calendar_tool.py   # calendar_create / calendar_query (factory)
|       |       +-- task_tool.py       # task_create/query/update/deadline_proposal/get_own (factory)
|       |       +-- rag_tool.py        # rag_search (гибридный поиск)
|       |       +-- expand_chunk_tool.py# expand_chunk
|       |       +-- table_rows_tool.py # table_rows_search
|       |       +-- list_documents.py  # list_documents / list_own_documents
|       |       +-- user_tool.py       # user_search (factory)
|       |       +-- analyze_image.py   # analyze_image (factory)
|       |       +-- show_modal.py      # show_modal (action confirmation)
|       |       +-- list_invoices.py   # list_invoices (factory)
|       |       +-- get_invoice.py     # get_invoice (factory)
|       |       +-- web_tool.py        # web_search (Perplexity)
|       |       +-- role_call_tool.py  # role_call (stub)
|       |
|       +-- notifications/             # Каналы доставки
|       |   +-- base_sender.py         # Абстрактный интерфейс
|       |   +-- telegram_sender.py     # Telegram Bot API (httpx)
|       |   +-- email_sender.py        # SMTP (aiosmtplib)
|       |
|       +-- prompts/                   # Системные промпты (git-версионирование)
|       |   +-- lawyer.md, accountant.md, hr.md, chu.md, humorist.md
|       |   +-- admin_assistant.md, pm.md, reasoner.md, report_generator.md
|       |   +-- doc_search.md, support_assistant.md, invoice_clerk.md
|       |   +-- poll_interviewer.md, poll_summarizer.md
|       |   +-- memory_summary.md, agent_compaction_summary.md
|       |   +-- rag_doc_summary.md, rag_table_summary.md, web_search.md
|       |   +-- proftech_*.md          # Демо-роли организации ProfTech
|       |   +-- subagents/, tools/     # Промпты сабагентов и инструментов
|       |
|       +-- tasks/                     # Фоновые задачи
|       |   +-- ingest_queue.py        # IngestQueue (ThreadPoolExecutor)
|       |
|       +-- llm/                       # Legacy LLM-провайдеры (только .pyc, исходников нет)
|       |   +-- providers/             # base / ollama — скомпилированы, в рантайме не используются
|       |
|       +-- migrations/                # SQL миграции (001-046, без пропусков)
|       |   +-- 001_initial ... 013_rag_functions
|       |   +-- 014_org_timezone
|       |   +-- 015_departments
|       |   +-- 016_task_ownership
|       |   +-- 017_projects_and_task_chats
|       |   +-- 018_pm_role_and_agent_runs
|       |   +-- 019_three_main_agents
|       |   +-- 020_litellm_model_names
|       |   +-- 021_agent_tools_user_search_list_documents
|       |   +-- 022_pm_calendar_tools
|       |   +-- 023_memory_snapshots_and_correction_rules_rework
|       |   +-- 024_search_correction_rules_function
|       |   +-- 025_correction_rules_is_active
|       |   +-- 026_support_tickets
|       |   +-- 027_tasks_tsv
|       |   +-- 028_report_generator_role
|       |   +-- 029_poll_chat_and_roles
|       |   +-- 030_lower_oc_search_threshold
|       |   +-- 031_task_priority
|       |   +-- 032_chunk_context_by_index
|       |   +-- 033_chat_attachments
|       |   +-- 034_task_participants
|       |   +-- 035_user_files_user_content_hash
|       |   +-- 036_chat_read_state
|       |   +-- 037_user_file_folders
|       |   +-- 038_rename_private_documents_tool
|       |   +-- 039_supervisor_subagent_roles
|       |   +-- 040_search_related_docs_admin_access
|       |   +-- 041_search_correction_rules_scope_filters
|       |   +-- 042_cascade_org_fks
|       |   +-- 043_messages_metadata
|       |   +-- 044_invoices
|       |   +-- 045_invoice_clerk_and_notify_tracker
|       |   +-- 046_project_department
|       |
|       +-- utils/                     # Утилиты (token_counter и пр.)
|
+-- docker-compose.kafka.yml            # Apache Kafka 3.7 KRaft
+-- scripts/
|   +-- kafka_up.sh / kafka_down.sh / kafka_init.sh
|
+-- requirements.txt
+-- setup.sh / migrate.sh / deploy.sh / local_restart.sh / sync.sh
+-- .env.example / .env
```

## Приложение (app.py)

### Стек middleware и порядок

FastAPI выполняет middleware «снаружи внутрь», и последний добавленный
`add_middleware` оборачивает все добавленные раньше. Поэтому порядок добавления
обратный порядку исполнения. Фактический порядок на входящем запросе:

```
WebSignatureMiddleware   (добавлен последним -> исполняется первым)
   -> CORSMiddleware       (allow_origins=["*"], credentials/methods/headers=*)
      -> CorrelationIDMiddleware  (привязывает X-Correlation-ID на весь запрос)
         -> RequestLoggingMiddleware (логирует внутри correlation-scope)
            -> route handler
```

- `WebSignatureMiddleware` — проверяет device-подпись для `/api/v1/web/*`,
  пропускает `WEB_NO_SIGNATURE_ROUTES`. Делегирует в `SignatureService`.
- `RequestLoggingMiddleware` добавлен первым специально — чтобы лог-строки
  попадали уже внутрь correlation-scope.

### Жизненный цикл

- `@app.on_event("startup")` → `init_engine_service()` — создаёт singleton
  `EngineService`, открывает все storage-пулы, инициализирует Redis nonce-store,
  RAG-store, token counter, стартует Kafka producer + consumer (`agent.requests`),
  ActionRegistry, регистрирует поздние tools (`show_modal`, `list_invoices`,
  `get_invoice`), запускает Scheduler.
- `@app.on_event("shutdown")` → `ingest_queue.shutdown(wait=False)` (сбрасывает
  очередь индексации, запущенные потоки доигрывают), затем `engine.close()`
  (закрытие пулов, остановка scheduler/Kafka/nonce-store).
- Глобальный `@app.exception_handler(Exception)` — прогоняет необработанные
  исключения через `rugpt`-логгер с `exc_info=True` (uvicorn по дефолту пишет
  traceback только в stderr, JSONL-handler его не видит) и возвращает 500 с
  `{"detail": "Internal error: <Type>: <msg>"}`.

### Роутеры (21)

Все, кроме `chats`, монтируются с префиксом `/api/v1`. `chats_router`
self-prefixed (префикс задан внутри роутера), монтируется без `prefix`.

```
health, auth, organizations, users, roles, chats (self-prefixed),
calendar, notifications, in_app_notifications, tasks, task_polls,
task_reports, files, folders, rag, departments, projects, support,
corrections, actions, invoices
```

Детали эндпоинтов — в `docs/api.md`.

## LLM

Весь инференс (генерация + эмбеддинги) идёт через единый OpenAI-совместимый
шлюз — **LiteLLM proxy** на Zver (`http://192.168.1.80:4000/v1`, auth
`Bearer sk-dummy`). LiteLLM сам разводит вызовы на vLLM/Ollama по имени модели.

- Клиент: **`ChatOpenAI`** (`langchain-openai`), base_url = `Config.LLM_BASE_URL`.
- `DEFAULT_MODEL` = `google/gemma-4-31B-it` (генерация, image-анализ, RAG-summary).
- `EMBEDDING_MODEL` = `Qwen/Qwen3-Embedding-0.6B`, 1024-dim (`RAG_VECTOR_DIM`) для pgvector.
- НЕ Ollama напрямую: каталог `src/engine/llm/` содержит только `.pyc`
  (legacy-провайдеры), исходников нет, в рантайме не используется.
- `web_search` ходит в Perplexity (`PERPLEXITY_API_KEY`).

Подробнее — `docs/llm.md` и `docs/lang-ecosystem.md`.

## Flow сообщения с @@ упоминанием

```
1. WebClient: пользователь пишет "@@lawyer проверь договор"
2. WebClient -> Engine: POST /api/v1/web/chats/{chat_id}/messages
3. Engine: MentionService парсит @@ -> находит роль lawyer
4. Engine: сохраняет сообщение пользователя в PostgreSQL
5. Engine: находит User c role_id -> Role(lawyer), читает agent_type
6. AIService: создаёт agent_runs(pending) и публикует в 'agent.requests',
   возвращает agent_pending=true мгновенно (HTTP не ждёт инференс). Это
   единственный путь — Kafka обязательна.
7. AgentExecutor выбирает граф по agent_type:
   - simple: при наличии tools -> ReAct agent (langchain.agents.create_agent);
             без tools -> прямой вызов ChatOpenAI (LiteLLM)
   - supervisor: langgraph_supervisor.create_supervisor с роль-сабагентами
             (role_subagents) и handoff-инструментами
8. PromptCache читает системный промпт из файла (или кеша), коррекционные
   правила/память подмешиваются в контекст
9. RunnableConfig инжектирует org_id, user_id в tools (agents/runtime.py)
10. AI-ответ сохраняется (ai_is_valid=NULL, pending review)
11. Результат доставляется во фронт через chat.events + Socket.IO
12. Владелец роли валидирует / редактирует / отклоняет
13. При отклонении: CorrectionRuleService генерирует правило через
    rule_generator graph
```

## Проактивный запуск агента (Scheduler)

```
SchedulerService: фоновый asyncio task, polling каждые SCHEDULER_POLL_INTERVAL (30 сек)

=== Calendar Events ===
1. SELECT calendar_events WHERE is_active AND next_trigger_at <= NOW()
2. Для каждого: mark_triggered() -> загрузка роли -> AgentExecutor.execute()
   -> NotificationService (Telegram/Email) -> notification_log

=== Task Jobs (per-org timezone) ===
Morning: check_overdue(), expire_stale_polls(), create_daily_poll()
Evening: generate_report() для руководителей
+ retry «зависших» poll-инициалов, напоминания о сроках счетов (_notify_invoice_due)
```

## Система упоминаний и ссылок

### @ упоминание (USER)
- Формат: `@username` — уведомляет пользователя (in-app колокольчик)

### @@ упоминание (AI_ROLE)
- Формат: `@@username` — вызывает AI-агента через AgentExecutor
- Ответ требует валидации владельцем роли (`ai_is_valid`: NULL -> true/false)
- Асинхронный режим (Kafka): вызов не блокирует HTTP, результат через
  `chat.events` + Socket.IO

### ! / !! ссылки на задачи и проекты
- `!<task-uuid>` / `!!<project-uuid>` — кликабельные pills на чат задачи/проекта
- Per-viewer visibility: недоступные рендерятся серым disabled без названия
- Вставляются через autocomplete в `ChatInput`

## Типы чатов

Колонка `chats.chat_type` с CHECK на множество значений:

1. **direct** — прямые сообщения между двумя пользователями
2. **group** — групповой чат
3. **task** — чат задачи, автосоздаётся с участниками {creator, assignee}
4. **project** — чат проекта, автосоздаётся при первой задаче в проекте
5. **support** — чат тикета тех-поддержки (user <-> support_ai / оператор)
6. **poll** — чат утреннего опроса (интервью с poll_interviewer)

## Kafka event bus

Асинхронная событийная шина между Engine и WebClient, закрывает два кейса:

**1. Проактивная доставка сообщений от Engine в WS.** Сообщения, которые Engine
создаёт сам (bell-уведомления о задачах, support, scheduler), не попадают в
обычный HTTP-цикл `io.emit`. Через `chat.events` они доезжают до фронта live.

**2. Асинхронный LLM-инференс.** HTTP-запрос не держит соединение 30-300 сек:
возвращает `agent_pending=true` сразу, run крутится в фоновом consumer'е,
результат доставляется через тот же канал.

**Топики:**

| Топик | Producer | Consumer | Partitions | Retention |
|---|---|---|---|---|
| `agent.requests` | Engine (`AIService`) | Engine background (`AgentRequestHandler`) | 3 | 24h |
| `chat.events` | Engine: `InAppNotificationService`, `AgentRequestHandler`, `AIService`, `SupportTicketService`, `CorrectionRuleService` | NestJS `KafkaConsumerService` -> Socket.IO | 3 | 1h |

**Поток для `@@mention`** (async):

```
юзер пишет "@@role привет"
  -> Engine POST /chats/{id}/messages
  -> сохранить user message
  -> AIService: agent_runs(pending) + publish 'agent.requests' + return agent_pending=true
  -> HTTP 200 -> NestJS io.emit user message (юзер видит своё мгновенно)

[фоновый asyncio task в том же процессе]
KafkaConsumerLoop('agent.requests') -> AgentRequestHandler:
  1. agent_runs CAS pending -> running (atomic UPDATE ... RETURNING).
     Уже running/done -> skip (идемпотентность при Kafka redelivery)
  2. load user message -> AIService.generate_response (LangGraph + tools)
  3. persist AI message -> agent_runs mark_done -> publish 'chat.events'

[NestJS] consume 'chat.events' -> SocketGateway.broadcastToChat -> фронт live
```

**Реальный механизм уведомлений по задачам.** Отдельного
`TaskNotificationService` / PM-агента в коде **нет**. `TaskService` на
событиях (`take_task`, `submit_for_review`, `approve_task`, `reject_task`,
`add_participant`, `check_overdue` и т.д.) делает две вещи:
`_record_event(...)` (audit trail в `task_events`) и `_bell_to_recipients(...)`
— best-effort in-app уведомление (колокольчик) всем участникам кроме актора
через `InAppNotificationService`. Именно `InAppNotificationService` публикует
событие в Kafka `chat.events`, откуда оно через NestJS долетает до фронта live.

**Идемпотентность** — `agent_runs(request_id PK, status, ...)` с атомарным CAS
`UPDATE WHERE status='pending' RETURNING`. При redelivery второй consumer
получает False от `mark_running` и скипает молча.

**Broadcast pattern в NestJS** — каждый инстанс в своей consumer group
(`webclient-<hostname>-<pid>`), все инстансы получают все события.

**Kafka обязательна.** Синхронного fallback'а нет — агентные вызовы всегда
идут через `agent.requests`. Если брокер недоступен на старте, `KafkaProducerService.start()`
бросает исключение и Engine не поднимается (fail-fast). Тесты используют
мок-продюсер.

**Инфраструктура:** `docker-compose.kafka.yml` (Apache Kafka 3.7, KRaft,
single-node), скрипты `scripts/kafka_up.sh|down.sh|init.sh`.

## Безопасность (Zero Trust / App-layer)

- **WebSignatureMiddleware** (`middleware/web_signature.py`) — навешан на весь
  `/api/v1/web/*`, проверяет device-подпись каждого мутирующего запроса.
- **SignatureService** (`services/signature_service.py`) — каноничный payload
  (`security/signature_payload.py`), timestamp-окно ±5 мин
  (`SIG_TIMESTAMP_TOLERANCE_SECONDS`), подбор device-ключей юзера, ECDSA P-256 verify.
- **crypto-функции** (`services/crypto_service.py`) — sign/verify, парсинг
  ключей (не класс `CryptoService`).
- **NonceStore** (`services/nonce_store.py`) — anti-replay в **Redis**,
  TTL = `NONCE_TTL_SECONDS` (5 мин), **fail-closed**: при недоступности Redis
  запрос отклоняется (503). Из-за этого Redis — обязательная зависимость Engine.
- Отдельного эндпоинта верификации подписи нет; `WEB_NO_SIGNATURE_ROUTES`
  пропускают login / engine-public-key / config / health / telegram-webhook.

## Порты

| Сервис | Порт | Хост | Auth |
|---|---|---|---|
| Engine FastAPI | 8100 | rugpt-container (B.I.1) `127.0.0.1` | JWT + ECDSA device signature |
| Engine nginx (reverse-proxy) | 80 | rugpt-container | фильтр `/api/v1/web/*` → FastAPI, остальное 403 |
| PostgreSQL | 5432 | postgres-vm (B.II.1) `192.168.1.82` | scram-sha-256 |
| LiteLLM proxy | 4000 | Zver (C) `192.168.1.80` | `Bearer sk-dummy` |
| Apache Kafka | 9092 | docker-vm (B.II.2) `192.168.1.84` | **нет** (trust by VPN) |
| Apache Tika | 9998 | docker-vm (B.II.2) `192.168.1.84` | **нет** |
| Redis (Engine nonce-store) | 6379 | rugpt-container / `Config.REDIS_URL` | по конфигу |
| WebClient nginx | 443 / 80 | Prod-VPS (A) | TLS Let's Encrypt |
| Redis (webclient) | 6379 | Prod-VPS (loopback) | password (prod) |

## API Endpoints

Полный перечень эндпоинтов с параметрами — в **`docs/api.md`**.

Группы роутов (все под `/api/v1`, кроме self-prefixed chats):
`auth`, `organizations`, `users`, `roles`, `chats`, `calendar`,
`notifications`, `in-app-notifications`, `tasks`, `task-polls`,
`task-reports`, `projects`, `files`, `folders`, `rag`, `departments`,
`support`, `corrections`, `actions`, `invoices`, `health`.

## Сетевая архитектура

Подробнее о VPN, Nginx и маршрутизации запросов — `docs/networking.md`.

## Запуск

```bash
cd /root/rugpt

./setup.sh          # установка
./migrate.sh        # миграции 001-046

source venv/bin/activate
uvicorn src.engine.app:app --host 127.0.0.1 --port 8100
```
