# Чат тех. поддержки RuGPT — дизайн

**Дата:** 2026-04-26
**Статус:** Утверждён к имплементации
**Тип:** Cross-cutting (Engine + WebClient)

---

## 1. Контекст и мотивация

Корпоративные клиенты RuGPT сталкиваются с двумя классами проблем:
1. Не знают как пользоваться продуктом (вопросы «как сделать X»).
2. Сталкиваются с багами / странным поведением.
3. Просят что-то ещё (запросы на фичу, организационные вопросы).

Сейчас канала обращений нет. Нужен чат тех. поддержки, доступный любому юзеру через иконку в сайдбаре, с явным разделением категорий обращений (юзер сам говорит про что пишет — не AI-классификатор), AI первой линии для типовых вопросов и эскалацией на живого оператора по необходимости.

Операторы — наша внутренняя команда, отдельная организация в системе.

---

## 2. Ключевые архитектурные решения

| Решение | Что выбрано | Альтернативы и почему отвергнуты |
|---|---|---|
| Семантика тикета | Отдельная сущность `support_ticket` (новая таблица + storage + сервис + routes) | `tasks.kind = 'support'` отвергнут — kind-aware ветки в task-коде, риск регрессии для внутренних задач, семантическое смешение «поручение» и «обращение клиента» |
| Орг операторов | Новая орг `RuGPT Support` (UUID `00000001-0000-0000-0000-000000000000`), отдельная от системной `RuGPT` (`00000000-...`) | Поместить операторов в системную RuGPT отвергнуто — смешение «системные сущности» и «команда саппорта», команда саппорта может расти и иметь отделы |
| Категории | Юзер выбирает явно из трёх: `how_to`, `bug`, `other` (не AI-классификатор) | AI-классификация на старте отвергнута — лишняя сложность, юзер сам быстрее выберет |
| AI первой линии | Срабатывает только для `how_to`. Soft handoff: после первого AI-ответа кнопки «Продолжить с ИИ» / «Позвать оператора». Шапка чата постоянно содержит «Позвать оператора» пока `ai_handoff_at IS NULL` | Hard handoff (юзер обязан выбрать) отвергнут — UX давление |
| Lifecycle | 3 статуса: `open` → `in_progress` → `closed`. Закрыть может любая сторона. Reopen — без отдельного route, через любое сообщение в чат в окне 7 дней после закрытия | 5-статусный аналог tasks отвергнут — `awaiting_review` создавал бы завалы зависших тикетов; жёсткий close без reopen отвергнут — теряется контекст, юзер плодит дубли |
| Cross-org | Чат тикета `org_id = requester_org_id` (орг клиента). Точечный exemption в фильтрах **строго** по `chat.type = 'support'` для четырёх мест в существующем коде | Универсальное снятие orgship-фильтра отвергнуто — лазейка для других типов чатов |
| Видимость | Имя оператора скрыто у клиента, рендерится «Тех. поддержка». Имя клиента у оператора видно через payload `GET /tickets/{id}` (без открытия общего `/users/` API) | Симметричное раскрытие профилей отвергнуто — лишняя поверхность атаки и не нужно функционально |
| Очередь | Self-assign первый кликнувший «Взять» через atomic SQL CAS. Без супервайзера / round-robin | Распределитель отвергнут на старте — операторов мало, очередь короткая |
| Уведомления операторам | In-app fan-out всем активным юзерам орг RuGPT Support через существующий `InAppNotificationService` + Kafka `chat.events` + WS | Telegram/Email отложены — операторы сидят в webclient'е |
| UI оператора | На `/tasks` — табы «Задачи RuGPT Support» / «Тикеты тех. поддержки». Очередь — отдельная страница `/support/queue`. Существующий task-код не трогаем | Слитный список отвергнут — оператор должен явно переключать контекст |

---

## 3. Схема БД

### 3.1 Миграция `019_support_tickets.sql`

