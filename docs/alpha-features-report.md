# Отчёт по альфа-фичам RuGPT

Дата: 2026-03-09

---

## 1. Задачи (Tasks + Polls + Reports)

**Статус: ~90% engine, 100% webclient**

### Модель данных

3 таблицы (миграции 005, 006, 007):

- **tasks** — id, title, description, status (`created | in_progress | done | overdue`), assignee_user_id, deadline, org_id
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

- Scheduler jobs (`_morning_poll_job`, `_evening_report_job`) **не подключены** в SchedulerService
- AI-генерация текста отчётов — заглушка (plain text вместо LLM)
- Конфиги `TASK_MORNING_POLL_CRON`, `TASK_EVENING_REPORT_CRON`, `TASK_POLL_EXPIRE_HOURS` не в .env

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

**Статус: полностью реализовано (без RAG)**

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

- MinIO / S3 интеграция (S3StorageAdapter)
- RAG-индексация (`rag_search` — stub, `list_pending_indexing()` готов)
- Presigned URLs для прямого скачивания

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

## Сводная таблица готовности

| Фича | Engine | WebClient Backend | WebClient Frontend | Блокеры |
|-------|--------|-------------------|-------------------|---------|
| Задачи | 90% | 100% | 100% | Scheduler jobs не подключены |
| Колокольчик | 100% | 100% | 100% | Нет WebSocket push, нет навигации по клику |
| Файлы | 100% | 100% | 100% | RAG-индексация не реализована |
| Упоминания/Коррекция | 100% | 100% | 100% | RAG для правил не подключён |
