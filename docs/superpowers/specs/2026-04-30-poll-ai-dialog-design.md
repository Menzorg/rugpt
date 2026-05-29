# AI-диалог для утренних опросов сотрудников

**Дата:** 2026-04-30
**Статус:** Draft (на согласовании)
**Связанные документы:** `2026-04-14-pm-agent-and-kafka-bus-design.md` (Kafka bus item 10), `2026-04-13-pm-agent-design.md`

---

## 1. Цель

Заменить текущую форму утреннего опроса AI-диалогом. Сотрудник попадает в чат с AI-интервьюером, который просит рассказать про каждую активную задачу: статус, что изменилось, есть ли проблемы. После завершения сотрудником диалога второй AI собирает структурированную сводку, которую вечерний `report_generator` использует как input для отчёта руководителю.

Мотивация: люди плохо описывают своё состояние в форме (забывают детали, ставят галочки наугад). Диалог с AI вытаскивает контекст: уточняющие вопросы, переформулировки, выявление тревожных сигналов.

## 2. Scope

**Входит:**
- AI-интервьюер `poll_interviewer` ведёт диалог с сотрудником в специальном чате (тип `poll`)
- AI-сводчик `poll_summarizer` собирает markdown-сводку при нажатии "Завершить отчёт"
- Новый таб "Опрос" в главном чате для не-админов
- История прошлых опросов в виде дропдауна с переходом в read-only режим
- Все три LLM-сценария идут через Kafka `agent.requests` (без sync fallback)
- Существующий `report_generator` использует `poll.summary` как input