```sql
-- Орг RuGPT Support
INSERT INTO organizations (id, name, slug, description) VALUES (
  '00000001-0000-0000-0000-000000000000',
  'RuGPT Support',
  'rugpt-support',
  'Команда тех. поддержки RuGPT'
) ON CONFLICT (id) DO NOTHING;

-- Роль + system user — через PL/pgSQL DO-блок (паттерн миграции 003).
-- Idempotent через ON CONFLICT.
DO $$
DECLARE
  role_support_ai_id UUID;
BEGIN
  INSERT INTO roles (org_id, name, code, description, system_prompt, model_name, agent_type, tools, prompt_file, is_active)
  VALUES (
    '00000000-0000-0000-0000-000000000000',
    'Support Assistant',
    'support_assistant',
    'AI первая линия тех. поддержки RuGPT',
    'Вы — AI-ассистент тех. поддержки RuGPT. Отвечаете кратко по вопросам использования продукта.',
    'hosted_vllm/google/gemma-4-31B-it',
    'simple',
    '[]'::jsonb,
    'support_assistant.md',
    true
  )
  ON CONFLICT (org_id, code) DO UPDATE SET prompt_file = EXCLUDED.prompt_file
  RETURNING id INTO role_support_ai_id;

  INSERT INTO users (org_id, username, name, email, is_system, role_id, is_active)
  VALUES (
    '00000000-0000-0000-0000-000000000000',
    'support_ai',
    'AI Support',
    'ai-support@rugpt.system',
    true,
    role_support_ai_id,
    true
  )
  ON CONFLICT (org_id, username) DO UPDATE SET role_id = EXCLUDED.role_id;
END $$;

-- Расширение ChatType
ALTER TABLE chats DROP CONSTRAINT IF EXISTS chats_type_check;
ALTER TABLE chats ADD CONSTRAINT chats_type_check
  CHECK (type IN ('direct', 'task', 'project', 'support'));

ALTER TABLE chats ADD COLUMN IF NOT EXISTS support_ticket_id UUID;
CREATE INDEX IF NOT EXISTS idx_chats_support_ticket ON chats(support_ticket_id)
  WHERE support_ticket_id IS NOT NULL;

-- Основная таблица (idempotent)
CREATE TABLE IF NOT EXISTS support_tickets (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),

  requester_user_id UUID NOT NULL REFERENCES users(id),
  requester_org_id  UUID NOT NULL REFERENCES organizations(id),

  category VARCHAR(20) NOT NULL CHECK (category IN ('how_to', 'bug', 'other')),
  status   VARCHAR(20) NOT NULL DEFAULT 'open'
    CHECK (status IN ('open', 'in_progress', 'closed')),

  assignee_user_id UUID REFERENCES users(id),

  ai_handoff_at        TIMESTAMP,
  ai_first_response_at TIMESTAMP,

  closed_at         TIMESTAMP,
  closed_by_user_id UUID REFERENCES users(id),
  closed_by_role    VARCHAR(20) CHECK (closed_by_role IN ('requester', 'operator')),

  title VARCHAR(200),

  created_at TIMESTAMP NOT NULL DEFAULT NOW(),
  updated_at TIMESTAMP NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_support_tickets_requester ON support_tickets(requester_user_id, status);
CREATE INDEX IF NOT EXISTS idx_support_tickets_assignee  ON support_tickets(assignee_user_id, status)
  WHERE assignee_user_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_support_tickets_queue     ON support_tickets(created_at)
  WHERE status = 'open' AND assignee_user_id IS NULL;
CREATE INDEX IF NOT EXISTS idx_support_tickets_closed    ON support_tickets(closed_at)
  WHERE status = 'closed';

-- Audit trail (idempotent)
CREATE TABLE IF NOT EXISTS support_ticket_events (
  id         UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  ticket_id  UUID NOT NULL REFERENCES support_tickets(id) ON DELETE CASCADE,
  actor_user_id UUID NOT NULL REFERENCES users(id),
  actor_role VARCHAR(20) NOT NULL
    CHECK (actor_role IN ('requester', 'operator', 'ai', 'system')),
  event_type VARCHAR(30) NOT NULL,
  -- created | ai_responded | ai_handoff | taken | closed | reopened | message
  payload    JSONB NOT NULL DEFAULT '{}'::jsonb,
  created_at TIMESTAMP NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_support_ticket_events_ticket
  ON support_ticket_events(ticket_id, created_at DESC);
```

