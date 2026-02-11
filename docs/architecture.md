# RuGPT Engine Architecture

## Обзор

**RuGPT** — корпоративный AI-ассистент с агентной системой, календарём, уведомлениями и multi-tenancy.

**Engine** отвечает за:
- Управление организациями, пользователями, ролями (агентами)
- Хранение чатов и сообщений
- Систему упоминаний (@ и @@)
- Агентную систему (LangChain/LangGraph): simple, chain, multi_agent
- Календарь + фоновый планировщик (cron, рекуррентные события)
- Уведомления (Telegram-бот, Email)
- Проактивный запуск агентов при срабатывании событий
- Аутентификацию (JWT)

**WebClient** отвечает за:
- Real-time (WebSocket)
- UI

## Архитектура

```
┌─────────────────────────────────────┐
│       WebClient (10.0.0.1)           │
│  (NestJS + Next.js)                  │
│                                      │
│  RuGPTEngineAdapter:                 │
│  └─ POST /api/v1/web/chats/...      │
│  └─ GET  /api/v1/web/calendar/...   │
│  └─ POST /api/v1/web/notifications/. │
│                                      │
└───────────────────┬──────────────────┘
                    │ HTTP через VPN
                    ▼
┌───────────────────────────────────────┐
│  Nginx (10.0.0.2:80)                 │
│  └─ /api/v1/web/* → :8100/api/v1/*  │
│     всё остальное → 403              │
└───────────────────┬───────────────────┘
                    │
                    ▼
┌───────────────────────────────────────┐
│           Engine (FastAPI)            │
│                                       │
│  PostgreSQL:                          │
│  ├─ organizations                     │
│  ├─ users (org_id, role_id)           │
│  ├─ roles (agent_type, tools,         │
│  │         prompt_file)               │
│  ├─ chats                             │
│  ├─ messages                          │
│  ├─ calendar_events                   │
│  ├─ notification_channels             │
│  └─ notification_log                  │
│                                       │
│  Agents (LangChain/LangGraph):        │
│  ├─ AgentExecutor (simple/chain/multi)│
│  ├─ ToolRegistry (calendar, rag, web) │
│  └─ PromptCache (файлы, git-версии)  │
│                                       │
│  Scheduler:                           │
│  └─ Background task → agent exec →   │
│     notification delivery             │
│                                       │
│  Notifications:                       │
│  ├─ TelegramSender (Bot API)          │
│  └─ EmailSender (SMTP)               │
│                                       │
│  LLM (Ollama/vLLM):                   │
│  └─ ChatOllama (LangChain)            │
│                                       │
└───────────────────────────────────────┘
```

## Структура проекта

```
/root/rugpt/
├── docs/                           # Документация
├── src/
│   └── engine/
│       ├── app.py                  # FastAPI приложение
│       ├── config.py               # Конфигурация
│       ├── run.py                  # Entry point
│       │
│       ├── models/                 # Модели данных
│       │   ├── organization.py     # Организация (tenant)
│       │   ├── user.py             # Пользователь
│       │   ├── role.py             # AI-агент (+ agent_type, tools, prompt_file)
│       │   ├── chat.py             # Чат
│       │   ├── message.py          # Сообщение и упоминания
│       │   ├── calendar_event.py   # Календарное событие
│       │   └── notification.py     # NotificationChannel, NotificationLog
│       │
│       ├── storage/                # PostgreSQL хранилище
│       │   ├── base.py             # Базовый класс с пулом
│       │   ├── org_storage.py
│       │   ├── user_storage.py
│       │   ├── role_storage.py
│       │   ├── chat_storage.py
│       │   ├── message_storage.py
│       │   ├── calendar_storage.py
│       │   ├── notification_channel_storage.py
│       │   └── notification_log_storage.py
│       │
│       ├── services/               # Бизнес-логика
│       │   ├── engine_service.py   # Композитный singleton
│       │   ├── org_service.py
│       │   ├── users_service.py
│       │   ├── roles_service.py    # Только чтение + кеш промптов
│       │   ├── chat_service.py
│       │   ├── mention_service.py
│       │   ├── ai_service.py       # Использует AgentExecutor
│       │   ├── prompt_cache.py     # In-memory кеш промптов из файлов
│       │   ├── calendar_service.py # Календарные события + croniter
│       │   ├── scheduler_service.py    # Фоновый polling + проактивность
│       │   └── notification_service.py # Оркестрация уведомлений
│       │
│       ├── routes/                 # API endpoints
│       │   ├── health.py           # Health checks
│       │   ├── auth.py             # Аутентификация
│       │   ├── organizations.py    # /api/v1/organizations
│       │   ├── users.py            # /api/v1/users
│       │   ├── roles.py            # /api/v1/roles (GET + admin cache)
│       │   ├── chats.py            # /api/v1/chats
│       │   ├── calendar.py         # /api/v1/calendar
│       │   └── notifications.py    # /api/v1/notifications
│       │
│       ├── agents/                 # Агентная система
│       │   ├── executor.py         # AgentExecutor
│       │   ├── result.py           # AgentResult
│       │   ├── graphs/             # simple, chain, multi_agent
│       │   └── tools/              # registry, calendar, rag, web, role_call
│       │
│       ├── notifications/          # Каналы доставки
│       │   ├── base_sender.py
│       │   ├── telegram_sender.py
│       │   └── email_sender.py
│       │
│       ├── prompts/                # Системные промпты (git-версионирование)
│       │   ├── lawyer.md
│       │   ├── accountant.md
│       │   ├── hr.md
│       │   └── chu.md
│       │
│       ├── llm/                    # LLM интеграция (legacy)
│       │   └── providers/
│       │       ├── base.py
│       │       └── ollama.py
│       │
│       └── migrations/
│           ├── 001_initial.sql
│           └── 002_role_evolution.sql
│
├── requirements.txt
├── setup.sh
├── migrate.sh
├── .env.example
└── .env
```