**НЕ входит:**
- Изменение запросов в `/api/tasks/*` (AI читает задачи только через snapshot в poll'е)
- AI не имеет права писать в `tasks.status` или другие поля задач
- Изменения для админского интерфейса (у них переключатель AI-агентов остаётся как был)
- Tools для AI (он работает только текстом, без вызова функций)

**Технический долг (фиксируется отдельной записью):**
- Выпил `KAFKA_ENABLED=false` режима из всего движка. Опросная фича пишется Kafka-only; параллельная задача — переписать существующие sync-ветки в `AIService` (`try_auto_respond`, `process_ai_mentions`).

## 3. Архитектура

```
┌─────────────────────────────────────────────────────────────────┐
│  Frontend (Next.js)                                              │
│  MainChat → tabs "Мой ИИ" / "Опрос"                              │
│  PollChat: useChatService(chat_id), кнопка "Завершить отчёт"     │
│  Dropdown истории прошлых опросов                                │
└────────────────────┬────────────────────────────────────────────┘
                     │ HTTP (signed) + WebSocket
                     ▼
┌─────────────────────────────────────────────────────────────────┐
│  WebClient backend (NestJS) — прозрачный proxy                   │
└────────────────────┬────────────────────────────────────────────┘
                     │
                     ▼
┌─────────────────────────────────────────────────────────────────┐
│  Engine                                                           │
│                                                                   │
│  Scheduler (8-10 утра org timezone):                             │
│    create_daily_poll → создать chat type='poll'                  │
│                      → publish kind='poll_initial'               │
│  Routes:                                                          │
│    POST /chats/{id}/messages  (диалог)                           │
│      → publish kind='message_reply'                              │
│    POST /task-polls/{id}/submit                                  │
│      → publish kind='poll_summary', 202 Accepted                 │
│                                                                   │
│  AgentRequestHandler (agent.requests):                           │
│    parse(payload) → kind ∈ {message_reply, poll_initial,         │
│                              poll_summary}                        │
│    CAS agent_runs pending→running                                │
│    switch kind → AIService.generate_*                            │
│    publish chat.events                                            │
│                                                                   │
│  TaskReportService (вечерний отчёт):                             │
│    использует poll.summary | raw transcript | "не пройден"       │
└─────────────────────────────────────────────────────────────────┘
```

**Принципы границ:**
- `AgentRequestHandler` — диспетчер, switch по `kind`, общая обвязка (CAS, mark_done, publish chat.events)
- `AIService` — три LLM-сценария: `generate_response` (существующий), `generate_poll_initial`, `generate_poll_summary`
- Kafka-only: новые методы не имеют sync fallback'ов
- Фронт — стандартный `useChatService` для poll-чата, никаких новых протоколов
- Webclient backend — прозрачный proxy, изменения только в типах TS

## 4. Компоненты

### 4.1. Миграция SQL `029_poll_chat_and_roles.sql`

**Расширение существующих таблиц:**

```sql
-- ChatType += 'poll'
ALTER TABLE chats DROP CONSTRAINT IF EXISTS chats_type_check;
ALTER TABLE chats ADD CONSTRAINT chats_type_check
  CHECK (type IN ('direct', 'task', 'project', 'support', 'poll'));

-- chats.poll_id (FK)
ALTER TABLE chats ADD COLUMN IF NOT EXISTS poll_id UUID
  REFERENCES task_polls(id) ON DELETE CASCADE;
CREATE INDEX IF NOT EXISTS idx_chats_poll
  ON chats(poll_id) WHERE poll_id IS NOT NULL;

-- task_polls.summary + task_ids snapshot
ALTER TABLE task_polls ADD COLUMN IF NOT EXISTS summary TEXT NULL;
ALTER TABLE task_polls ADD COLUMN IF NOT EXISTS task_ids JSONB
  NOT NULL DEFAULT '[]'::jsonb;
```

**Новые роли в системной org `00000000-...`:**
- `poll_interviewer` (`agent_type='simple'`, `tools=[]`, `prompt_file='poll_interviewer.md'`)
- `poll_summarizer` (`agent_type='simple'`, `tools=[]`, `prompt_file='poll_summarizer.md'`)

**Системный юзер:**
- `poll_interviewer_ai` (org=системная, role=poll_interviewer, is_system=true) — участник poll-чатов как "автор" AI-сообщений
- Под `poll_summarizer` юзер не нужен — он зовётся напрямую при submit, в чатах не участвует

### 4.2. Промпты

**`prompts/poll_interviewer.md`:**
- Роль: AI-интервьюер для утреннего опроса
- Получает в первом user-message: имя сотрудника + список активных задач (title, deadline)
- Цель: по каждой задаче выяснить статус, что изменилось, проблемы, нужна ли помощь
- Не давать советов
- Не настаивать если сотрудник сказал «всё»
- Стиль: вежливый, краткий, по делу

**`prompts/poll_summarizer.md`:**
- Роль: AI-сводчик
- Получает: транскрипт диалога интервьюера и сотрудника
- Цель: вытянуть структурированный markdown — по каждой задаче (статус по словам сотрудника, комментарий, тревожные сигналы)
- Не выдумывать факты — только то, что в транскрипте
- Если задача в диалоге не упоминалась — отметить "не обсуждалась"

### 4.3. Модели (Python dataclass)

**`models/chat.py`:**
- `ChatType` enum: добавить `POLL = "poll"`
- `Chat`: новое поле `poll_id: Optional[UUID] = None`

**`models/task_poll.py`:**
- Добавить `summary: Optional[str] = None`
- Добавить `task_ids: List[UUID] = field(default_factory=list)` (snapshot)

### 4.4. Storage

**`storage/chat_storage.py`:**
- `get_by_poll_id(poll_id) -> Optional[Chat]` — для idempotent создания
- SELECT/INSERT учитывают `poll_id`

**`storage/task_poll_storage.py`:**
- SELECT/INSERT учитывают `summary` и `task_ids`
- `update_summary(poll_id, summary)` — атомарный UPDATE
- `update_status` совместим с переводом в `'completed'` + установкой `completed_at`

### 4.5. ChatService

**Новый метод `create_poll_chat(poll_id, assignee_user_id) -> Chat`:**
- Идемпотентно: проверяет `chat_storage.get_by_poll_id`
- Создаёт chat type='poll', participants=[assignee, poll_interviewer_ai.id]
- Возвращает Chat

### 4.6. AIService — новые методы

```python
async def generate_poll_initial(
    self,
    poll_id: UUID,
    chat_id: UUID,
    responder_id: UUID,
) -> Optional[Message]:
    """Генерирует первое приветственное AI-сообщение в poll-чате.
    Читает task_ids snapshot, подгружает названия через task_storage.get_many_by_ids,
    зовёт agent_executor с ролью poll_interviewer, persist'ит AI-сообщение."""

async def generate_poll_summary(
    self,
    poll_id: UUID,
    chat_id: UUID,
    responder_id: UUID,
) -> Optional[Message]:
    """Загружает chat history, формирует transcript, зовёт agent_executor
    с ролью poll_summarizer. В одной транзакции пишет poll.summary +
    переводит в status='completed' + completed_at=now. Persist'ит
    системное сообщение 'Отчёт сдан, спасибо.'."""

async def _enqueue_poll_initial(self, poll_id: UUID, chat_id: UUID) -> None:
    """Создаёт agent_runs row + publish в agent.requests с kind='poll_initial'."""

async def _enqueue_poll_summary(self, poll_id: UUID, chat_id: UUID) -> None:
    """Создаёт agent_runs row + publish с kind='poll_summary'."""
```

### 4.7. AgentRequestHandler — расширение

Парсинг payload получает поле `kind: str = "message_reply"` (default для обратной совместимости). `user_message_id` становится опциональным для `kind != "message_reply"`.

После CAS — switch по `kind`:
- `message_reply` → `ai_service.generate_response(user_message, responder_id, ...)` (существующая логика как есть)
- `poll_initial` → `ai_service.generate_poll_initial(poll_id, chat_id, responder_id)`
- `poll_summary` → `ai_service.generate_poll_summary(poll_id, chat_id, responder_id)`
- `unknown kind` → лог ERROR, **return без raise** (poison message не делает retry)

Остальная обвязка (mark_done, publish chat.events) общая для всех веток.

### 4.8. TaskPollService

**`create_daily_poll(assignee_user_id, org_id)` (расширение):**
1. `tasks = task_storage.list_by_assignee(assignee_id, active_only=True)`
2. Если `len(tasks) == 0` — возвращает None (poll не создаётся)
3. Создаёт poll с `task_ids=[t.id for t in tasks]`, `status='pending'`
4. Idempotent создание chat: `chat_service.create_poll_chat(poll.id, assignee_id)`
5. `ai_service._enqueue_poll_initial(poll.id, chat.id)` — может бросить exception, ловится наверху
6. Создаёт in-app notification (`reference_type='task_poll'`, `reference_id=poll.id`)

### 4.9. TaskReportService — расширение

В `_build_llm_input` для каждого poll'а в org:
- Если `poll.summary` есть → блок input использует summary
- Иначе если `poll.status='expired'` и в чате есть user-сообщения → input содержит raw transcript из chat.messages
- Иначе → блок "Сотрудник не прошёл опрос"

### 4.10. Routes

**`POST /api/v1/task-polls/{id}/submit` (модификация):**
1. Валидация: poll exists, ownership (caller == assignee), status='pending'
2. Валидация: в `chat.messages` есть хотя бы одно сообщение от текущего юзера (assignee). Защита от пустого submit.
3. Идемпотентность: если последний agent_run для (chat_id, kind='poll_summary') в статусе `running` — возвращаем 409 (уже обрабатывается). Если failed — создаём новый request_id (до лимита 3 попыток на poll).
4. `ai_service._enqueue_poll_summary(poll.id, chat.id)`
5. Возвращает `202 Accepted`

**`GET /api/v1/task-polls/today/chat` (новый):**
- Резолвит `chat_id` для активного сегодняшнего poll'а текущего юзера
- Возвращает `{ chat_id, poll_id, status }` или 404

**`GET /api/v1/task-polls?include_completed=true&limit=30` (расширение):**
- Параметр `include_completed` контролирует выдачу истории

### 4.11. Frontend компоненты

**`MainChat.tsx`:**
- Tabs наверху: "Мой ИИ" / "Опрос"
- Tab "Опрос" виден только не-админам (`!user.isAdmin`)
- State выбранного таба синхронизирован с `?tab=poll` в URL

**`PollChat.tsx` (новый):**
- Hook `useTodayPoll()` fetch'ит `/api/task-polls/today/chat`
- Если активный poll — `useChatService(chat_id)` стандартный
- Шапка: "Утренний опрос за {date}", кнопка "Завершить отчёт"
- Кнопка disabled пока в чате нет хотя бы одного user-сообщения от текущего юзера
- Empty state когда `messages.length === 0` и poll active: спиннер "AI готовит сообщение..."
- Если нет today_poll или completed — empty state + дропдаун истории
- Read-only режим для прошлых опросов: `ChatInput` скрыт

**`NotificationDropdown.tsx`:**
- `notificationHref('task_poll')` → `/?tab=poll` вместо `/tasks`

### 4.12. WebClient backend (минимально)

**`packages/common/src/types/task-poll.ts`:**
- `TaskPoll`: добавить `summary?: string`, `taskIds?: string[]`, `chatId?: string`

**`packages/backend/src/engine/adapters/rugpt.adapter.ts`:**
- Команда `get_today_poll_chat` (резолв chat_id для активного poll'а)

**ChatType extension** в местах использования NestJS — добавить `'poll'`.

## 5. Data flow

### Сценарий A: утреннее создание poll'а и приветствие

1. Scheduler tick (8-10 утра org timezone) → `_run_morning_polls_for_org`
2. Для каждого assignee с активными задачами:
   - `create_daily_poll`: poll → chat type='poll' → publish `kind='poll_initial'` → in-app notification
3. KafkaConsumerLoop читает `agent.requests`, передаёт в AgentRequestHandler
4. Handler: CAS pending→running → `generate_poll_initial` → AI message persisted → publish chat.events
5. NestJS читает chat.events → WS broadcast → фронт получает приветствие, спиннер исчезает

При сбое LLM на шаге 4 — handler помечает agent_run failed и raise. Auto-retry механизм в SchedulerService на следующих 30s-tick'ах подхватит (см. раздел 6).

### Сценарий B: ответ юзера в диалоге

1. Юзер пишет в poll-chat → WS chat:send → NestJS → Engine `POST /chats/{id}/messages`
2. Engine: persist user message → `try_auto_respond` (существующая логика для DIRECT-чатов с system user; для type='poll' тот же путь)
3. Publish `kind='message_reply'` в Kafka → handler → `generate_response` → AI ответ
4. publish chat.events → WS broadcast

### Сценарий C: submit "Завершить отчёт"

1. Юзер жмёт кнопку → `POST /api/task-polls/{id}/submit`
2. Engine: валидация ownership + наличия user-сообщений → `_enqueue_poll_summary` → 202
3. Handler: `generate_poll_summary`:
   - Load chat messages → формат transcript
   - LLM (poll_summarizer) → markdown
   - Транзакция: `update_summary` + `update_status('completed', completed_at)`
   - Persist системного "Отчёт сдан, спасибо."
4. publish chat.events → фронт обновляет UI: poll completed, кнопка скрыта, чат read-only

### Сценарий D: expired poll вечером

1. `expire_stale_polls`: pending poll'ы старше cutoff → `status='expired'`. Summary не генерируется.
2. `evening_report_job` для каждого admin → `task_report_service.generate_report`
3. Для каждого poll: использовать `summary` (если completed) | raw transcript (если expired с диалогом) | "не пройден"
4. LLM (`report_generator`) собирает финальный отчёт руководителю

## 6. Error handling и idempotency

### Idempotency: agent_runs CAS

Каждый publish создаёт `agent_runs` row pending. Handler делает atomic `mark_running` (CAS pending→running). False — кто-то уже взял, skip.

Гарантия: **at-most-once на исполнение**, **at-least-once на доставку** (через Kafka redelivery до commit offset).

### Поведение при сбоях LLM

| kind | Поведение |
|---|---|
| `poll_initial` | Handler raise → Kafka retry skip'ает (status='failed'). **Auto-retry в скедулере**: каждый 30s tick проверяет poll'ы где status='pending' и messages_count==0; если суммарное число failed agent_runs за этот poll < 3 — публикует новый request_id. После 3 попыток — fallback шаблонное сообщение в чат + in-app notification "AI временно недоступен, начните диалог сами". |
| `message_reply` | Существующее поведение: handler raise → лог ERROR. Юзер может перепослать сообщение. |
| `poll_summary` | Endpoint `/submit` идемпотентен: повторное нажатие создаёт новый request_id если последний agent_run в статусе failed. Защита от спама: max 3 submit-попытки за поллём. Юзер на фронте видит ошибку через WS timeout (60s без chat.events) и может повторить. |

### Целостность транзакции poll_summary

```
result = agent_executor.execute(...)
if not result.content:
  raise RuntimeError('summarizer empty content')   # mark_failed → manual retry

async with task_poll_storage.transaction():
  await task_poll_storage.update_summary(poll_id, result.content)
  await task_poll_storage.update_status(poll_id, 'completed', completed_at=now)

# Системное сообщение "Отчёт сдан" — отдельно, не критично
try:
  await message_storage.create(...)
except Exception as e:
  logger.warning(...)
```

### Некорректный payload

Неизвестный `kind` или отсутствие обязательных полей (`poll_id` для poll_*, `user_message_id` для message_reply) — handler логирует ERROR и **возвращает без raise**, чтобы Kafka не зацикливалась на ядовитом сообщении.

### Сбой Kafka publish (брокер недоступен)

`_enqueue_poll_*` ловит исключение, помечает agent_run failed, raise.
- В скедулере `create_daily_poll`: исключение ловится, чат создан без приветствия. Auto-retry в следующих tick'ах подхватит.
- В route `submit`: возвращает 503, юзер повторяет.

### Логирование

- `mark_failed` → `logger.error(..., exc_info=True)` с контекстом (request_id, kind, poll_id)
- Превышение retry-лимита (3) → `logger.error` + in-app notification ассайни

## 7. Testing strategy

### Unit (моки storage/Kafka/AgentExecutor)
- AgentRequestHandler dispatch для каждого `kind`, malformed payload → graceful skip
- AIService.generate_poll_initial: empty task_ids edge, agent_executor exception → propagate
- AIService.generate_poll_summary: пустой transcript edge, LLM empty content → raise, transaction rollback
- TaskPollService.create_daily_poll: задач нет → None, idempotent на повторный вызов
- TaskReportService._build_llm_input: ветки summary / transcript / not-passed

### Integration (реальная PostgreSQL, моки LLM/Kafka)
- agent_runs CAS под параллельной нагрузкой (`asyncio.gather`)
- generate_poll_summary: транзакционная целостность (mock storage failure mid-transaction)
- create_daily_poll end-to-end: запись в task_polls + chats + in_app_notifications + agent_runs
- Auto-retry для poll_initial: симуляция 3-х последовательных failed → fallback message

### E2E (опционально, реальный Kafka KRaft)
- Полный путь scheduler → publish → consumer → handler → DB → chat.events

### Frontend (Jest + RTL)
- PollChat: спиннер при пустом poll, кнопка disabled без user-сообщений, read-only при completed
- MainChat tabs visibility (admin vs не-admin)
- `?tab=poll` в URL → активный tab

### Не покрываем тестами
- Контент промптов (это git-versioned markdown, не код)
- Качество AI-сводки (ручной просмотр + тонкий тюнинг промптов)

## 8. План реализации (по этапам)

Этапы независимы и могут исполняться последовательно с code review между ними.

1. **Миграция + модели + storage**: SQL `029`, расширение `Chat`/`TaskPoll`, методы storage. Покрывается миграционными тестами.
2. **Промпты + роли**: `poll_interviewer.md`, `poll_summarizer.md`, system user `poll_interviewer_ai`.
3. **AIService методы**: `generate_poll_initial`, `generate_poll_summary`, helpers `_enqueue_poll_*`. Unit тесты.
4. **AgentRequestHandler dispatch**: расширение payload, switch по kind. Unit тесты + integration на CAS.
5. **TaskPollService + ChatService**: `create_poll_chat`, обновление `create_daily_poll`. Integration тест end-to-end создания.
6. **Routes**: модификация `/submit`, новый `/today/chat`, расширение `?include_completed`. API тесты.
7. **Auto-retry в SchedulerService**: bounded retry для poll_initial. Integration тест.
8. **TaskReportService**: использование poll.summary в input. Регрессионный тест.
9. **WebClient backend**: типы + adapter command. TS компиляция + минимальные тесты.
10. **Frontend**: PollChat компонент, tabs в MainChat, hook `useTodayPoll`, history dropdown, NotificationDropdown href. Jest тесты.
11. **E2E (smoke)**: с docker-compose.kafka, проверка полного flow.

## 9. Открытые вопросы (для будущего)

- **AI-генерация финального отчёта руководителю** (`report_generator`) — уже на месте через миграцию 028. Использует `poll.summary` в input.
- **Удаление `KAFKA_ENABLED=false`** — отдельная задача в `tech-debt.md`, не входит в эту фичу.
- **Multi-tenant промпты** — роли `poll_interviewer/_summarizer` живут в системной org. Если будет потребность кастомизировать стиль интервьюера для конкретной org — отложено.
- **AI-инициированное завершение** — сейчас только юзер нажимает "Завершить". В будущем интервьюер может сам предложить завершить когда все задачи покрыты — отложено.