### 3.2 Инварианты

- На один `support_ticket` ровно одна строка в `chats` с `support_ticket_id = ticket.id` и `type = 'support'`.
- `chat.org_id = ticket.requester_org_id` (орг клиента).
- `messages.org_id = chat.org_id = requester_org_id` всегда — даже сообщения от оператора. Идентификация авторства через `sender_id`, не через org.
- `chat.participants` для `how_to` без handoff: `[requester_user_id, support_ai_user_id]`. После handoff добавляется `assignee_user_id`. Для `bug` / `other`: `[requester_user_id]`, после `take` — добавляется `assignee_user_id`.
- При закрытии `participants` не меняются — только `status`.

### 3.3 Конфиг

- `Config.SUPPORT_REOPEN_WINDOW_DAYS = 7` — константа, без UI настройки.
- `Config.RUGPT_SUPPORT_ORG_ID = '00000001-0000-0000-0000-000000000000'` — константа.

---

## 4. API

### 4.1 Engine routes (`/api/v1/support/*`)

**Клиент:**

| Endpoint | Описание |
|---|---|
| `POST /tickets` | Создать тикет. Body: `{ category, initial_message }`. Создаёт ticket + chat + первое сообщение. Для `how_to` — добавляет `support_ai` в participants и триггерит `try_auto_respond`. Для `bug`/`other` — `ai_handoff_at = now()` сразу + `SupportNotificationService.notify_new_in_queue`. Возвращает `{ ticket, chat_id }` |
| `GET /tickets/my` | Свои тикеты, query `?status=&limit=` |
| `GET /tickets/{id}` | Деталь. Доступ: requester либо assignee либо любой оператор RuGPT Support (для очереди). Для оператора в payload встроены `requester_name`, `requester_email`, `requester_org_name`, `requester_org_id` |
| `POST /tickets/{id}/escalate` | «Позвать оператора» в чате `how_to`. Ставит `ai_handoff_at = now()`, шлёт `notify_new_in_queue` |
| `POST /tickets/{id}/close` | Закрыть. Доступ: requester или assignee. Внутри определяет `closed_by_role` по принадлежности юзера к requester или RuGPT Support орг |

**Оператор:**

| Endpoint | Описание |
|---|---|
| `GET /queue` | `WHERE status='open' AND assignee_user_id IS NULL` ORDER BY `created_at`. Доступ только юзерам орг RuGPT Support |
| `POST /tickets/{id}/take` | Atomic CAS: `UPDATE support_tickets SET assignee_user_id=$1, status='in_progress' WHERE id=$2 AND assignee_user_id IS NULL RETURNING *`. Если NULL — 409 Conflict. Добавляет оператора в `chat.participants`. Записывает event в `support_ticket_events`. In-app уведомление клиенту |
| `GET /operator/my` | Assigned оператору, `status='in_progress'`. Используется во вкладке `/tasks` |

**Общие:**

| Endpoint | Описание |
|---|---|
| `GET /tickets/{id}/chat` | Резолв `chat_id` тикета |
| `GET /tickets/{id}/events` | Audit trail |

### 4.2 Reopen — без route

