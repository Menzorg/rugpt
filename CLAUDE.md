# RuGPT Engine

Корпоративный AI-ассистент с агентной системой, календарём, уведомлениями и multi-tenancy.

## Стек

- Python 3.10+, FastAPI, Uvicorn
- PostgreSQL (asyncpg, пул 2-10 соединений)
- LLM: Ollama / vLLM (модель по умолчанию: qwen2.5:7b)
- LangChain + LangGraph (агентный фреймворк)
- JWT (PyJWT), bcrypt для паролей
- croniter (рекуррентные события)
- aiosmtplib (email-уведомления)

## Структура проекта

```
src/engine/
├── app.py              # FastAPI приложение, entry point
├── config.py           # Конфигурация из .env
├── run.py              # CLI runner (uvicorn)
│
├── models/             # Dataclass-модели
│   ├── organization.py
│   ├── user.py
│   ├── role.py             # Role + agent_type, tools, prompt_file
│   ├── chat.py
│   ├── message.py
│   ├── calendar_event.py   # Календарные события
│   └── notification.py     # NotificationChannel, NotificationLog
│
├── storage/            # PostgreSQL CRUD (asyncpg)
│   ├── base.py
│   ├── org_storage.py
│   ├── user_storage.py
│   ├── role_storage.py
│   ├── chat_storage.py
│   ├── message_storage.py
│   ├── calendar_storage.py
│   ├── notification_channel_storage.py
│   └── notification_log_storage.py
│
├── services/           # Бизнес-логика
│   ├── engine_service.py       # Композитный singleton
│   ├── org_service.py
│   ├── users_service.py
│   ├── roles_service.py        # Только чтение (роли предсозданы)
│   ├── chat_service.py
│   ├── mention_service.py
│   ├── ai_service.py           # Использует AgentExecutor
│   ├── prompt_cache.py         # Кеш промптов из файлов
│   ├── calendar_service.py     # CRUD событий, croniter
│   ├── scheduler_service.py    # Фоновый polling + проактивный запуск агентов
│   └── notification_service.py # Оркестрация уведомлений по каналам
│
├── routes/             # FastAPI роутеры
│   ├── health.py
│   ├── auth.py
│   ├── organizations.py
│   ├── users.py
│   ├── roles.py            # GET + admin cache clear (без CRUD)
│   ├── chats.py
│   ├── calendar.py         # CRUD календарных событий
│   └── notifications.py    # Каналы, Telegram webhook, лог
│
├── agents/             # Агентная система (LangChain/LangGraph)
│   ├── executor.py         # AgentExecutor — маршрутизатор по agent_type
│   ├── result.py           # AgentResult dataclass
│   ├── graphs/
│   │   ├── simple.py       # prompt → LLM (или ReAct с tools)
│   │   ├── chain.py        # Последовательные шаги
│   │   └── multi_agent.py  # LangGraph StateGraph
│   └── tools/
│       ├── registry.py     # ToolRegistry
│       ├── calendar_tool.py    # create/query события (factory)
│       ├── rag_tool.py         # Поиск по документам (stub)
│       ├── web_tool.py         # Веб-поиск (stub)
│       └── role_call_tool.py   # Вызов другой роли (stub)
│
├── notifications/      # Каналы доставки уведомлений
│   ├── base_sender.py      # Абстрактный интерфейс
│   ├── telegram_sender.py  # Telegram Bot API (httpx)
│   └── email_sender.py     # SMTP (aiosmtplib)
│
├── prompts/            # Системные промпты (git-версионирование)
│   ├── lawyer.md
│   ├── accountant.md
│   ├── hr.md
│   └── chu.md
│
├── llm/providers/      # LLM провайдеры (legacy, для health checks)
│   ├── base.py
│   └── ollama.py
│
├── migrations/         # SQL миграции
│   ├── 001_initial.sql
│   └── 002_role_evolution.sql
│
└── utils/
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

Основные роуты: `/auth/*`, `/users/*`, `/roles/*`, `/chats/*`, `/organizations/*`, `/calendar/*`, `/notifications/*`, `/health`

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
- AgentExecutor — маршрутизация по `role.agent_type` (simple/chain/multi_agent)
- ToolRegistry — реестр инструментов (calendar, rag, web, role_call)
- LangChain (ChatOllama) + LangGraph (StateGraph) для оркестрации

### Календарь + Планировщик

- CalendarService: события (one_time / recurring с cron)
- SchedulerService: фоновый asyncio task, polling каждые 30 сек
- При срабатывании: проактивный запуск агента → уведомление

### Уведомления

- NotificationService: оркестрация по каналам (priority desc)
- Каналы: Telegram (Bot API), Email (SMTP)
- Telegram-бот: webhook `/api/v1/notifications/telegram/webhook`, /start привязка
- notification_log: лог всех попыток доставки

## БД

PostgreSQL, база `rugpt`. Таблицы: organizations, users, roles, chats, messages, calendar_events, notification_channels, notification_log.
Миграции в `src/engine/migrations/`. Soft delete через is_active/is_deleted.

## LLM

- AgentExecutor: LangChain ChatOllama → ReAct agent (с tools) или прямой вызов
- OllamaProvider: legacy, для health checks и model listing
- Каждая Role имеет `agent_type`, `tools`, `prompt_file`
- Таймаут: 120-300 секунд (CPU-инференс)
- Планируется Kafka для асинхронных очередей к GPU (см. `docs/llm.md`)

## Документация

`docs/`: architecture.md, api.md, storage.md, services.md, llm.md, models.md, networking.md, lang-ecosystem.md

## Логи

`/root/rugpt/logs/` (если настроено через logging config)

## Связанные проекты

- WebClient: `/root/webclient_rugpt` — тонкий proxy-клиент (NestJS + Next.js)
