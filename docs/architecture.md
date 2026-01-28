# RuGPT Engine Architecture

## Обзор

**RuGPT** — корпоративный AI-ассистент с ролевой системой и multi-tenancy.

**Engine** отвечает за:
- Управление организациями, пользователями, ролями
- Хранение чатов и сообщений
- Систему упоминаний (@ и @@)
- Генерацию AI-ответов через LLM
- Аутентификацию (JWT)

**WebClient** отвечает за:
- Real-time (WebSocket)
- UI

## Архитектура

```
┌─────────────────────────────────────┐
│           WebClient                  │
│  (NestJS + Next.js)                  │
│                                      │
│  При @@ mention:                     │
│  └─ POST /api/v1/chats/...          │
│                                      │
└───────────────────┬──────────────────┘
                    │ HTTP
                    ▼
┌───────────────────────────────────────┐
│           Engine (FastAPI)            │
│                                       │
│  PostgreSQL:                          │
│  ├─ organizations                     │
│  ├─ users (org_id, role_id)           │
│  ├─ roles (system_prompt, model)      │
│  ├─ chats                             │
│  └─ messages                          │
│                                       │
│  LLM (Ollama/vLLM):                   │
│  └─ Генерация AI-ответов              │
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
│       │   ├── role.py             # AI-роль
│       │   ├── chat.py             # Чат
│       │   └── message.py          # Сообщение и упоминания
│       │
│       ├── storage/                # PostgreSQL хранилище
│       │   ├── base.py             # Базовый класс с пулом
│       │   ├── org_storage.py      # CRUD организаций
│       │   ├── user_storage.py     # CRUD пользователей
│       │   ├── role_storage.py     # CRUD ролей
│       │   ├── chat_storage.py     # CRUD чатов
│       │   └── message_storage.py  # CRUD сообщений
│       │
│       ├── services/               # Бизнес-логика
│       │   ├── engine_service.py   # Композитный сервис
│       │   ├── org_service.py      # Логика организаций
│       │   ├── users_service.py    # Логика пользователей
│       │   ├── roles_service.py    # Логика ролей
│       │   ├── chat_service.py     # Логика чатов и сообщений
│       │   └── mention_service.py  # Парсинг @ и @@ упоминаний
│       │
│       ├── routes/                 # API endpoints
│       │   ├── health.py           # Health checks
│       │   ├── auth.py             # Аутентификация
│       │   ├── organizations.py    # /api/v1/organizations
│       │   ├── users.py            # /api/v1/users
│       │   ├── roles.py            # /api/v1/roles
│       │   └── chats.py            # /api/v1/chats
│       │
│       ├── llm/                    # LLM интеграция
│       │   └── providers/
│       │       ├── base.py         # Базовый провайдер
│       │       └── ollama.py       # Ollama/vLLM
│       │
│       └── migrations/             # Миграции БД
│           └── 001_initial.sql
│
├── requirements.txt
├── setup.sh                        # Скрипт установки
├── migrate.sh                      # Скрипт миграций
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
| `GET /api/v1/roles` | Список ролей |
| `GET /api/v1/chats/my` | Чаты пользователя |
| `GET /api/v1/chats/main` | Основной чат |
| `POST /api/v1/chats/direct` | Создать прямой чат |
| `POST /api/v1/chats/group` | Создать групповой чат |
| `GET /api/v1/chats/{id}/messages` | Сообщения чата |
| `POST /api/v1/chats/{id}/messages` | Отправить сообщение |
| `POST /api/v1/chats/messages/{id}/validate` | Валидировать AI-ответ |

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
6. Engine: Role(lawyer) → system_prompt → LLM
7. Engine: Сохраняет AI-ответ (ai_validated=false)
8. Engine → WebClient: Response
9. WebClient: Показывает ответ AI
10. Пользователь (владелец роли) → может валидировать/редактировать
```

## Система упоминаний

### @ упоминание (USER)
- Формат: `@username`
- Действие: Уведомляет пользователя
- Пример: `@ivan_petrov посмотри документ`

### @@ упоминание (AI_ROLE)
- Формат: `@@username`
- Действие: Вызывает AI-роль пользователя
- Пример: `@@lawyer проверь договор`
- Ответ AI требует валидации владельцем роли

## Типы чатов

1. **MAIN** — У каждого пользователя свой чат с его AI-ролью
2. **DIRECT** — Прямые сообщения между двумя пользователями
3. **GROUP** — Групповой чат (несколько участников)

## Порты

- **RuGPT Engine**: 8100 (не конфликтует с rptext 8000-8003)
- **PostgreSQL**: 5432 (база `rugpt`)
- **LLM (Ollama)**: 11434

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