Hook в `ChatService.add_message`: новое сообщение в `ChatType.SUPPORT` чате при `support_ticket.status='closed'`:
- Если `closed_at + Config.SUPPORT_REOPEN_WINDOW_DAYS > now()`: тикет переоткрывается (status → `in_progress`, `closed_at = NULL`, event `'reopened'` в audit trail, in-app уведомление другой стороне). Сообщение пишется как обычно.
- Иначе: `403 Forbidden`. Фронт показывает баннер «Тикет архивирован, создать новый?».

### 4.3 WebClient proxy (`/api/support/*`)

`SupportModule` (NestJS) — стандартный proxy-паттерн как `ChatModule`/`UserModule`. Команды в `RuGPTEngineAdapter`: `support_create_ticket`, `support_get_my_tickets`, `support_get_ticket`, `support_escalate`, `support_close`, `support_queue`, `support_take`, `support_operator_my`, `support_get_chat`, `support_get_events`. Контроллер `SupportController` с `SignatureGuard` + `JwtAuthGuard`.

---

## 5. Cross-org exemptions

**Защитное требование:** все exemption-проверки **строго** `chat.type == ChatType.SUPPORT`. Никаких более общих критериев. Тесты обязаны покрывать обратное: попытка cross-org доступа к `direct`/`task`/`project` чату даёт 403, даже если злоумышленник находится в `participants`.

### 5.1 Точки правки

1. **`ChatStorage` (already correct)** — no orgship filters at storage level. Verified by regression tests in `tests/test_chat_storage_support_cross_org.py`. If anyone later adds `WHERE org_id = ...` to `list_by_user` or `get_by_id`, those tests will catch it.
2. **`ChatService.can_user_access_chat(user, chat)`** — добавить ветку: `chat.type == 'support'` → доступ если `user.id IN chat.participants` ИЛИ (`user.org_id == RUGPT_SUPPORT_ORG_ID` AND `chat.support_ticket_id IS NOT NULL`). Последнее — для оператора, кликающего тикет из `/queue` до взятия.
3. **`MessageStorage` (already correct)** — таблица `messages` (миграция 001) не имеет колонки `org_id`; ни один метод (`create`, `list_by_chat`, `get_by_id`, `list_pending_review`, etc.) не использует orgship в WHERE. Orgship-чек живёт исключительно в `ChatService.can_user_access_chat`, выше по стеку. Поведение зафиксировано регрессионными тестами в `tests/test_message_storage_support_cross_org.py` — если кто-то добавит `WHERE org_id = ...` или JOIN на `chats.org_id`, тесты упадут.
4. **`SocketGateway.handleChatJoin` (NestJS) — already correct.** Уже использует только participant-check (`chat.participants.includes(userId)`), без orgship-фильтра. Cross-org для SUPPORT работает естественно — оператор RuGPT Support после `take` добавляется в `chat.participants` через `chat_storage.add_participant` (Task 10), после чего может join'нуться в WS-комнату. Никаких code-изменений. Поведение проверяется E2E-сценарием (Task 32 — оператор берёт тикет, видит live-сообщения клиента из чужой орг).

### 5.2 Что **не** трогаем

- **`MentionService`** — cross-org `@`-упоминания осознанно не работают. Клиент не упоминает оператора по имени, оператор клиента тоже. Это не баг, а граница системы.
- **`UserStorage.get_by_id` / `get_by_username`** — общий API остаётся orgship-bound. Cross-org профиль клиента оператор получает встроенным в `GET /tickets/{id}`. Имя оператора клиенту никогда не отдаётся (фронт подменяет на «Тех. поддержка»).

---

## 6. AI первой линии

### 6.1 Системный юзер `support_ai`

- Орг: системная RuGPT (`00000000-...`).
- Роль: `support_assistant`, prompt-файл `src/engine/prompts/support_assistant.md`, `agent_type='simple'`, без tools.
- Модель: `gemma-4-31B-it` через LiteLLM (как остальные system users).

### 6.2 Промпт `support_assistant.md`