## API Endpoints

| Endpoint | Описание |
|----------|----------|
| `GET /health` | Health check |
| `POST /api/v1/auth/login` | Логин |
| `POST /api/v1/auth/register` | Регистрация |
| `GET /api/v1/users` | Список пользователей |
| `GET /api/v1/roles` | Список ролей (предсозданы, без CRUD) |
| `POST /api/v1/roles/admin/cache/prompts/clear` | Сброс кеша промптов |
| `GET /api/v1/chats/my` | Чаты пользователя |
| `GET /api/v1/chats/main` | Основной чат |
| `POST /api/v1/chats/direct` | Создать прямой чат |
| `POST /api/v1/chats/group` | Создать групповой чат |
| `GET /api/v1/chats/{id}/messages` | Сообщения чата |
| `POST /api/v1/chats/{id}/messages` | Отправить сообщение |
| `POST /api/v1/chats/messages/{id}/validate` | Валидировать AI-ответ |
| `GET /api/v1/calendar/events` | Список событий |
| `POST /api/v1/calendar/events` | Создать событие |
| `PATCH /api/v1/calendar/events/{id}` | Обновить событие |
| `DELETE /api/v1/calendar/events/{id}` | Деактивировать событие |
| `GET /api/v1/calendar/roles/{role_id}/events` | События роли |
| `GET /api/v1/notifications/channels` | Каналы уведомлений |
| `POST /api/v1/notifications/channels` | Регистрация канала |
| `DELETE /api/v1/notifications/channels/{type}` | Удалить канал |
| `POST /api/v1/notifications/channels/{type}/verify` | Подтвердить канал |
| `POST /api/v1/notifications/telegram/webhook` | Telegram webhook |
| `GET /api/v1/notifications/log` | Лог уведомлений |

## Flow сообщений с @@ упоминанием

```
1. WebClient: Пользователь пишет "@@lawyer проверь договор"
2. WebClient → Engine:
   POST /api/v1/chats/{chat_id}/messages
   {
     "content": "@@lawyer проверь договор"
   }
3. Engine: MentionService парсит @@ → находит lawyer
4. Engine: Сохраняет сообщение в PostgreSQL
5. Engine: Находит User с role_id → Role(lawyer)
6. Engine: AgentExecutor выбирает граф по agent_type:
   - simple без tools → прямой вызов ChatOllama
   - simple с tools → ReAct agent (LangGraph)
   - chain → последовательные шаги
   - multi_agent → StateGraph
7. Engine: PromptCache читает промпт из файла (или кеша)
8. Engine: Сохраняет AI-ответ (ai_validated=false)
9. Engine → WebClient: Response
10. WebClient: Показывает ответ AI
11. Пользователь (владелец роли) → может валидировать/редактировать
```

## Проактивный запуск агента (Scheduler)

```
1. SchedulerService каждые 30 сек: SELECT * FROM calendar_events
   WHERE is_active=true AND next_trigger_at <= NOW()
2. Для каждого события:
   a. mark_triggered() — инкремент trigger_count, пересчёт next_trigger_at
   b. Загрузка роли → AgentExecutor.execute() с контекстом события
   c. Агент генерирует уведомление
   d. NotificationService: отправка по каналам (Telegram → Email → fallback)
   e. Логирование в notification_log
```

## Система упоминаний

### @ упоминание (USER)
- Формат: `@username`
- Действие: Уведомляет пользователя
- Пример: `@ivan_petrov посмотри документ`

### @@ упоминание (AI_ROLE)
- Формат: `@@username`
- Действие: Вызывает AI-агента через AgentExecutor
- Пример: `@@lawyer проверь договор`
- Ответ AI требует валидации владельцем роли

## Типы чатов

1. **MAIN** — У каждого пользователя свой чат с его AI-ролью
2. **DIRECT** — Прямые сообщения между двумя пользователями
3. **GROUP** — Групповой чат (несколько участников)

## Kafka (планируется)

Kafka будет добавлена на стороне Engine для очередей запросов к GPU-серверу с LLM:

```
llm.requests     → GPU Server обрабатывает
llm.responses    ← Ответ возвращается в Engine
```

Подробнее см. `docs/llm.md`.

> **Примечание:** Kafka была убрана из WebClient. Она будет только на Engine.

## Порты

- **RuGPT Engine**: 8100 (не конфликтует с rptext 8000-8003)
- **PostgreSQL**: 5432 (база `rugpt`)
- **LLM (Ollama)**: 11434

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
