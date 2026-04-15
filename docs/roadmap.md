# RuGPT — Роадмап проекта

> Документ для согласования с владельцем продукта.
> Дата: 4 марта 2026
> Обновлено: 9 апреля 2026

---

## Видение продукта

**RuGPT** — корпоративный AI-ассистент, который автоматизирует внутренние процессы компании: контроль задач сотрудников, работа с документами через AI-роли, уведомления и отчётность. Долгосрочно — гибрид ERP + AI, минимизирующий человеческий фактор.

**Лидмагнит (ключевое предложение):** руководитель получает из коробки AI-систему, которая ставит задачи сотрудникам, контролирует их выполнение, и каждый вечер даёт сводный отчёт. Понятная ценность для любого руководителя.

---

## Alpha (цель: конец апреля 2026)

Минимальный продукт, готовый к тестированию на реальных пользователях.

### 1. Управление задачами сотрудников -- РЕАЛИЗОВАНО

Ключевая функция Alpha. AI контролирует задачи и разгружает руководителя.

**Реализовано в Engine:**
- Модели: Task, TaskPoll, TaskReport (миграции 005-007)
- API: /tasks/*, /task-polls/*, /task-reports/*
- AI-инструменты: task_create, task_query, task_update
- SchedulerService: утренние опросы (часы 8-10), вечерние отчёты (18-20), per-org timezone
- In-app уведомления при создании задачи, опросе, отчёте, просрочке
- Контроль дедлайнов: автоматическая пометка overdue

**Статусы задач (текущие):** created, in_progress, done, overdue.

**Что ещё не сделано (вынесено в пункты 9-12):**
- `created_by_user_id` на задаче — нужно для приоритизации (п.9)
- Поле `project` для группировки (п.11)
- Промежуточный статус `awaiting_review` (исполнитель сделал, ждёт приёмки создателя)
- AI-генерация текста вечерних отчётов
- Исполнитель не должен сам закрывать задачу — только создатель

### 2. RAG -- работа с документами -- РЕАЛИЗОВАНО

AI-роли получают доступ к загруженным документам и могут ими оперировать.

**Реализовано в Engine:**
- FileService: загрузка, хранение (LocalStorageAdapter), дедупликация по SHA-256
- RAGService: индексация через Apache Tika + OllamaEmbeddings + pgvector
- Два типа: текстовые документы (чанки) и табличные (построчные эмбеддинги)
- Гибридный поиск: vector + full-text (TSV rank fusion), 7 SQL-функций
- IngestQueue: фоновая индексация (ThreadPoolExecutor, 3 воркера)
- rag_search LangChain tool: двухуровневый поиск (docs -> chunks)
- API: /files/*, /rag/*
- Изоляция по org_id + user_id (is_public для общих файлов)
- Миграции: 009 (user_files), 012 (pgvector schema), 013 (search functions)

Подробнее: `docs/rag-info.md`

### 3. Центр оповещений (колокольчик) -- РЕАЛИЗОВАНО

**Реализовано в Engine:**
- InAppNotificationService + InAppNotificationStorage (миграция 008)
- API: /in-app-notifications/* (список, unread-count, mark read, mark all read)
- Типы: new_task, poll, report, mention, task_status_change, system
- reference_type + reference_id для перехода к источнику

### 4. Мониторинг инфраструктуры -- НЕ РЕАЛИЗОВАНО

Инфраструктура усложняется -- нужна видимость состояния серверов и сервисов.
- Мониторинг состояния серверов (CPU, RAM, диск, GPU)
- Оповещения при проблемах
- Дашборд состояния сервисов

> В Engine есть только базовые /health /health/ready /health/live без метрик ресурсов.

### 5. Система очередей -- НЕ РЕАЛИЗОВАНО

Асинхронная обработка запросов к LLM через очереди (Kafka):
- Engine не блокируется на ожидании ответа от LLM
- Масштабирование: несколько GPU-воркеров
- Основа для потоковой отдачи ответов (streaming)

> Kafka не реализована. LLM-вызовы синхронные (ChatOllama.invoke).

### 6. Три агента на главной странице -- НЕ РЕАЛИЗОВАНО

Три специализированных агента, доступных всем пользователям с главной страницы:
- **Размышлятор** -- агент для рассуждений, анализа, цепочки мыслей
- **Поиск по документам** -- поиск по загруженным файлам организации (rag_search tool готов)
- **Поиск по интернету** -- веб-поиск (web_search -- stub)

> Отдельных "агентов на главной" нет. rag_search работает как инструмент ролей.

### 7. Подбор LLM-модели -- В ПРОЦЕССЕ

Тестирование и выбор одной оптимальной модели для продукта:
- Текущая основная: `qwen2.5:7b` (Ollama)
- Эмбеддинги: `qwen3-embedding:0.6b`
- Выполняет: Александр

### 8. Отделы и права видимости -- РЕАЛИЗОВАНО

Организационная структура внутри организации.

**Реализовано в Engine:**
- Миграция 015: departments, department_visibility, user.department_id, user.is_head, org.org_context
- DepartmentService: CRUD отделов, назначение руководителей, правила видимости
- Центральный метод get_visible_user_ids() — единая точка фильтрации
- Видимость встроена в: users, chats, tasks, roles, mentions, task_tool (AI-агенты)
- org_context: текстовое поле + загрузка через Tika → инъекция в system prompt всех ролей
- API: /departments/* (10 эндпоинтов, admin-only)
- Login возвращает department_id и is_head

**Реализовано в WebClient (2026-04-13):**
- Общие типы Department/DepartmentVisibility, User.departmentId/isHead
- Engine adapter: 10 команд отделов + update_organization + upload_org_context + department_id/is_head в user CRUD
- NestJS DepartmentModule (controller+service, admin-guarded)
- User service/controller проброс departmentId
- Страница `/departments` с тремя табами:
  - Отделы: CRUD + назначение руководителя через expand-row dropdown
  - Правила видимости: аккордеон с симметричными чекбоксами
  - Контекст организации: textarea + загрузка файла через Tika
- `/users`: колонка "Отдел" + dropdown назначения в модалке
- Sidebar: группировка по отделам со сворачиванием (localStorage), скрытие системных (AI) пользователей, ★ для руководителей, секция "Без отдела" внизу
- Settings: пункт "Отделы и видимость" (admin-only)
- Ссылка: `/root/webclient_rugpt/doc/superpowers/plans/2026-04-13-departments-ui.md`

**Требуется ручное тестирование (Task 12 плана):**
- Поднять backend + frontend локально
- Проверить CRUD отделов, назначение руководителя
- Проверить симметричность правил видимости
- Сохранить текст и загрузить файл в "Контексте организации"
- Назначить пользователя в отдел через /users, проверить колонку
- Проверить группировку в сайдбаре, сворачивание, сохранение состояния
- Под non-admin: страница `/departments` недоступна, пункт в Settings скрыт

**Отделы (Departments):**
- Группировка пользователей внутри организации
- Связка user<->department с флагом `is_head` (руководитель отдела)
- Контекст организации для AI-ролей: текстовое поле или загружаемый текстовый документ с описанием структуры (при интеграции с клиентом). Текст извлекается и добавляется в system prompt всех ролей организации

**Права видимости:**
- Единая модель: если человек не виден -- нельзя писать, упоминать, ставить задачи
- Правила на уровне отдел -> отдел (какие отделы видят друг друга)
- Сотрудник видит: свой отдел + отделы с открытым доступом по правилам
- Руководитель отдела (is_head) видит: то же + все другие руководители + владелец организации
- Владелец организации (is_admin) видит: всех
- Без персональных overrides — только правила на отделы. Проще, нет мусора от забытых исключений
- Отделы плоские (без вложенности)


### 9. Приоритизация и владение задачами

Расширение базовой системы задач (п.1): кто владеет статусом, кто меняет дедлайн, как сортировать.

**Поля на задаче (миграция):**
- `created_by_user_id uuid REFERENCES users(id)` — кто поставил
- `awaiting_review_at timestamptz` — когда исполнитель отметил готово
- `proposed_deadline timestamptz` — предложенный исполнителем срок
- `proposed_deadline_by uuid` — кто предложил

**Расширение статусов:**
`created → in_progress → awaiting_review → done` (плюс `overdue`)

**Кто что может:**
| Действие | Кто может |
|---|---|
| Создать | любой видящий исполнителя |
| `created → in_progress` | исполнитель (взял в работу) |
| `in_progress → awaiting_review` | исполнитель (отметил готово) |
| `awaiting_review → done` | **только создатель** (принял) |
| `awaiting_review → in_progress` | **только создатель** (вернул в работу) |
| Менять дедлайн напрямую | **только создатель** |
| Предлагать новый дедлайн | исполнитель (записывается в `proposed_deadline`) |
| Принять/отклонить предложение | создатель |
| Переназначить, отменить, перенести в другой проект | **только создатель** (+ admin) |

Исполнитель никогда сам не закрывает задачу — только создатель.

**Приоритет по источнику:**
- Вычисляется по роли создателя: `is_admin > is_head > обычный` (3/2/1)
- Не хранится в БД — считается на бэке при выдаче списка

**API:**
- `GET /api/v1/tasks/my` — мои (как исполнитель), сортировка по приоритету источника, затем по дедлайну
- `GET /api/v1/tasks/created-by-me` — поставленные мной
- `PATCH /api/v1/tasks/{id}/deadline` — создатель меняет
- `POST /api/v1/tasks/{id}/deadline-proposal` — исполнитель предлагает
- `POST /api/v1/tasks/{id}/deadline-proposal/accept` — создатель принимает
- `POST /api/v1/tasks/{id}/deadline-proposal/reject` — создатель отклоняет

**WebClient:**
- `/tasks` — две вкладки: "Мои задачи" и "Поставленные мной"
- Бейджи приоритета (красный/жёлтый/серый по роли создателя)
- В карточке задачи: кнопка "Изменить срок" (создатель) / "Предложить другой срок" (исполнитель)
- В карточке: кнопки "Принять" / "Вернуть в работу" для `awaiting_review`

### 10. Агент "Проджект-менеджер" (PM) + Kafka event bus -- РЕАЛИЗОВАНО (требуется ручная верификация)

PM-агент как личный уведомитель по задачам + **фундамент асинхронного
инференса через Kafka**. П.10 в итоге закрыл сразу три задачи: PM-агент,
часть п.5 (очереди к нейронкам через Kafka), и решил архитектурный пробел
«проактивная доставка сообщений от Engine в WS клиентам».

Подробная спецификация: `docs/superpowers/specs/2026-04-14-pm-agent-and-kafka-bus-design.md`.

**Что реализовано (engine + webclient, 2026-04-14):**

**Kafka инфраструктура:**
- Apache Kafka 3.7.0 (Apache License 2.0), KRaft mode, single-node в Docker
- `docker-compose.kafka.yml` + `scripts/kafka_up.sh`/`kafka_down.sh`/`kafka_init.sh`
- Два топика с разной retention: `agent.requests` (24h, внутренняя очередь
  инференса), `chat.events` (1h, доставка сообщений в WS)
- `aiokafka` (Engine, Python), `kafkajs` (WebClient, TypeScript)
- `Config.KAFKA_BOOTSTRAP_SERVERS` + `KAFKA_ENABLED` флаг — весь Kafka-код
  работает как no-op когда выключен, тесты без Kafka продолжают работать

**PM агент:**
- Миграция `018_pm_role_and_agent_runs.sql`: роль `pm` в системной org +
  system user `pm` (идемпотентно через `ON CONFLICT DO NOTHING`)
- `src/engine/prompts/pm.md` — system prompt для conversational режима
- `TaskNotificationService` — 9 методов (`notify_take`, `notify_mark_done`,
  `notify_accept`, `notify_reject`, `notify_set_deadline`,
  `notify_propose_deadline`, `notify_accept_proposed_deadline`,
  `notify_reject_proposed_deadline`, `notify_overdue`)
- Сообщения идут **от лица PM system user** (`sender_type=ai_role`,
  `ai_is_valid=true`) в lazy-созданный direct-chat между PM и получателем
- Правило адресации: уведомляется сторона, которая **не инициировала**
  изменение (assignee-действия → creator, creator-действия → assignee,
  scheduler/overdue → обе стороны)
- Hook-integration в `TaskService` через helper `_notify(method, ...)`,
  вызывается после `_record_event` во всех методах перехода +
  `check_overdue`
- Deep-link в тексте уведомления: `/chat/task/<uuid>` (страница из п.11,
  вместо изначально планировавшегося `/tasks#<id>`)
- **НЕ реализовано** (намеренно отброшено в дизайне):
  - Интерактивные кнопки «Принять» / «Вернуть» / «Отклонить» в сообщениях
    PM — юзер идёт в `/chat/task/<id>` для действий
  - `acting_on_behalf_of_user_id` поле и `POST /api/v1/messages/{id}/action`
    эндпоинт

**Kafka event bus (фундамент для async доставки):**
- `KafkaProducerService` — aiokafka с `enable_idempotence=True`, JSON
  сериализация, UUID/datetime поддержка
- `KafkaConsumerLoop` — background asyncio task, at-least-once с manual
  commit; при exception offset не коммитится → Kafka redeliver
- `chat.events` topic: publisher в Engine (TaskNotificationService +
  AgentRequestHandler), consumer в NestJS (`KafkaConsumerService` →
  `SocketGateway.broadcastToChat(chatId, msg)` → `io.to('chat:<id>').emit(...)`)
- Broadcast pattern: каждый NestJS instance в своей consumer group
  (`webclient-<hostname>-<pid>`), все instance'ы получают все события

**Асинхронный агентный инференс (начало п.5):**
- Таблица `agent_runs(request_id PK, status, ...)` для идемпотентности при
  Kafka redelivery. Атомарный CAS `mark_running` через
  `UPDATE WHERE status='pending' RETURNING`
- `AIService` — два режима:
  - Async (Kafka enabled): `process_ai_mentions` и `try_auto_respond`
    публикуют в `agent.requests`, возвращают пустой `ai_responses`
  - Sync fallback: старый синхронный путь через `generate_response()`,
    тесты существующего контракта не ломаются
- `AgentRequestHandler` — callable для KafkaConsumerLoop:
  1. Atomic CAS `pending → running`, если уже running/done/failed — skip
  2. Load user message by id
  3. Вызов `AIService.generate_response()` — существующая синхронная
     логика (context build, LangGraph ReAct loop, tools, persistence)
  4. `mark_done` + publish в `chat.events`
  5. При exception: `mark_failed`, re-raise чтобы offset не коммитился
- Kafka consumer для `agent.requests` — background task в том же uvicorn
  (через `asyncio.create_task` в `EngineService.initialize`)
- HTTP response `POST /chats/{id}/messages` получил новое поле
  `agent_pending: bool` — для typing indicator на фронте

**NestJS (webclient):**
- `KafkaModule` + `KafkaConsumerService` с подпиской на `chat.events`
- `SocketGateway.broadcastToChat(chatId, msg)` — новый public метод
- `ChatService.saveMessage` возвращает `agentPending?: boolean`
- Зарегистрирован в `AppModule`

**Тесты:**
- Engine: **131 passed** (109 до п.10 + 22 новых)
  - `test_task_notifications.py` — 12 тестов TaskNotificationService с моками
  - `test_agent_runs_idempotency.py` — 5 тестов атомарного CAS на реальной БД
    (including concurrent `asyncio.gather` → exactly-one-wins)
  - `test_ai_service_async.py` — 9 тестов async/sync branching
  - `test_agent_request_handler.py` — 6 тестов consumer handler
  - `tests/integration/test_kafka_end_to_end.py` — 2 e2e теста с живым
    Kafka (PM notification доставляется в `chat.events`, `@@mention` →
    publish в `agent.requests` + agent_run row создаётся)
- Backend NestJS: `tsc --noEmit` exit 0

**Отклонения от исходного плана роадмапа (обоснованные):**
- Исходный план подразумевал полу-синхронный PM (event-hook в
  `TaskService.update()` → прямой `message_storage.create`). Реализовано
  через Kafka event bus — потому что без него проактивные сообщения из
  Engine не долетают до подключённых WS-клиентов (см. обсуждение в дизайн-
  доке, P2 из investigation).
- Интерактивные кнопки в сообщениях PM отброшены. Юзер переходит в
  `/chat/task/<id>` по ссылке в тексте.
- Deep-link `/tasks#<id>` заменён на `/chat/task/<id>` (страница из п.11).

**Разблокировано:**
- **п.12 (команды агентам)** — вся инфраструктура event bus + async
  агенты уже работает. Остаётся добавить tools `chat_post`,
  `project_chat_post`, `summary_*`, `notify_*` в PM role и расширить
  prompt
- **п.5 (очереди к нейронкам)** — частично закрыто: `agent.requests` как
  очередь задач для inference. Масштабирование через `gunicorn --workers N`
  — Kafka consumer group сама распределит partition'ы
- Любая будущая проактивная коммуникация Engine→фронт (scheduler алерты,
  batch отчёты) — пользуется тем же `chat.events` каналом

**Оставшееся (follow-up):**
- Ручная верификация в браузере под живым Engine+NestJS+фронт
- Typing indicator на фронте через `agent_pending` (сейчас работает
  через regexp `/@@\w+/`, функционально эквивалентно)
- Cleanup politics для `agent_runs` старше 30 дней (scheduler job)
- Dead letter queue для хронически упавших runs
- Раскатка Kafka на prod-машину Engine (5090/Proxmox) + open порт `9092`
  на WireGuard VPN интерфейс для NestJS консьюмера

### 11. Проекты (группировка задач) и чаты задач -- РЕАЛИЗОВАНО (требуется ручная верификация)

Группировка задач + автоматические чаты для обсуждения, audit trail и
кликабельные ссылки `!<task>` / `!!<project>` в сообщениях.

Подробная спецификация: `docs/superpowers/specs/2026-04-13-projects-and-task-chats-design.md`.

**Что реализовано (engine + webclient, 2026-04-13/14):**

- **Миграция `017_projects_and_task_chats.sql`** — таблицы `projects`, `task_events`,
  колонки `tasks.project_id`, `chats.task_id`, `chats.project_id`; legacy `main`/`group`
  типы конвертированы в `direct`, default `type='direct'`; partial-индексы
  `idx_projects_org`, `idx_tasks_project`, `idx_chats_task`, `idx_chats_project`,
  `idx_task_events_task(task_id, created_at DESC)`.
- **Отдельная таблица проектов** (вместо изначально планировавшегося `tasks.project text`):
  дубликаты имён в одной org разрешены, soft-delete через `is_active`, `created_by_user_id`
  для аудита.
- **Audit trail** — таблица `task_events` + `TaskEventService`. Каждый переход статуса
  (`created`, `took`, `marked_done`, `accepted`, `rejected`, `deadline_set`,
  `deadline_proposed`, `deadline_proposal_accepted|rejected`, `assignee_changed`,
  `project_changed`, `cancelled`, `overdue`) пишется отдельным событием.
  **В чаты задач/проектов системные сообщения НЕ пишутся** — всё в `task_events`.
- **Auto-создание чатов**:
  - `TaskService.create()` → `ChatService.create_task_chat()` (участники: creator + assignee,
    chat.type=TASK, chat.task_id).
  - Если передан `project_id` → `ensure_project_chat_membership()` лениво создаёт project-chat
    или мерджит участников в существующий. Идемпотентно, реактивирует архивированный чат
    (решает race с `archive_project_chat`).
  - `TaskService.update()` при смене assignee → `add_task_chat_participant`, при смене
    `project_id` → `ensure_project_chat_membership` в новом проекте. Старый project-chat
    остаётся как наблюдатель (монотонный рост participants — осознано).
  - `TaskService.deactivate()` → `archive_task_chat`; если последняя активная задача в
    проекте — `archive_project_chat`.
- **Чистка group-чатов** — `ChatType.GROUP`, `ChatService.create_group_chat`, роут
  `POST /api/v1/chats/group`, `CreateGroupChatRequest`, frontend `isGroup`, NestJS
  `createGroupChat` — всё удалено. Legacy строки в БД сконвертированы.
- **Ссылки `!<task-uuid>` / `!!<project-uuid>`** — параллельная подсистема к `@`/`@@`.
  `ReferenceService` парсит regex `!!(UUID)|!(UUID)`, `resolve_batch` делает per-viewer
  проверку через `can_see_task` (creator / assignee / same-org admin) и проверку
  `org_id`/`is_active` для проектов. Результат ездит в response сообщения как поле
  `references: [{type, id, title, accessible, position}]`. Недоступные — `accessible=false`,
  `title=null`, кросс-org не утекает.
- **Права**: только `is_head || is_admin` могут создавать/редактировать/удалять проекты;
  multi-tenancy по `org_id` на всех уровнях (service + route), кросс-org сущности
  скрываются как 404.
- **Engine API**:
  - `/projects` CRUD + `/projects/{id}/chat`;
  - `/tasks/{id}/chat`, `/tasks/{id}/events`;
  - `POST/PATCH /tasks` принимают `project_id`, `PATCH` поддерживает отдельный
    `detach_project` для явного сброса;
  - `/tasks/my` и `/tasks/created-by-me` принимают `?project_id`;
  - `/chats/my?type=direct|task|project` серверный фильтр;
  - `/chats/{id}/messages` возвращает `references` per-viewer.
- **WebClient (NestJS proxy)**: адаптер дополнен командами `get_projects`, `get_project`,
  `create_project`, `update_project`, `delete_project`, `get_project_chat`, `get_task_chat`,
  `get_task_events`; новый `ProjectModule` (controller + service); `TaskService.listMy`/
  `listCreatedByMe` принимают `projectId?`, `getChat`, `getEvents`; `TaskController` —
  новые endpoints `:id/chat`, `:id/events`; common types обновлены (`ChatType.TASK/PROJECT`,
  `ReferenceType`, `MessageReference`, `Task.projectId`, новые `Project`, `TaskEvent`).
- **Frontend (Next.js)**: новые роуты `/chat/task/[id]`, `/chat/project/[id]` с
  pinned card + HTTP-polling чатом (WS-интеграция — follow-up); `Sidebar` — секции
  «Задачи» и «Проекты» через `useSidebarTaskProjectChats`; `ChatInput` — `detectReference`
  autocomplete для `!`/`!!` с клавиатурной навигацией; `MessageBubble` — рендеринг
  pills через `references`, серые pills для недоступных; форма создания задачи —
  dropdown проектов + inline «+ создать проект» (head/admin); кнопка «Чат» в строке
  задачи → `/chat/task/{id}`; история задачи через `<details>` в pinned card.
- **Покрытие тестами (engine pytest)**: 68 новых тестов в 8 файлах (`test_projects`,
  `test_task_events`, `test_task_chat_auto_create`, `test_project_chat_integration`,
  `test_multitenancy_guards`, `test_references`, `test_sidebar_filter`,
  `test_migration_017`). Весь suite: 97 passed.

**Отклонения от исходного плана роадмапа (обоснованные):**
- Отдельная таблица `projects` вместо `tasks.project text` — позволяет soft-delete,
  хранить description и created_by_user_id, мигрировать переименование без обновления
  всех задач.
- Ссылки через `!<uuid>` / `!!<uuid>` вместо `@task:<id>`/`@task:"имя"`/`@project:"имя"` —
  не переиспользуют `MentionService` (избегаем type confusion в `Mention.user_id`),
  UI вставляет UUID через autocomplete, пользователь печатает `!`/`!!` и выбирает.
- Audit trail как отдельная таблица `task_events` (выбрано из двух опций, указанных
  в плане).

**Оставшееся (follow-up):**
- Полноценная WebSocket-интеграция для task/project chat pages (сейчас HTTP polling после
  send — работает, но без live-апдейта от других участников).
- Колонка «Проект» в tasks-table (сейчас фильтр только через sidebar).
- Отдельная страница «Архив задач» для доступа к закрытым.
- Ручная верификация в браузере (см. ниже).

### 12. Команды агентам

Общий механизм: пользователь обращается к AI-агенту на естественном языке, агент выполняет действия в системе через инструменты.

**Примеры:**
- "@@pm напиши в чат задачи `Отчёт по Альфа` что срок переносится на пятницу"
- "@@pm дай суммаризацию как дела в проекте `Alpha релиз`"
- "@@pm покажи мои просроченные задачи"
- "@@pm создай задачу @ivan: подготовить КП до 25 апреля, проект Альфа"
- "@@pm сообщи всем в отделе маркетинга что встреча в 15:00"

**Архитектура:**
- ReAct loop в LangGraph с набором tools, `max_iterations=5`
- PM-агент (п.10) — основная точка входа, но команды доступны любой роли с подходящими tools
- Сообщения от агента в чатах задач/проектов идут **от его имени** (роль `pm`), с подписью "по команде @<user>" в теле и `acting_on_behalf_of_user_id` в метаданных
- Проверка прав: агент проверяет видимость целевых сущностей через `engineToken` инициатора — нельзя писать туда, куда инициатор не имеет доступа

**Новые tools:**

*Постинг сообщений (`chat_tool.py`):*
- `chat_post(chat_id, text)` — базовый, общий
- `task_chat_post(task_id_or_name, text)` — fuzzy-поиск задачи, обёртка
- `project_chat_post(project_name, text)` — обёртка
- `dept_chat_post(dept_id_or_name, text)` — для группового чата отдела

*Суммаризация (`summary_tool.py`):*
- `chat_summary(chat_id, since_hours=24)` — последние сообщения → LLM-сводка
- `project_summary(project_name)` — статусы задач + highlights чатов проекта
- `tasks_summary(filter)` — фильтр `my|created|overdue|department`

*Поиск (`search_tool.py`):*
- `task_query(...)` — расширить существующий: by_project, by_deadline, by_status
- `user_search(query)` — поиск по имени/отделу через `get_visible_user_ids()`

*Уведомления (`notify_tool.py`):*
- `notify_users(user_ids, text)` — in-app уведомление списку (только admin/head)
- `notify_department(dept_id, text)` — всем видимым в отделе (только admin/head этого отдела)

**Безопасность:**
- Все деструктивные действия (удаление, массовая рассылка) требуют явного подтверждения от пользователя в следующем сообщении
- Весь chain-of-tools логируется через correlation_id
- max_iterations=5 предотвращает бесконечные циклы

**UI слэш-шорткаты:**
- В инпуте чата кнопки `/задача`, `/проект`, `/дедлайн`, `/упоминание`
- Клик вставляет шаблон команды в поле ввода: `/задача` → `@@pm создай задачу: ____ исполнитель: @____ срок: ____`
- Не отдельная механика, просто префиллы для команд агенту

**Зависимости:**
- п.8 (отделы) ✓
- п.9 (приоритизация + `created_by_user_id`) — нужен для `tasks_summary`
- п.10 (PM-агент)
- п.11 (проекты + чаты задач) — нужны для `task_chat_post`/`project_chat_post`

### Реализовано вне роадмапа

Следующие фичи были реализованы, но не описаны в исходном роадмапе:

- **Zero Trust / устройства** -- миграция 011, ECDSA P-256 верификация устройств, CryptoService
- **Correction Rules** -- миграция 010, система обучения AI на обратной связи (отклонённые ответы -> правила)
- **Organization timezone** -- миграция 014, per-org timezone для scheduler jobs
- **6 системных промптов** -- lawyer, accountant, hr, chu, admin_assistant, humorist

---

## Что НЕ входит в Alpha

| Направление | Когда |
|-------------|-------|
| Нагрузочное тестирование | Full version |
| Правки интерфейсов | Full version |
| Мобильное приложение | Full version |
| Пользовательские роли (кастомные) | Full version |
| Интеграция с внешними системами (1С и др.) | Будущие апселлы |

---

## Full Version (после Alpha)

На основе результатов тестирования Alpha на реальных пользователях.

### Расширение работы с документами
- Больше форматов (Excel, таблицы, изображения)
- Продвинутая интерпретация документов
- Верификация пользователем извлечённых данных
- Список сформированных правил из документов

### Оптимизация производительности
- Результаты нагрузочного тестирования → оптимизация
- Масштабирование под реальную нагрузку

### Пользовательские роли
- Возможность пользователям создавать свои AI-роли
- Тестовый режим в Alpha (наблюдаем какие роли создают) → оформление в Full version
- Шаблоны ролей для типовых задач

### Кастомизация дизайна
- Брендирование под клиента
- Настраиваемые темы

### Мобильное приложение
- Опросы, задачи, чат, отчёты — с мобильного устройства
- Push-уведомления

### Интеграция с внешними системами (будущие апселлы)
- 1С, CRM, ERP-системы
- API-инструменты для агентов (контроль остатков, заказы и т.д.)
- Каждая интеграция = новый инструмент для AI-роли

---

## Стратегическое направление

```
Alpha                           Full Version                 Будущее
-----                           ------------                 -------
Задачи + опросы + отчёты [done] Пользоват. роли             ERP-гибрид
RAG (PDF, DOCX) [done]          Расширенные документы        Интеграции (1С, CRM)
Колокольчик [done]              Оптимизация                  Автономные агенты
Отделы + права видимости [done] Мобильное приложение         Автоматизация процессов
Приоритизация и владение задач  Кастом дизайн
PM-агент + уведомления
Проекты + чаты задач
Команды агентам
Подбор модели [in progress]
```

**Текущая цель:** выкатить Alpha, тестировать на реальных людях, понять точное направление.

**Первый тестовый клиент:** согласован ресторан (детали после Alpha).