Инструкции:
- Отвечать кратко, по делу, про возможности RuGPT (как создать чат, использовать `@@`-упоминания, посмотреть задачи и т.д.).
- Если вопрос не про использование продукта — рекомендовать позвать оператора через кнопку в шапке чата.
- Не выдавать информацию про инфраструктуру / админ-функции / другие организации.

### 6.3 Триггеры AI-ответа

Используется существующий механизм `try_auto_respond` (тот, что работает в admin DIRECT-чатах с system users). Срабатывает на каждое юзер-сообщение в `ChatType.SUPPORT` чате при условиях:

1. `support_ticket.category == 'how_to'`
2. `support_ticket.ai_handoff_at IS NULL`
3. `support_ai_user_id IN chat.participants`

При первом AI-ответе в чате — ставится `support_ticket.ai_first_response_at = now()`. Фронт по этому полю рендерит блок «Продолжить с ИИ / Позвать оператора» под первым AI-сообщением.

### 6.4 Эскалация

- Юзер жмёт «Позвать оператора» → `POST /support/tickets/{id}/escalate` → `ai_handoff_at = now()`. После этого `try_auto_respond` молча скипает (условие 2). AI остаётся в `participants` для атрибуции старых сообщений.
- Юзер жмёт «Продолжить с ИИ» → ничего не вызывается на бекенде, фронт скрывает блок. Следующее юзер-сообщение снова триггерит AI.

### 6.5 Async режим

AI-ответ идёт через `agent.requests` → `chat.events` → WS. Прозрачно, тот же поток что для `@@`-mentions. Никаких особенностей.

---

## 7. Уведомления

### 7.1 `SupportNotificationService` — операторам

Fan-out на всех активных юзеров орг RuGPT Support.

**Триггеры:**
- Создание тикета `bug` / `other` → новый тикет в очереди.
- `escalate` тикета `how_to` → новый тикет в очереди.

**Реализация:** реюз существующего `InAppNotificationService` с новым типом `support_ticket_new`. Live-доставка через Kafka `chat.events` → NestJS `KafkaConsumerService` → WS broadcast в комнаты `user:<operator_id>` каждому оператору. Колокольчик в `NotificationDropdown` показывает «Новый тикет в очереди — категория `<category>`».

### 7.2 Уведомления клиенту

Status changes тикета (взял / закрыл / переоткрыл) **не пишутся как сообщения в ленту чата**. У клиента в ленте только реальные сообщения (от себя, AI, оператора — последние два подписаны «Тех. поддержка»). Статус — отдельная плоскость:

1. **Audit trail** — записывается в `support_ticket_events` (взял / закрыл / переоткрыл).
2. **WS push** — сервер шлёт WS event с обновлённым тикетом (статусом). Фронт перерисовывает шапку чата (`Открыт` / `В работе` / `Закрыт`) + баннер reopen-окна.
3. **In-app колокольчик** через `InAppNotificationService`:
   - `support_ticket_taken` — клиенту, когда оператор взял.
   - `support_ticket_closed` — клиенту, когда оператор закрыл.
   - `support_ticket_reopened` — assignee оператору, когда клиент переоткрыл сообщением в окне.

Доставка WS push и колокольчика — через Kafka `chat.events` (re-use existing infrastructure).

---

## 8. Frontend

### 8.1 Sidebar

В `packages/frontend/src/app/components/Sidebar.tsx`:
- Новая иконка «Тех. поддержка» (`HeadsetIcon` — уже существует) под кнопкой «Персональный ИИ», над разделителем перед группами юзеров.
- Click → `/support`.
- Бейдж непрочитанных через `useSupportUnread` хук.

### 8.2 Новые страницы

**`app/support/page.tsx`** — лендинг:
- Шапка: переключатель категорий (3 кнопки `how_to` / `bug` / `other`) + поле ввода первого сообщения. Submit → `POST /api/support/tickets` → `router.push('/chat/support/' + ticket.id)`.
- Ниже: «Мои обращения» — список через `useSupportTickets`. Активные открытые сверху, закрытые в окне reopen — серым ниже, архивные — отдельной свернутой секцией.

**`app/chat/support/[id]/page.tsx`** — чат тикета:
- Использует тот же `Chat` компонент что `/chat/task/[id]` через props-адаптер `chatType='support'`.
- Шапка: статус тикета, категория, дата создания. Для `closed`: если `closed_at + 7 дней > now()` — баннер с датой когда окно закроется, input активен; если окно истекло — баннер «Тикет архивирован, создать новый?», input disabled.
- Под первым AI-сообщением (когда `ai_first_response_at IS NOT NULL && ai_handoff_at IS NULL`) — блок soft-handoff с двумя кнопками.
- В шапке постоянная иконка/меню «Позвать оператора» (пока `ai_handoff_at IS NULL`) и «Закрыть тикет» (для активных).
- Имена оператора подменяются на «Тех. поддержка». Фильтр: `chat.type === 'support' && отправитель сообщения принадлежит орг RuGPT Support` (проверка по `senderOrgId === RUGPT_SUPPORT_ORG_ID`, идентификатор берётся из payload сообщения). AI-ответы от `support_ai` остаются с обычным именем «AI Support» — это системный юзер из орг RuGPT, не из RuGPT Support, под фильтр не попадает.

**`app/support/queue/page.tsx`** — очередь, **только** для юзеров орг RuGPT Support (guard через `useUser()`). Список unassigned тикетов, кнопка «Взять» в каждой строке.

### 8.3 Изменения существующего

- **`app/tasks/page.tsx`** — для оператора (юзеров RuGPT Support) рендерим табы «Задачи RuGPT Support» / «Тикеты тех. поддержки». Без изменения существующей логики `/tasks` для остальных юзеров — гард `if user.orgId === RUGPT_SUPPORT_ORG_ID`.
- **`useSidebarTaskProjectChats`** остаётся неприкосновенным. Support-чаты у клиента/оператора отображаются через свою иконку и страницу, не лезут в task/project секции сайдбара.

### 8.4 Новые хуки

- `useSupportTickets` — свои тикеты клиента.
- `useSupportQueue` — очередь оператора.
- `useSupportOperatorMy` — assigned оператору, для таба на `/tasks`.
- `useSupportUnread` — бейдж сайдбара.

---

## 9. UX flows

### 9.1 Клиент создаёт тикет (`how_to`)

1. Иконка → `/support` → выбирает «Что-то непонятно» → пишет «как переслать сообщение в чат?» → submit.
2. Создаётся тикет + чат + первое сообщение → редирект в `/chat/support/{id}`.
3. AI отвечает (через несколько секунд, async через Kafka). Под ответом блок «Продолжить с ИИ / Позвать оператора».
4. Юзер пишет следующий вопрос (или жмёт «Продолжить») → AI снова отвечает. Цикл пока юзер не позовёт оператора или не закроет тикет.

### 9.2 Эскалация

1. Юзер жмёт «Позвать оператора» (либо в блоке после первого AI-ответа, либо в шапке потом).
2. Тикет → в очередь, AI отключается, операторы получают in-app уведомление.

### 9.3 Клиент создаёт тикет (`bug` / `other`)

1. Лендинг → выбирает «Что-то не работает» → пишет → submit.
2. Тикет сразу `ai_handoff_at = now()`, в очереди. AI не подключается.
3. В чате системное сообщение «Тикет создан, ожидает оператора».

### 9.4 Оператор берёт

1. Сидит на `/support/queue` или получает live-уведомление через WS.
2. Жмёт «Взять» → CAS → тикет переезжает в его `/tasks` таб «Тикеты тех. поддержки».
3. Системное сообщение в чате клиента «Оператор взял тикет в работу».
4. В чате тикета у оператора — обычный chat UI с заголовком тикета, инфой о клиенте (имя, орг).

### 9.5 Закрытие

1. Любая сторона жмёт «Закрыть тикет» → подтверждение → `status='closed'`, системное сообщение «<кто-то> закрыл тикет», над input баннер с датой окна reopen.

### 9.6 Reopen через сообщение

1. В окне 7 дней клиент пишет в чат → сообщение проходит, тикет автоматически переоткрывается, системное сообщение «Тикет переоткрыт», assignee получает уведомление.
2. После окна — фронт отказывается отправлять (input disabled, баннер «архив»). Если злоумышленник всё-таки отправит запрос напрямую — Engine возвращает 403.

---

## 10. Миграции и порядок раскатки

### 10.1 Engine

1. Миграция `019_support_tickets.sql` — таблицы, орг RuGPT Support, `support_ai` юзер, роль `support_assistant`, расширение `chats.type` enum, поле `chats.support_ticket_id`.
2. Промпт-файл `src/engine/prompts/support_assistant.md` — коммит вместе с миграцией.
3. Backend-код: модели → storage → сервисы → routes → подключение в `EngineService` и `app.py`.
4. `Config.SUPPORT_REOPEN_WINDOW_DAYS = 7`, `Config.RUGPT_SUPPORT_ORG_ID` — константы в `config.py` / `.env`.
5. Cross-org exemptions точечно в существующих storage/service методах + регрессионные тесты на отсутствие лазейки в других типах чатов.

### 10.2 WebClient

1. `SupportModule` (NestJS) с командами в адаптере, контроллер.
2. WS — проверка orgship-чека в `SocketGateway.joinChat`, exempt при `chat.type='support'`.
3. Frontend — иконка в Sidebar, страницы `/support`, `/support/queue`, `/chat/support/[id]`, табы на `/tasks`, новые хуки.
4. Локальная проверка end-to-end: создать тикет (все три категории) → AI ответил для `how_to` → escalate → оператор из тестового RuGPT Support аккаунта взял → закрыл → reopen через сообщение в окне.

---

## 11. Тесты — обязательный минимум

### 11.1 Engine

- `support_ticket_storage` — CRUD + atomic CAS на `take` (concurrent через `asyncio.gather` с 3 одновременными вызовами → exactly one wins, остальные получают NULL).
- `support_ticket_service.create` — все три категории, проверка `ai_handoff_at` поведения.
- `support_ticket_service.escalate` — корректно ставит `ai_handoff_at`, AI после этого не отвечает.
- `support_ticket_service.close` — обе стороны могут закрыть, `closed_by_role` корректный.
- `chat_service.add_message` — reopen через сообщение в окне, 403 после окна.
- **Cross-org регрессии:** три теста — попытка юзера из чужой орг открыть `direct` / `task` / `project` чат через тот же путь что `support` → 403. Покрывает все non-support `ChatType`.
- AI-handoff: после `escalate` `try_auto_respond` для этого чата возвращает no-op.

### 11.2 WebClient

- `SupportController` — все routes отвечают корректно, signature/JWT работают.
- WS — оператор RuGPT Support подключается к support-чату клиента из чужой орг → join успешен.
- Frontend — Playwright или ручная проверка golden path всех 6 UX flows.

---

## 12. Out of scope (не делаем на старте)

- Telegram / Email уведомления операторам.
- Приоритет / SLA / теги тикета.
- Супервайзер / round-robin распределение.
- Web-форма bug-репорта с auto-prefill (URL, скрин, версия браузера).
- Метрики (среднее время первого ответа, % AI-резолвов, conversion ai → handoff).
- Multi-level саппорт (L1/L2 эскалация).

Эти пункты — отдельные follow-up разговоры, не блокируют MVP.

---

## 13. Открытые вопросы

Нет. Все архитектурные развилки закрыты в разделе 2.
