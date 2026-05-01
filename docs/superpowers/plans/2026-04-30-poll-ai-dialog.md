# Poll AI Dialog Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace static morning poll forms with AI-driven dialog. AI interviewer pulls task status from employee, AI summarizer writes markdown summary used by evening report_generator.

**Architecture:** New `ChatType='poll'` with two new system roles (`poll_interviewer`, `poll_summarizer`). All three LLM scenarios (initial greeting, dialog reply, final summary) flow through Kafka `agent.requests` with extended payload field `kind`. Extended `AgentRequestHandler` dispatches by `kind` to appropriate `AIService` method. Frontend gets new "Опрос" tab in `MainChat` for non-admins.

**Tech Stack:** Python 3.10+ / FastAPI / asyncpg / LangChain+LangGraph / Apache Kafka / TypeScript / NestJS / Next.js 15 / React 19

**Spec:** `docs/superpowers/specs/2026-04-30-poll-ai-dialog-design.md`

---

## File Structure

### Engine (Python — `/root/rugpt/`)

**New files:**
- `src/engine/migrations/029_poll_chat_and_roles.sql` — DB migration
- `src/engine/prompts/poll_interviewer.md` — interviewer system prompt
- `src/engine/prompts/poll_summarizer.md` — summarizer system prompt
- `tests/test_migration_029.py` — migration smoke test
- `tests/test_ai_service_poll.py` — unit tests for new AIService methods
- `tests/test_agent_handler_poll_dispatch.py` — kind dispatch tests
- `tests/test_task_poll_service_chat.py` — create_daily_poll integration
- `tests/test_task_report_uses_summary.py` — report uses summary

**Modified files:**
- `src/engine/models/chat.py` — add `POLL` to ChatType, add `poll_id` to Chat
- `src/engine/models/task_poll.py` — add `summary`, `task_ids` fields
- `src/engine/storage/chat_storage.py` — handle `poll_id`, add `get_by_poll_id`
- `src/engine/storage/task_poll_storage.py` — handle `summary`/`task_ids`, add `update_summary`
- `src/engine/services/ai_service.py` — add `generate_poll_initial`, `generate_poll_summary`, `_enqueue_poll_initial`, `_enqueue_poll_summary`
- `src/engine/services/chat_service.py` — add `create_poll_chat`
- `src/engine/services/task_poll_service.py` — update `create_daily_poll` to create chat + enqueue
- `src/engine/services/task_report_service.py` — use `poll.summary` in input
- `src/engine/services/scheduler_service.py` — auto-retry for stuck `poll_initial`
- `src/engine/services/engine_service.py` — wire new system user resolution for poll_interviewer_ai
- `src/engine/kafka/agent_handler.py` — extend payload parse, dispatch by `kind`
- `src/engine/routes/task_polls.py` — modify `submit`, add `today/chat`, extend list filter

### WebClient backend (TypeScript — `/root/webclient_rugpt/packages/backend/`)

**Modified files:**
- `packages/common/src/types/task-poll.ts` — add `summary`, `taskIds`, `chatId` fields
- `packages/common/src/types/chat.ts` — extend ChatType with `'poll'`
- `packages/backend/src/engine/adapters/rugpt.adapter.ts` — add `get_today_poll_chat` command
- `packages/backend/src/task-poll/task-poll.service.ts` — add `getTodayChat` method
- `packages/backend/src/task-poll/task-poll.controller.ts` — add `GET /today/chat`

### Frontend (TypeScript — `/root/webclient_rugpt/packages/frontend/`)

**New files:**
- `src/app/components/PollChat.tsx` — poll dialog tab content
- `src/app/hooks/useTodayPoll.ts` — fetch today's poll chat resolution

**Modified files:**
- `src/app/components/MainChat.tsx` — add tabs "Мой ИИ" / "Опрос"
- `src/app/components/NotificationDropdown.tsx` — `task_poll` href → `/?tab=poll`

### Documentation

**New files:**
- Append to `docs/tech-debt.md` — "Remove KAFKA_ENABLED=false fallback mode" entry

---

## Task 1: Migration 029 — schema + roles + system user

**Files:**
- Create: `src/engine/migrations/029_poll_chat_and_roles.sql`
- Create: `tests/test_migration_029.py`

- [ ] **Step 1: Write the failing migration smoke test**

```python
# tests/test_migration_029.py
"""Smoke test for migration 029: poll chat type, poll_id FK, summary field, two roles, system user."""
import asyncpg
import pytest

from src.engine.config import Config


@pytest.mark.asyncio
async def test_migration_029_creates_schema_and_roles():
    conn = await asyncpg.connect(Config.get_postgres_dsn())
    try:
        # ChatType extension
        constraint = await conn.fetchval("""
            SELECT pg_get_constraintdef(oid) FROM pg_constraint
            WHERE conname = 'chats_type_check'
        """)
        assert constraint and "'poll'" in constraint, f"chats_type_check missing 'poll': {constraint}"

        # chats.poll_id column + index
        col_exists = await conn.fetchval("""
            SELECT 1 FROM information_schema.columns
            WHERE table_name='chats' AND column_name='poll_id'
        """)
        assert col_exists == 1

        idx_exists = await conn.fetchval("""
            SELECT 1 FROM pg_indexes
            WHERE indexname='idx_chats_poll'
        """)
        assert idx_exists == 1

        # task_polls.summary + task_ids
        for col in ("summary", "task_ids"):
            exists = await conn.fetchval(
                "SELECT 1 FROM information_schema.columns "
                "WHERE table_name='task_polls' AND column_name=$1",
                col,
            )
            assert exists == 1, f"task_polls.{col} missing"

        # Two roles
        for code in ("poll_interviewer", "poll_summarizer"):
            role_exists = await conn.fetchval(
                "SELECT 1 FROM roles WHERE org_id='00000000-0000-0000-0000-000000000000'::uuid "
                "AND code=$1",
                code,
            )
            assert role_exists == 1, f"role {code} missing"

        # System user poll_interviewer_ai
        user_exists = await conn.fetchval(
            "SELECT 1 FROM users WHERE org_id='00000000-0000-0000-0000-000000000000'::uuid "
            "AND username='poll_interviewer_ai' AND is_system=true"
        )
        assert user_exists == 1, "system user poll_interviewer_ai missing"
    finally:
        await conn.close()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /root/rugpt && venv/bin/pytest tests/test_migration_029.py -v`
Expected: FAIL with assertion error (constraint missing 'poll', or column not found).

- [ ] **Step 3: Write the migration SQL**

Create `src/engine/migrations/029_poll_chat_and_roles.sql`:

```sql
-- Migration 029: poll chat type, poll-scoped chat, summary field, AI dialog roles.
-- Idempotent.

-- 1. Extend ChatType to include 'poll'
ALTER TABLE chats DROP CONSTRAINT IF EXISTS chats_type_check;
ALTER TABLE chats ADD CONSTRAINT chats_type_check
  CHECK (type IN ('direct', 'task', 'project', 'support', 'poll'));

-- 2. chats.poll_id (FK back to task_polls)
ALTER TABLE chats ADD COLUMN IF NOT EXISTS poll_id UUID;
CREATE INDEX IF NOT EXISTS idx_chats_poll
  ON chats(poll_id) WHERE poll_id IS NOT NULL;

-- 3. task_polls.summary (markdown produced by poll_summarizer)
ALTER TABLE task_polls ADD COLUMN IF NOT EXISTS summary TEXT NULL;

-- 4. task_polls.task_ids — snapshot of active task IDs at poll creation
ALTER TABLE task_polls ADD COLUMN IF NOT EXISTS task_ids JSONB
  NOT NULL DEFAULT '[]'::jsonb;

-- 5. FK chats.poll_id -> task_polls.id (after both tables exist)
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'chats_poll_id_fkey'
    ) THEN
        ALTER TABLE chats
            ADD CONSTRAINT chats_poll_id_fkey
            FOREIGN KEY (poll_id) REFERENCES task_polls(id) ON DELETE CASCADE;
    END IF;
END $$;

-- 6. Roles: poll_interviewer + poll_summarizer in system org
DO $$
DECLARE
    rugpt_org_id UUID := '00000000-0000-0000-0000-000000000000'::uuid;
    role_interviewer_id UUID;
BEGIN
    INSERT INTO roles (
        org_id, name, code, description, system_prompt, model_name,
        agent_type, tools, prompt_file, is_active
    ) VALUES (
        rugpt_org_id,
        'AI-интервьюер опроса',
        'poll_interviewer',
        'AI-интервьюер для утреннего опроса сотрудников',
        'Вы — AI-интервьюер. Выясните у сотрудника статус каждой его задачи.',
        'google/gemma-4-31B-it',
        'simple',
        '[]'::jsonb,
        'poll_interviewer.md',
        true
    )
    ON CONFLICT (org_id, code) DO NOTHING;

    SELECT id INTO role_interviewer_id FROM roles
    WHERE org_id = rugpt_org_id AND code = 'poll_interviewer';

    INSERT INTO roles (
        org_id, name, code, description, system_prompt, model_name,
        agent_type, tools, prompt_file, is_active
    ) VALUES (
        rugpt_org_id,
        'AI-сводчик опроса',
        'poll_summarizer',
        'AI-сводчик: формирует markdown-сводку из транскрипта опроса',
        'Вы — AI-сводчик. Извлеките сводку из транскрипта диалога.',
        'google/gemma-4-31B-it',
        'simple',
        '[]'::jsonb,
        'poll_summarizer.md',
        true
    )
    ON CONFLICT (org_id, code) DO NOTHING;

    -- 7. System user for poll_interviewer (member of poll chats)
    INSERT INTO users (
        org_id, name, username, email, password_hash,
        role_id, is_admin, is_system, is_active
    ) VALUES (
        rugpt_org_id, 'AI-интервьюер', 'poll_interviewer_ai',
        'poll-interviewer@rugpt.system', NULL,
        role_interviewer_id, false, true, true
    )
    ON CONFLICT (org_id, username) DO UPDATE SET role_id = EXCLUDED.role_id;
END $$;
```

- [ ] **Step 4: Apply migration locally**

Run: `cd /root/rugpt && ./migrate.sh`
Expected: "Applied 029_poll_chat_and_roles" (or equivalent — check migrate.sh output style).

- [ ] **Step 5: Run test to verify it passes**

Run: `cd /root/rugpt && venv/bin/pytest tests/test_migration_029.py -v`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
cd /root/rugpt && git add src/engine/migrations/029_poll_chat_and_roles.sql tests/test_migration_029.py
git commit -m "feat(poll): migration 029 — poll chat type, summary field, AI dialog roles"
```

---

## Task 2: Update model classes

**Files:**
- Modify: `src/engine/models/chat.py`
- Modify: `src/engine/models/task_poll.py`
- Test: `tests/test_chat_model.py` (extend), new tests for task_poll model

- [ ] **Step 1: Write failing test for ChatType.POLL and Chat.poll_id**

Append to `tests/test_chat_model.py`:

```python
def test_chat_type_poll_exists():
    from src.engine.models.chat import ChatType
    assert ChatType.POLL.value == "poll"


def test_chat_poll_id_field():
    from src.engine.models.chat import Chat, ChatType
    from uuid import uuid4
    poll_id = uuid4()
    chat = Chat(type=ChatType.POLL, poll_id=poll_id)
    assert chat.poll_id == poll_id

    d = chat.to_dict()
    assert d["poll_id"] == str(poll_id)
    assert d["type"] == "poll"
```

- [ ] **Step 2: Run to verify fail**

Run: `cd /root/rugpt && venv/bin/pytest tests/test_chat_model.py::test_chat_type_poll_exists tests/test_chat_model.py::test_chat_poll_id_field -v`
Expected: FAIL with `AttributeError` for POLL or `TypeError` for poll_id.

- [ ] **Step 3: Update Chat model**

Edit `src/engine/models/chat.py`:
- Find `class ChatType(str, Enum)`. Add `POLL = "poll"` next to `SUPPORT`.
- Find `@dataclass class Chat:`. Add field after `support_ticket_id`:
  ```python
  poll_id: Optional[UUID] = None
  ```
- In `to_dict()` add `"poll_id": str(self.poll_id) if self.poll_id else None`.

- [ ] **Step 4: Run test, verify pass**

Run: `cd /root/rugpt && venv/bin/pytest tests/test_chat_model.py -v`
Expected: PASS for new tests, no regressions.

- [ ] **Step 5: Write failing test for TaskPoll.summary and task_ids**

Create `tests/test_task_poll_model.py`:

```python
"""TaskPoll model new fields: summary + task_ids snapshot."""
from uuid import uuid4
from src.engine.models.task_poll import TaskPoll


def test_task_poll_summary_default_none():
    poll = TaskPoll()
    assert poll.summary is None


def test_task_poll_summary_set():
    poll = TaskPoll(summary="## Сводка\n- задача 1: в работе")
    assert "Сводка" in poll.summary
    assert poll.to_dict()["summary"] == poll.summary


def test_task_poll_task_ids_default_empty():
    poll = TaskPoll()
    assert poll.task_ids == []


def test_task_poll_task_ids_serializable():
    ids = [uuid4(), uuid4()]
    poll = TaskPoll(task_ids=ids)
    d = poll.to_dict()
    assert d["task_ids"] == [str(x) for x in ids]
```

- [ ] **Step 6: Run to verify fail**

Run: `cd /root/rugpt && venv/bin/pytest tests/test_task_poll_model.py -v`
Expected: FAIL with AttributeError on summary/task_ids.

- [ ] **Step 7: Update TaskPoll model**

Edit `src/engine/models/task_poll.py`:
- Add field `summary: Optional[str] = None` after `expires_at`.
- Add `task_ids: list = field(default_factory=list)` after `summary`.
- In `to_dict()` add:
  ```python
  "summary": self.summary,
  "task_ids": [str(x) for x in self.task_ids],
  ```

- [ ] **Step 8: Run test, verify pass**

Run: `cd /root/rugpt && venv/bin/pytest tests/test_task_poll_model.py -v`
Expected: 4 passed.

- [ ] **Step 9: Commit**

```bash
cd /root/rugpt && git add src/engine/models/chat.py src/engine/models/task_poll.py tests/test_chat_model.py tests/test_task_poll_model.py
git commit -m "feat(poll): extend Chat (poll_id) and TaskPoll (summary, task_ids) models"
```

---

## Task 3: Storage updates — chat_storage and task_poll_storage

**Files:**
- Modify: `src/engine/storage/chat_storage.py`
- Modify: `src/engine/storage/task_poll_storage.py`
- Create: `tests/test_storage_poll_fields.py`

- [ ] **Step 1: Write failing tests for storage operations**

Create `tests/test_storage_poll_fields.py`:

```python
"""Storage roundtrip tests for new poll-related fields."""
import pytest
import asyncpg
from datetime import date
from uuid import uuid4

from src.engine.config import Config
from src.engine.models.chat import Chat, ChatType
from src.engine.models.task_poll import TaskPoll
from src.engine.storage.chat_storage import ChatStorage
from src.engine.storage.task_poll_storage import TaskPollStorage


@pytest.fixture
async def dsn():
    return Config.get_postgres_dsn()


@pytest.fixture
async def system_org_id():
    return "00000000-0000-0000-0000-000000000000"


@pytest.mark.asyncio
async def test_chat_storage_roundtrips_poll_id(dsn, system_org_id):
    storage = ChatStorage(dsn)
    poll_storage = TaskPollStorage(dsn)
    user_id = uuid4()

    poll = TaskPoll(
        org_id=system_org_id,
        assignee_user_id=user_id,
        poll_date=date.today(),
        task_ids=[uuid4()],
    )
    created_poll = await poll_storage.create(poll)

    chat = Chat(
        org_id=system_org_id,
        type=ChatType.POLL,
        participants=[str(user_id)],
        poll_id=created_poll.id,
    )
    created_chat = await storage.create(chat)
    assert created_chat.poll_id == created_poll.id

    fetched = await storage.get_by_id(created_chat.id)
    assert fetched.poll_id == created_poll.id
    assert fetched.type == ChatType.POLL


@pytest.mark.asyncio
async def test_chat_storage_get_by_poll_id(dsn, system_org_id):
    storage = ChatStorage(dsn)
    poll_storage = TaskPollStorage(dsn)
    user_id = uuid4()

    poll = TaskPoll(org_id=system_org_id, assignee_user_id=user_id, poll_date=date.today())
    created_poll = await poll_storage.create(poll)

    chat = Chat(
        org_id=system_org_id,
        type=ChatType.POLL,
        participants=[str(user_id)],
        poll_id=created_poll.id,
    )
    created_chat = await storage.create(chat)

    found = await storage.get_by_poll_id(created_poll.id)
    assert found is not None
    assert found.id == created_chat.id


@pytest.mark.asyncio
async def test_chat_storage_get_by_poll_id_returns_none_for_missing(dsn):
    storage = ChatStorage(dsn)
    result = await storage.get_by_poll_id(uuid4())
    assert result is None


@pytest.mark.asyncio
async def test_task_poll_storage_roundtrips_summary_and_task_ids(dsn, system_org_id):
    storage = TaskPollStorage(dsn)
    user_id = uuid4()
    task_ids = [uuid4(), uuid4()]

    poll = TaskPoll(
        org_id=system_org_id,
        assignee_user_id=user_id,
        poll_date=date.today(),
        task_ids=task_ids,
    )
    created = await storage.create(poll)
    assert [str(x) for x in created.task_ids] == [str(x) for x in task_ids]

    fetched = await storage.get_by_id(created.id)
    assert [str(x) for x in fetched.task_ids] == [str(x) for x in task_ids]
    assert fetched.summary is None


@pytest.mark.asyncio
async def test_task_poll_storage_update_summary(dsn, system_org_id):
    storage = TaskPollStorage(dsn)
    poll = TaskPoll(
        org_id=system_org_id,
        assignee_user_id=uuid4(),
        poll_date=date.today(),
    )
    created = await storage.create(poll)

    await storage.update_summary(created.id, "## test summary")
    fetched = await storage.get_by_id(created.id)
    assert fetched.summary == "## test summary"
```

- [ ] **Step 2: Run to verify fail**

Run: `cd /root/rugpt && venv/bin/pytest tests/test_storage_poll_fields.py -v`
Expected: FAIL — `get_by_poll_id` not defined, `update_summary` not defined, `task_ids` not selected.

- [ ] **Step 3: Update chat_storage.py**

In `src/engine/storage/chat_storage.py`:
- Find existing SELECT-list (`SELECT id, org_id, type, ...`) — add `poll_id` column.
- Find row→Chat mapper — pass `poll_id=row["poll_id"]`.
- Find `create()` INSERT — add `poll_id` to columns and parameters.
- Add new method:

```python
async def get_by_poll_id(self, poll_id: UUID) -> Optional[Chat]:
    async with self.pool.acquire() as conn:
        row = await conn.fetchrow(
            f"SELECT {self._select_columns()} FROM chats "
            "WHERE poll_id = $1 AND is_active = true LIMIT 1",
            poll_id,
        )
        return self._row_to_chat(row) if row else None
```

(Adapt method names to existing private helpers in the file.)

- [ ] **Step 4: Update task_poll_storage.py**

In `src/engine/storage/task_poll_storage.py`:
- Find SELECT-list — add `summary, task_ids`.
- Find row→TaskPoll mapper — pass `summary=row["summary"]`, `task_ids=[UUID(x) for x in (row["task_ids"] or [])]`.
- Find `create()` INSERT — add `summary, task_ids` to columns and parameters. Pass `[str(x) for x in poll.task_ids]` and `poll.summary` (None).
- Add method:

```python
async def update_summary(self, poll_id: UUID, summary: str) -> None:
    async with self.pool.acquire() as conn:
        await conn.execute(
            "UPDATE task_polls SET summary = $1 WHERE id = $2",
            summary, poll_id,
        )
```

- [ ] **Step 5: Run tests to verify pass**

Run: `cd /root/rugpt && venv/bin/pytest tests/test_storage_poll_fields.py -v`
Expected: 5 passed.

- [ ] **Step 6: Run full storage tests for regressions**

Run: `cd /root/rugpt && venv/bin/pytest tests/ -k storage -v`
Expected: all pre-existing tests still pass.

- [ ] **Step 7: Commit**

```bash
cd /root/rugpt && git add src/engine/storage/chat_storage.py src/engine/storage/task_poll_storage.py tests/test_storage_poll_fields.py
git commit -m "feat(poll): chat_storage handles poll_id, task_poll_storage handles summary + task_ids"
```

---

## Task 4: Prompt files

**Files:**
- Create: `src/engine/prompts/poll_interviewer.md`
- Create: `src/engine/prompts/poll_summarizer.md`

- [ ] **Step 1: Create poll_interviewer.md**

Content of `src/engine/prompts/poll_interviewer.md`:

```markdown
# AI-интервьюер утреннего опроса

Вы — внутренний AI-интервьюер. Ваша единственная задача — короткий утренний разговор с сотрудником про его активные задачи.

## Что вы получаете

В первом сообщении пользователя — имя сотрудника и список его активных задач: для каждой указаны название и срок (если есть).

## Что вы должны сделать

1. Поприветствуйте сотрудника по имени, кратко.
2. Перечислите задачи списком и предложите рассказать про каждую.
3. По мере диалога уточняйте по каждой задаче:
   - текущий статус (в работе / готово / заблокирован / переехал срок и пр.)
   - что изменилось со вчера
   - есть ли проблемы / нужна ли помощь
4. Не давайте советов, не предлагайте решений. Вы только собираете информацию.
5. Если по задаче сотрудник коротко сказал "всё нормально, в работе" — не настаивайте.
6. Когда видите что по всем задачам прошлись — мягко напомните что есть кнопка "Завершить отчёт".

## Чего нельзя делать

- Не выдумывайте задачи, которых нет на входе.
- Не комментируйте качество работы сотрудника.
- Не давайте обещаний от лица руководителя.
- Не предлагайте свои варианты решения проблем.

## Стиль

Краткий, доброжелательный, по делу. Без воды. Без формальностей вроде "С уважением". На "ты" с сотрудником если в имени есть только имя; на "вы" если ФИО.
```

- [ ] **Step 2: Create poll_summarizer.md**

Content of `src/engine/prompts/poll_summarizer.md`:

```markdown
# AI-сводчик утреннего опроса

Вы — AI-сводчик. Получаете на вход транскрипт диалога между AI-интервьюером и сотрудником. Ваша задача — извлечь структурированную сводку для руководителя.

## Формат входа

Транскрипт диалога в виде последовательности сообщений. Сообщения сотрудника помечены как USER, сообщения интервьюера — как ASSISTANT.

## Формат выхода

Markdown-документ со следующей структурой:

```
## По задачам

- **Название задачи 1** — статус по словам сотрудника. Комментарий, если был.
- **Название задачи 2** — ...
- **Название задачи 3** — не обсуждалась

## Тревожные сигналы

- Что-то конкретное, требующее внимания руководителя (просрочки, блокировки, проблемы).
- Если ничего нет — раздел опускайте.

## Рекомендации

(Опционально, не более 2 пунктов. Только если в диалоге сотрудник прямо просил помощи или эскалации.)
```

## Правила

- **Не выдумывайте факты.** Только то, что есть в транскрипте.
- Если задача не упоминалась — пишите "не обсуждалась" вместо домыслов.
- Передавайте слова сотрудника близко к оригиналу — это нужно руководителю.
- Не оценивайте работу сотрудника (хорошо/плохо).
- Если транскрипт пустой или почти пустой — пишите одну строку "Сотрудник не предоставил информации".
- Без приветствий и подписей.
```

- [ ] **Step 3: Verify files are valid markdown**

Run: `cd /root/rugpt && python -c "from pathlib import Path; t = Path('src/engine/prompts/poll_interviewer.md').read_text(); s = Path('src/engine/prompts/poll_summarizer.md').read_text(); assert len(t) > 200 and len(s) > 200; print('ok')"`
Expected: `ok`.

- [ ] **Step 4: Commit**

```bash
cd /root/rugpt && git add src/engine/prompts/poll_interviewer.md src/engine/prompts/poll_summarizer.md
git commit -m "feat(poll): system prompts for poll_interviewer and poll_summarizer roles"
```

---

## Task 5: ChatService — create_poll_chat

**Files:**
- Modify: `src/engine/services/chat_service.py`
- Create: `tests/test_chat_service_poll.py`

- [ ] **Step 1: Write failing test**

Create `tests/test_chat_service_poll.py`:

```python
"""ChatService.create_poll_chat — idempotent creation of poll-scoped chat."""
import pytest
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

from src.engine.models.chat import Chat, ChatType
from src.engine.services.chat_service import ChatService


@pytest.mark.asyncio
async def test_create_poll_chat_creates_when_missing():
    chat_storage = AsyncMock()
    chat_storage.get_by_poll_id = AsyncMock(return_value=None)
    chat_storage.create = AsyncMock(side_effect=lambda c: c)
    message_storage = AsyncMock()

    service = ChatService(chat_storage, message_storage)

    poll_id = uuid4()
    assignee_id = uuid4()
    interviewer_id = uuid4()
    org_id = uuid4()

    chat = await service.create_poll_chat(
        poll_id=poll_id,
        assignee_user_id=assignee_id,
        interviewer_user_id=interviewer_id,
        org_id=org_id,
    )

    assert chat.type == ChatType.POLL
    assert chat.poll_id == poll_id
    assert str(assignee_id) in chat.participants
    assert str(interviewer_id) in chat.participants
    chat_storage.create.assert_awaited_once()


@pytest.mark.asyncio
async def test_create_poll_chat_returns_existing():
    existing = Chat(
        id=uuid4(),
        type=ChatType.POLL,
        poll_id=uuid4(),
        participants=["x", "y"],
    )
    chat_storage = AsyncMock()
    chat_storage.get_by_poll_id = AsyncMock(return_value=existing)
    chat_storage.create = AsyncMock()
    message_storage = AsyncMock()

    service = ChatService(chat_storage, message_storage)

    chat = await service.create_poll_chat(
        poll_id=existing.poll_id,
        assignee_user_id=uuid4(),
        interviewer_user_id=uuid4(),
        org_id=uuid4(),
    )

    assert chat.id == existing.id
    chat_storage.create.assert_not_awaited()
```

- [ ] **Step 2: Run to verify fail**

Run: `cd /root/rugpt && venv/bin/pytest tests/test_chat_service_poll.py -v`
Expected: FAIL — `create_poll_chat` not defined.

- [ ] **Step 3: Add create_poll_chat to ChatService**

In `src/engine/services/chat_service.py` add method:

```python
async def create_poll_chat(
    self,
    poll_id: UUID,
    assignee_user_id: UUID,
    interviewer_user_id: UUID,
    org_id: UUID,
) -> Chat:
    """Idempotent creation of a poll-scoped chat.
    Participants: [assignee, poll_interviewer_ai].
    Returns existing chat if one already exists for this poll_id.
    """
    existing = await self.chat_storage.get_by_poll_id(poll_id)
    if existing is not None:
        return existing

    chat = Chat(
        org_id=org_id,
        type=ChatType.POLL,
        participants=[str(assignee_user_id), str(interviewer_user_id)],
        poll_id=poll_id,
    )
    return await self.chat_storage.create(chat)
```

(Add UUID/Chat/ChatType imports if needed.)

- [ ] **Step 4: Run test, verify pass**

Run: `cd /root/rugpt && venv/bin/pytest tests/test_chat_service_poll.py -v`
Expected: 2 passed.

- [ ] **Step 5: Commit**

```bash
cd /root/rugpt && git add src/engine/services/chat_service.py tests/test_chat_service_poll.py
git commit -m "feat(poll): ChatService.create_poll_chat idempotent for poll-scoped chats"
```

---

## Task 6: AIService — generate_poll_initial + _enqueue_poll_initial

**Files:**
- Modify: `src/engine/services/ai_service.py`
- Create: `tests/test_ai_service_poll_initial.py`

- [ ] **Step 1: Write failing tests**

Create `tests/test_ai_service_poll_initial.py`:

```python
"""AIService.generate_poll_initial — generate AI greeting for poll chat."""
import pytest
from unittest.mock import AsyncMock, MagicMock
from datetime import date
from uuid import uuid4

from src.engine.agents.result import AgentResult
from src.engine.models.task_poll import TaskPoll


def make_ai_service(
    poll=None,
    tasks_map=None,
    role=None,
    agent_result_content="Привет! Расскажи про задачи.",
):
    from src.engine.services.ai_service import AIService

    poll_storage = AsyncMock()
    poll_storage.get_by_id = AsyncMock(return_value=poll)

    task_storage = AsyncMock()
    task_storage.get_many_by_ids = AsyncMock(return_value=tasks_map or {})

    role_storage = AsyncMock()
    role_storage.get_by_code = AsyncMock(return_value=role)

    user_storage = AsyncMock()
    user_storage.get_by_id = AsyncMock(return_value=MagicMock(name="Иван", username="ivan"))

    chat_storage = AsyncMock()
    message_storage = AsyncMock()
    message_storage.create = AsyncMock(side_effect=lambda m: m)

    agent_executor = AsyncMock()
    agent_executor.execute = AsyncMock(return_value=AgentResult(content=agent_result_content))

    agent_run_storage = AsyncMock()
    agent_run_storage.create = AsyncMock(side_effect=lambda r: r)

    kafka_producer = AsyncMock()
    kafka_producer.send = AsyncMock()

    service = AIService(
        chat_storage=chat_storage,
        message_storage=message_storage,
        user_storage=user_storage,
        role_storage=role_storage,
        agent_executor=agent_executor,
        mention_service=AsyncMock(),
        agent_run_storage=agent_run_storage,
        kafka_producer=kafka_producer,
    )
    # Wire poll-related deps via setters (or constructor — match actual signature)
    service.task_poll_storage = poll_storage
    service.task_storage = task_storage

    return service, agent_executor, message_storage, poll_storage


@pytest.mark.asyncio
async def test_generate_poll_initial_persists_ai_message():
    poll_id = uuid4()
    chat_id = uuid4()
    responder_id = uuid4()
    assignee_id = uuid4()
    task_id = uuid4()

    poll = TaskPoll(
        id=poll_id,
        assignee_user_id=assignee_id,
        poll_date=date.today(),
        task_ids=[task_id],
    )
    task = MagicMock(id=task_id, title="Задача 1", deadline=None)
    role = MagicMock(code="poll_interviewer")

    service, executor, msg_storage, _ = make_ai_service(
        poll=poll, tasks_map={task_id: task}, role=role,
    )

    msg = await service.generate_poll_initial(poll_id, chat_id, responder_id)

    assert msg is not None
    executor.execute.assert_awaited_once()
    call_kwargs = executor.execute.call_args.kwargs or executor.execute.call_args[1]
    # role passed
    assert call_kwargs.get("role") == role or executor.execute.call_args[0][0] == role
    msg_storage.create.assert_awaited_once()


@pytest.mark.asyncio
async def test_generate_poll_initial_raises_when_role_missing():
    poll = TaskPoll(id=uuid4(), task_ids=[uuid4()])
    service, *_ = make_ai_service(poll=poll, role=None)

    with pytest.raises(RuntimeError, match="role.*not found"):
        await service.generate_poll_initial(poll.id, uuid4(), uuid4())


@pytest.mark.asyncio
async def test_generate_poll_initial_raises_when_poll_missing():
    service, *_ = make_ai_service(poll=None)

    with pytest.raises(RuntimeError, match="poll.*not found"):
        await service.generate_poll_initial(uuid4(), uuid4(), uuid4())


@pytest.mark.asyncio
async def test_generate_poll_initial_propagates_llm_error():
    poll = TaskPoll(id=uuid4(), task_ids=[])
    role = MagicMock(code="poll_interviewer")
    service, executor, *_ = make_ai_service(poll=poll, role=role)
    executor.execute = AsyncMock(side_effect=RuntimeError("llm down"))

    with pytest.raises(RuntimeError, match="llm down"):
        await service.generate_poll_initial(poll.id, uuid4(), uuid4())
```

- [ ] **Step 2: Run tests to verify fail**

Run: `cd /root/rugpt && venv/bin/pytest tests/test_ai_service_poll_initial.py -v`
Expected: FAIL — method not defined.

- [ ] **Step 3: Implement generate_poll_initial in AIService**

In `src/engine/services/ai_service.py`:

Add constants near top of file (after imports):
```python
POLL_INTERVIEWER_ROLE_CODE = "poll_interviewer"
POLL_SUMMARIZER_ROLE_CODE = "poll_summarizer"
RUGPT_SYSTEM_ORG_ID = UUID("00000000-0000-0000-0000-000000000000")
```

Make sure `task_poll_storage` and `task_storage` are accepted in `AIService.__init__` (extend constructor with these dependencies; default `None` for backwards compat with existing tests).

Add method:

```python
async def generate_poll_initial(
    self,
    poll_id: UUID,
    chat_id: UUID,
    responder_id: UUID,
) -> Optional[Message]:
    """Generate first AI greeting in a poll chat. Reads poll.task_ids snapshot,
    bulk-fetches task titles, calls AgentExecutor with poll_interviewer role,
    persists AI message in chat.messages."""

    if self.task_poll_storage is None or self.task_storage is None:
        raise RuntimeError("AIService: task_poll_storage / task_storage not wired")

    poll = await self.task_poll_storage.get_by_id(poll_id)
    if poll is None:
        raise RuntimeError(f"poll {poll_id} not found")

    role = await self.role_storage.get_by_code(
        POLL_INTERVIEWER_ROLE_CODE, RUGPT_SYSTEM_ORG_ID,
    )
    if role is None:
        raise RuntimeError(
            f"role '{POLL_INTERVIEWER_ROLE_CODE}' not found in system org "
            f"(did migration 029 run?)"
        )

    # Resolve assignee name
    assignee = await self.user_storage.get_by_id(poll.assignee_user_id)
    assignee_name = (assignee.name or assignee.username) if assignee else "сотрудник"

    # Bulk-fetch task titles
    task_ids_uuid = [
        x if isinstance(x, UUID) else UUID(x) for x in (poll.task_ids or [])
    ]
    tasks_map = (
        await self.task_storage.get_many_by_ids(task_ids_uuid)
        if task_ids_uuid else {}
    )

    # Build user input listing tasks
    lines = [f"Сотрудник: {assignee_name}.", "Активные задачи на сегодня:"]
    if not tasks_map:
        lines.append("- (список задач пуст)")
    else:
        for tid in task_ids_uuid:
            t = tasks_map.get(tid)
            if t:
                deadline_str = f" (срок: {t.deadline.isoformat()})" if t.deadline else ""
                lines.append(f"- {t.title}{deadline_str}")
    lines.append("")
    lines.append(
        "Поприветствуй сотрудника, перечисли задачи и попроси рассказать "
        "про каждую: статус, что изменилось, есть ли проблемы."
    )
    user_input = "\n".join(lines)

    result = await self.agent_executor.execute(
        role=role,
        messages=[{"role": "user", "content": user_input}],
        temperature=0.5,
        max_tokens=1024,
        user_id=poll.assignee_user_id,
    )

    if not (result and result.content and result.content.strip()):
        raise RuntimeError("agent_executor returned empty content")

    ai_message = Message(
        chat_id=chat_id,
        sender_id=responder_id,
        sender_type=SenderType.AI_ROLE,
        content=result.content.strip(),
        ai_is_valid=True,
    )
    return await self.message_storage.create(ai_message)
```

(Adjust imports: ensure `Message`, `SenderType` are imported.)

- [ ] **Step 4: Run tests, verify pass**

Run: `cd /root/rugpt && venv/bin/pytest tests/test_ai_service_poll_initial.py -v`
Expected: 4 passed.

- [ ] **Step 5: Add helper _enqueue_poll_initial**

Append to AIService:

```python
async def _enqueue_poll_initial(
    self,
    poll_id: UUID,
    chat_id: UUID,
    responder_id: UUID,
) -> Optional[UUID]:
    """Create AgentRun(pending) and publish to agent.requests with kind='poll_initial'.
    Returns request_id, or None if Kafka publish failed."""
    if self.agent_run_storage is None or self.kafka_producer is None:
        raise RuntimeError("AIService: agent_run_storage / kafka_producer not wired")

    request_id = uuid4()
    agent_run = AgentRun(
        id=request_id,
        chat_id=chat_id,
        responder_id=responder_id,
        status="pending",
    )
    try:
        await self.agent_run_storage.create(agent_run)
        await self.kafka_producer.send(
            Config.KAFKA_TOPIC_AGENT_REQUESTS,
            {
                "request_id": str(request_id),
                "chat_id": str(chat_id),
                "responder_id": str(responder_id),
                "kind": "poll_initial",
                "poll_id": str(poll_id),
            },
            key=str(chat_id),
        )
        return request_id
    except Exception as e:
        logger.error(
            f"_enqueue_poll_initial: failed for poll={poll_id}: {e}",
            exc_info=True,
        )
        try:
            await self.agent_run_storage.mark_failed(request_id, str(e))
        except Exception:
            pass
        raise
```

- [ ] **Step 6: Add quick test for _enqueue_poll_initial**

Append to `tests/test_ai_service_poll_initial.py`:

```python
@pytest.mark.asyncio
async def test_enqueue_poll_initial_publishes_kafka():
    poll = TaskPoll(id=uuid4())
    service, _, _, _ = make_ai_service(poll=poll, role=MagicMock())
    chat_id = uuid4()
    poll_id = uuid4()
    responder_id = uuid4()

    rid = await service._enqueue_poll_initial(poll_id, chat_id, responder_id)

    assert rid is not None
    service.kafka_producer.send.assert_awaited_once()
    args, kwargs = service.kafka_producer.send.call_args
    payload = args[1] if len(args) > 1 else kwargs.get("value") or args[1]
    assert payload["kind"] == "poll_initial"
    assert payload["poll_id"] == str(poll_id)
    assert payload["chat_id"] == str(chat_id)
```

- [ ] **Step 7: Run tests**

Run: `cd /root/rugpt && venv/bin/pytest tests/test_ai_service_poll_initial.py -v`
Expected: 5 passed.

- [ ] **Step 8: Commit**

```bash
cd /root/rugpt && git add src/engine/services/ai_service.py tests/test_ai_service_poll_initial.py
git commit -m "feat(poll): AIService.generate_poll_initial + _enqueue_poll_initial"
```

---

## Task 7: AIService — generate_poll_summary + _enqueue_poll_summary

**Files:**
- Modify: `src/engine/services/ai_service.py`
- Create: `tests/test_ai_service_poll_summary.py`

- [ ] **Step 1: Write failing tests**

Create `tests/test_ai_service_poll_summary.py`:

```python
"""AIService.generate_poll_summary — final summary at submit time."""
import pytest
from unittest.mock import AsyncMock, MagicMock
from datetime import date
from uuid import uuid4

from src.engine.agents.result import AgentResult
from src.engine.models.task_poll import TaskPoll


def make_service(
    poll=None,
    messages=None,
    role=None,
    agent_result_content="## По задачам\n- Задача 1: в работе.",
):
    from src.engine.services.ai_service import AIService

    poll_storage = AsyncMock()
    poll_storage.get_by_id = AsyncMock(return_value=poll)
    poll_storage.update_summary = AsyncMock()
    poll_storage.update_status = AsyncMock()

    msg_storage = AsyncMock()
    msg_storage.list_by_chat = AsyncMock(return_value=messages or [])
    msg_storage.create = AsyncMock(side_effect=lambda m: m)

    role_storage = AsyncMock()
    role_storage.get_by_code = AsyncMock(return_value=role)

    executor = AsyncMock()
    executor.execute = AsyncMock(return_value=AgentResult(content=agent_result_content))

    user_storage = AsyncMock()
    chat_storage = AsyncMock()
    agent_run_storage = AsyncMock()
    kafka_producer = AsyncMock()

    service = AIService(
        chat_storage=chat_storage,
        message_storage=msg_storage,
        user_storage=user_storage,
        role_storage=role_storage,
        agent_executor=executor,
        mention_service=AsyncMock(),
        agent_run_storage=agent_run_storage,
        kafka_producer=kafka_producer,
    )
    service.task_poll_storage = poll_storage
    service.task_storage = AsyncMock()

    return service, executor, poll_storage, msg_storage


@pytest.mark.asyncio
async def test_generate_poll_summary_writes_summary_and_completes_poll():
    poll = TaskPoll(id=uuid4(), assignee_user_id=uuid4(), poll_date=date.today())
    role = MagicMock(code="poll_summarizer")
    user_msg = MagicMock(sender_type="user", content="Задача 1 в работе", sender_id=poll.assignee_user_id)
    ai_msg = MagicMock(sender_type="ai_role", content="Понял.", sender_id=uuid4())

    service, executor, poll_storage, msg_storage = make_service(
        poll=poll, messages=[user_msg, ai_msg], role=role,
    )

    chat_id = uuid4()
    responder_id = uuid4()
    sys_msg = await service.generate_poll_summary(poll.id, chat_id, responder_id)

    assert sys_msg is not None
    poll_storage.update_summary.assert_awaited_once()
    args = poll_storage.update_summary.call_args[0]
    assert args[0] == poll.id
    assert "По задачам" in args[1]

    poll_storage.update_status.assert_awaited_once()
    # Persisted system "Отчёт сдан" message
    assert msg_storage.create.await_count == 1


@pytest.mark.asyncio
async def test_generate_poll_summary_raises_on_empty_llm_output():
    poll = TaskPoll(id=uuid4())
    role = MagicMock(code="poll_summarizer")
    service, executor, *_ = make_service(poll=poll, messages=[MagicMock()], role=role,
                                          agent_result_content="")

    with pytest.raises(RuntimeError, match="empty"):
        await service.generate_poll_summary(poll.id, uuid4(), uuid4())


@pytest.mark.asyncio
async def test_generate_poll_summary_raises_when_role_missing():
    poll = TaskPoll(id=uuid4())
    service, *_ = make_service(poll=poll, messages=[MagicMock()], role=None)

    with pytest.raises(RuntimeError, match="role.*not found"):
        await service.generate_poll_summary(poll.id, uuid4(), uuid4())
```

- [ ] **Step 2: Run to verify fail**

Run: `cd /root/rugpt && venv/bin/pytest tests/test_ai_service_poll_summary.py -v`
Expected: FAIL.

- [ ] **Step 3: Implement generate_poll_summary**

Add to AIService:

```python
async def generate_poll_summary(
    self,
    poll_id: UUID,
    chat_id: UUID,
    responder_id: UUID,
) -> Optional[Message]:
    """Build transcript from chat.messages, call poll_summarizer LLM,
    write result to poll.summary, mark poll completed, persist
    'Отчёт сдан' system message in chat."""

    if self.task_poll_storage is None:
        raise RuntimeError("AIService: task_poll_storage not wired")

    poll = await self.task_poll_storage.get_by_id(poll_id)
    if poll is None:
        raise RuntimeError(f"poll {poll_id} not found")

    role = await self.role_storage.get_by_code(
        POLL_SUMMARIZER_ROLE_CODE, RUGPT_SYSTEM_ORG_ID,
    )
    if role is None:
        raise RuntimeError(
            f"role '{POLL_SUMMARIZER_ROLE_CODE}' not found "
            f"(did migration 029 run?)"
        )

    messages = await self.message_storage.list_by_chat(chat_id)
    transcript_lines = []
    for m in messages:
        role_label = "USER" if m.sender_type == SenderType.USER else "ASSISTANT"
        transcript_lines.append(f"{role_label}: {m.content}")
    transcript = "\n".join(transcript_lines) if transcript_lines else "(пусто)"

    user_input = (
        f"Транскрипт диалога:\n\n{transcript}\n\n"
        f"Извлеки сводку по структуре, описанной в системном промпте."
    )

    result = await self.agent_executor.execute(
        role=role,
        messages=[{"role": "user", "content": user_input}],
        temperature=0.3,
        max_tokens=2048,
        user_id=poll.assignee_user_id,
    )

    if not (result and result.content and result.content.strip()):
        raise RuntimeError("agent_executor returned empty content")

    summary_text = result.content.strip()

    # Атомарная пара UPDATE'ов. Выполняем последовательно — если падёт между,
    # caller (handler) пометит agent_run failed, retry даст ту же картину.
    await self.task_poll_storage.update_summary(poll_id, summary_text)
    await self.task_poll_storage.update_status(poll_id, "completed")

    sys_message = Message(
        chat_id=chat_id,
        sender_id=responder_id,
        sender_type=SenderType.AI_ROLE,
        content="Отчёт сдан, спасибо.",
        ai_is_valid=True,
    )
    return await self.message_storage.create(sys_message)
```

(Note: `update_status` may need an additional param for `completed_at`. If signature differs, adapt; if no overload, add second call to update completed_at. See actual `task_poll_storage.update_status`.)

- [ ] **Step 4: Run tests, verify pass**

Run: `cd /root/rugpt && venv/bin/pytest tests/test_ai_service_poll_summary.py -v`
Expected: 3 passed.

- [ ] **Step 5: Add _enqueue_poll_summary helper**

```python
async def _enqueue_poll_summary(
    self,
    poll_id: UUID,
    chat_id: UUID,
    responder_id: UUID,
) -> Optional[UUID]:
    """Create AgentRun(pending) and publish kind='poll_summary' to agent.requests."""
    if self.agent_run_storage is None or self.kafka_producer is None:
        raise RuntimeError("AIService: agent_run_storage / kafka_producer not wired")

    request_id = uuid4()
    agent_run = AgentRun(
        id=request_id,
        chat_id=chat_id,
        responder_id=responder_id,
        status="pending",
    )
    try:
        await self.agent_run_storage.create(agent_run)
        await self.kafka_producer.send(
            Config.KAFKA_TOPIC_AGENT_REQUESTS,
            {
                "request_id": str(request_id),
                "chat_id": str(chat_id),
                "responder_id": str(responder_id),
                "kind": "poll_summary",
                "poll_id": str(poll_id),
            },
            key=str(chat_id),
        )
        return request_id
    except Exception as e:
        logger.error(
            f"_enqueue_poll_summary: failed for poll={poll_id}: {e}",
            exc_info=True,
        )
        try:
            await self.agent_run_storage.mark_failed(request_id, str(e))
        except Exception:
            pass
        raise
```

- [ ] **Step 6: Add test for _enqueue_poll_summary**

Append to `tests/test_ai_service_poll_summary.py`:

```python
@pytest.mark.asyncio
async def test_enqueue_poll_summary_publishes_kafka():
    poll = TaskPoll(id=uuid4())
    service, _, _, _ = make_service(poll=poll, role=MagicMock())
    rid = await service._enqueue_poll_summary(poll.id, uuid4(), uuid4())
    assert rid is not None
    service.kafka_producer.send.assert_awaited_once()
    payload = service.kafka_producer.send.call_args[0][1]
    assert payload["kind"] == "poll_summary"
    assert payload["poll_id"] == str(poll.id)
```

- [ ] **Step 7: Run tests**

Run: `cd /root/rugpt && venv/bin/pytest tests/test_ai_service_poll_summary.py -v`
Expected: 4 passed.

- [ ] **Step 8: Commit**

```bash
cd /root/rugpt && git add src/engine/services/ai_service.py tests/test_ai_service_poll_summary.py
git commit -m "feat(poll): AIService.generate_poll_summary + _enqueue_poll_summary"
```

---

## Task 8: AgentRequestHandler — kind dispatch

**Files:**
- Modify: `src/engine/kafka/agent_handler.py`
- Create: `tests/test_agent_handler_poll_dispatch.py`

- [ ] **Step 1: Write failing dispatch tests**

Create `tests/test_agent_handler_poll_dispatch.py`:

```python
"""AgentRequestHandler dispatch by 'kind' payload field."""
import pytest
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

from src.engine.kafka.agent_handler import AgentRequestHandler


def make_handler():
    ai_service = AsyncMock()
    ai_service.generate_response = AsyncMock(return_value=MagicMock(id=uuid4(), to_dict=lambda: {}))
    ai_service.generate_poll_initial = AsyncMock(return_value=MagicMock(id=uuid4(), to_dict=lambda: {}))
    ai_service.generate_poll_summary = AsyncMock(return_value=MagicMock(id=uuid4(), to_dict=lambda: {}))

    msg_storage = AsyncMock()
    msg_storage.get_by_id = AsyncMock(return_value=MagicMock(id=uuid4()))

    agent_run_storage = AsyncMock()
    agent_run_storage.mark_running = AsyncMock(return_value=True)
    agent_run_storage.mark_done = AsyncMock()
    agent_run_storage.mark_failed = AsyncMock()
    agent_run_storage.get = AsyncMock()

    kafka = AsyncMock()

    return AgentRequestHandler(
        ai_service=ai_service,
        message_storage=msg_storage,
        agent_run_storage=agent_run_storage,
        kafka_producer=kafka,
    ), ai_service


@pytest.mark.asyncio
async def test_dispatch_message_reply_calls_generate_response():
    handler, ai = make_handler()
    payload = {
        "request_id": str(uuid4()),
        "chat_id": str(uuid4()),
        "user_message_id": str(uuid4()),
        "responder_id": str(uuid4()),
        "kind": "message_reply",
    }
    await handler(payload)
    ai.generate_response.assert_awaited_once()
    ai.generate_poll_initial.assert_not_awaited()
    ai.generate_poll_summary.assert_not_awaited()


@pytest.mark.asyncio
async def test_dispatch_default_kind_is_message_reply():
    handler, ai = make_handler()
    payload = {
        "request_id": str(uuid4()),
        "chat_id": str(uuid4()),
        "user_message_id": str(uuid4()),
        "responder_id": str(uuid4()),
        # no 'kind' — must default to message_reply
    }
    await handler(payload)
    ai.generate_response.assert_awaited_once()


@pytest.mark.asyncio
async def test_dispatch_poll_initial_calls_generate_poll_initial():
    handler, ai = make_handler()
    payload = {
        "request_id": str(uuid4()),
        "chat_id": str(uuid4()),
        "responder_id": str(uuid4()),
        "kind": "poll_initial",
        "poll_id": str(uuid4()),
    }
    await handler(payload)
    ai.generate_poll_initial.assert_awaited_once()
    ai.generate_response.assert_not_awaited()
    ai.generate_poll_summary.assert_not_awaited()


@pytest.mark.asyncio
async def test_dispatch_poll_summary_calls_generate_poll_summary():
    handler, ai = make_handler()
    payload = {
        "request_id": str(uuid4()),
        "chat_id": str(uuid4()),
        "responder_id": str(uuid4()),
        "kind": "poll_summary",
        "poll_id": str(uuid4()),
    }
    await handler(payload)
    ai.generate_poll_summary.assert_awaited_once()


@pytest.mark.asyncio
async def test_dispatch_unknown_kind_skips_silently():
    handler, ai = make_handler()
    payload = {
        "request_id": str(uuid4()),
        "chat_id": str(uuid4()),
        "responder_id": str(uuid4()),
        "kind": "made_up_kind",
    }
    # Should NOT raise (poison message protection)
    await handler(payload)
    ai.generate_response.assert_not_awaited()
    ai.generate_poll_initial.assert_not_awaited()
    ai.generate_poll_summary.assert_not_awaited()


@pytest.mark.asyncio
async def test_dispatch_poll_initial_missing_poll_id_skips():
    handler, ai = make_handler()
    payload = {
        "request_id": str(uuid4()),
        "chat_id": str(uuid4()),
        "responder_id": str(uuid4()),
        "kind": "poll_initial",
        # poll_id missing
    }
    await handler(payload)
    ai.generate_poll_initial.assert_not_awaited()
```

- [ ] **Step 2: Run to verify fail**

Run: `cd /root/rugpt && venv/bin/pytest tests/test_agent_handler_poll_dispatch.py -v`
Expected: FAIL — handler doesn't recognize new kinds.

- [ ] **Step 3: Update agent_handler.py**

In `src/engine/kafka/agent_handler.py`, modify `__call__`:

Find payload parsing section and replace:

```python
async def __call__(self, payload: dict) -> None:
    """Entry point for KafkaConsumerLoop. Raises on failure to trigger retry."""
    try:
        request_id = UUID(payload["request_id"])
        chat_id = UUID(payload["chat_id"])
        responder_id = UUID(payload.get("responder_id"))
        kind = payload.get("kind", "message_reply")
    except (KeyError, ValueError, TypeError) as e:
        logger.error(f"Malformed agent.requests payload: {e} payload={payload}")
        return

    # Validate kind-specific required fields BEFORE acquiring agent_run lock
    if kind == "message_reply":
        user_message_id_raw = payload.get("user_message_id")
        if not user_message_id_raw:
            logger.error(f"message_reply payload missing user_message_id: {payload}")
            return
        try:
            user_message_id = UUID(user_message_id_raw)
        except (ValueError, TypeError):
            logger.error(f"message_reply payload bad user_message_id: {payload}")
            return
        strip_username = payload.get("strip_username")
    elif kind in ("poll_initial", "poll_summary"):
        poll_id_raw = payload.get("poll_id")
        if not poll_id_raw:
            logger.error(f"{kind} payload missing poll_id: {payload}")
            return
        try:
            poll_id = UUID(poll_id_raw)
        except (ValueError, TypeError):
            logger.error(f"{kind} payload bad poll_id: {payload}")
            return
    else:
        logger.error(f"Unknown kind={kind}, payload={payload}")
        return

    # Idempotency: atomic CAS pending -> running
    acquired = await self.agent_run_storage.mark_running(request_id)
    if not acquired:
        existing = await self.agent_run_storage.get(request_id)
        logger.info(
            f"agent_run {request_id} already status="
            f"{existing.status if existing else 'missing'}, skipping"
        )
        return

    try:
        if kind == "message_reply":
            user_message = await self.message_storage.get_by_id(user_message_id)
            if user_message is None:
                raise RuntimeError(f"user_message {user_message_id} not found")
            ai_message = await self.ai_service.generate_response(
                message=user_message,
                responder_id=responder_id,
                strip_username=strip_username,
            )
        elif kind == "poll_initial":
            ai_message = await self.ai_service.generate_poll_initial(
                poll_id=poll_id, chat_id=chat_id, responder_id=responder_id,
            )
        elif kind == "poll_summary":
            ai_message = await self.ai_service.generate_poll_summary(
                poll_id=poll_id, chat_id=chat_id, responder_id=responder_id,
            )
        else:
            raise RuntimeError(f"unreachable kind={kind}")

        if ai_message is None:
            await self.agent_run_storage.mark_failed(
                request_id, f"{kind}: returned None",
            )
            logger.warning(f"agent_run {request_id} kind={kind}: returned None")
            return

        await self.agent_run_storage.mark_done(request_id, ai_message.id)

        try:
            await self.kafka_producer.send(
                Config.KAFKA_TOPIC_CHAT_EVENTS,
                {
                    "chat_id": str(chat_id),
                    "message": ai_message.to_dict(),
                },
                key=str(chat_id),
            )
        except Exception as e:
            logger.error(f"Failed to publish to chat.events: {e}")

        logger.info(f"agent_run {request_id} kind={kind} done: ai_message={ai_message.id}")

    except Exception as e:
        logger.error(
            f"agent_run {request_id} kind={kind} failed: {e}", exc_info=True,
        )
        try:
            await self.agent_run_storage.mark_failed(request_id, str(e))
        except Exception as mark_err:
            logger.error(f"Failed to mark_failed: {mark_err}")
        raise
```

- [ ] **Step 4: Run tests, verify pass**

Run: `cd /root/rugpt && venv/bin/pytest tests/test_agent_handler_poll_dispatch.py tests/test_agent_request_handler.py -v`
Expected: all pass (existing + new).

- [ ] **Step 5: Commit**

```bash
cd /root/rugpt && git add src/engine/kafka/agent_handler.py tests/test_agent_handler_poll_dispatch.py
git commit -m "feat(poll): AgentRequestHandler dispatches by kind (message_reply/poll_initial/poll_summary)"
```

---

## Task 9: Wire AIService dependencies in EngineService

**Files:**
- Modify: `src/engine/services/engine_service.py`
- Test: existing engine bootstrap regression check

- [ ] **Step 1: Verify current AIService construction in engine_service.py**

Read `src/engine/services/engine_service.py` and locate `self.ai_service = AIService(...)`. Note current arguments.

- [ ] **Step 2: Extend AIService construction with new deps**

Modify the AIService instantiation to pass `task_poll_storage=self.task_poll_storage` and `task_storage=self.task_storage`.

If AIService constructor already accepts kwargs flexibly, just add the args. Otherwise update constructor signature in `services/ai_service.py` to accept these kwargs (default `None` for backwards compat already done in Task 6/7).

- [ ] **Step 3: Run engine bootstrap test (or regression suite)**

Run: `cd /root/rugpt && venv/bin/pytest tests/test_ai_service_async.py tests/test_ai_service_poll_initial.py tests/test_ai_service_poll_summary.py -v`
Expected: all pass.

Run: `cd /root/rugpt && venv/bin/python -c "import asyncio; from src.engine.services.engine_service import EngineService; asyncio.run(EngineService().__aenter__()) if False else None; print('import ok')"`
Expected: import ok (smoke).

- [ ] **Step 4: Commit**

```bash
cd /root/rugpt && git add src/engine/services/engine_service.py src/engine/services/ai_service.py
git commit -m "feat(poll): wire task_poll_storage and task_storage into AIService"
```

---

## Task 10: TaskPollService.create_daily_poll — create chat + enqueue initial

**Files:**
- Modify: `src/engine/services/task_poll_service.py`
- Modify: `src/engine/services/engine_service.py` (wire chat_service and ai_service into TaskPollService)
- Create: `tests/test_task_poll_service_chat.py`

- [ ] **Step 1: Write failing test**

Create `tests/test_task_poll_service_chat.py`:

```python
"""TaskPollService.create_daily_poll — creates chat + enqueues poll_initial."""
import pytest
from unittest.mock import AsyncMock, MagicMock
from datetime import date
from uuid import uuid4

from src.engine.services.task_poll_service import TaskPollService
from src.engine.models.task_poll import TaskPoll
from src.engine.models.chat import Chat, ChatType


def make_service(
    active_tasks=None,
    poll_interviewer_user_id=None,
):
    storage = AsyncMock()
    storage.get_by_user_and_date = AsyncMock(return_value=None)  # no existing poll
    storage.create = AsyncMock(side_effect=lambda p: p)

    task_service = AsyncMock()
    task_service.storage.list_by_assignee = AsyncMock(return_value=active_tasks or [])

    in_app = AsyncMock()
    in_app.create = AsyncMock()

    chat_service = AsyncMock()
    chat = Chat(id=uuid4(), type=ChatType.POLL, poll_id=uuid4())
    chat_service.create_poll_chat = AsyncMock(return_value=chat)

    ai_service = AsyncMock()
    ai_service._enqueue_poll_initial = AsyncMock(return_value=uuid4())

    user_storage = AsyncMock()
    user_storage.get_by_username = AsyncMock(
        return_value=MagicMock(id=poll_interviewer_user_id or uuid4())
    )

    service = TaskPollService(
        storage=storage,
        task_service=task_service,
        in_app_notification_service=in_app,
    )
    service.chat_service = chat_service
    service.ai_service = ai_service
    service.user_storage = user_storage

    return service, storage, chat_service, ai_service, in_app


@pytest.mark.asyncio
async def test_create_daily_poll_skips_when_no_active_tasks():
    service, storage, chat_svc, ai_svc, in_app = make_service(active_tasks=[])
    poll = await service.create_daily_poll(uuid4(), uuid4())
    assert poll is None
    storage.create.assert_not_awaited()
    chat_svc.create_poll_chat.assert_not_awaited()
    ai_svc._enqueue_poll_initial.assert_not_awaited()


@pytest.mark.asyncio
async def test_create_daily_poll_creates_chat_and_enqueues():
    task = MagicMock(id=uuid4(), title="t1")
    service, storage, chat_svc, ai_svc, in_app = make_service(active_tasks=[task])

    poll = await service.create_daily_poll(uuid4(), uuid4())

    assert poll is not None
    assert [str(x) for x in poll.task_ids] == [str(task.id)]
    storage.create.assert_awaited_once()
    chat_svc.create_poll_chat.assert_awaited_once()
    ai_svc._enqueue_poll_initial.assert_awaited_once()
    in_app.create.assert_awaited_once()
```

- [ ] **Step 2: Run to verify fail**

Run: `cd /root/rugpt && venv/bin/pytest tests/test_task_poll_service_chat.py -v`
Expected: FAIL.

- [ ] **Step 3: Update TaskPollService.create_daily_poll**

In `src/engine/services/task_poll_service.py`:

Modify `create_daily_poll` to:
1. Fetch active tasks via `self.task_service.storage.list_by_assignee(assignee_id, active_only=True)`
2. Return None if list empty
3. Create poll with `task_ids=[t.id for t in tasks]`
4. Resolve poll_interviewer_ai user_id (cache it)
5. Call `self.chat_service.create_poll_chat(poll.id, assignee_id, interviewer_id, org_id)`
6. Call `self.ai_service._enqueue_poll_initial(poll.id, chat.id, interviewer_id)`
7. Create in-app notification

Add to constructor (or as setter assignments below) optional fields: `chat_service`, `ai_service`, `user_storage`. Wire them in `engine_service.py` after both AIService and ChatService are constructed.

Implementation sketch (replace existing method body):

```python
async def create_daily_poll(
    self,
    assignee_user_id: UUID,
    org_id: UUID,
) -> Optional[TaskPoll]:
    today = date.today()

    # Idempotency: skip if already exists
    existing = await self.storage.get_by_user_and_date(assignee_user_id, today)
    if existing is not None:
        return existing

    # Fetch active tasks for assignee
    if hasattr(self.task_service, 'storage'):
        active_tasks = await self.task_service.storage.list_by_assignee(
            assignee_user_id, active_only=True,
        )
    else:
        active_tasks = []

    if not active_tasks:
        logger.info(f"No active tasks for {assignee_user_id}, skipping poll")
        return None

    # Create poll
    poll = TaskPoll(
        org_id=org_id,
        assignee_user_id=assignee_user_id,
        poll_date=today,
        status="pending",
        task_ids=[t.id for t in active_tasks],
    )
    poll = await self.storage.create(poll)

    # Create chat (if wired)
    if self.chat_service is not None and self.ai_service is not None and self.user_storage is not None:
        # Resolve poll_interviewer_ai user (cached lookup)
        interviewer = await self.user_storage.get_by_username(
            "poll_interviewer_ai",
        )
        if interviewer is None:
            logger.error(
                "poll_interviewer_ai user not found in DB; "
                "did migration 029 run?"
            )
        else:
            chat = await self.chat_service.create_poll_chat(
                poll_id=poll.id,
                assignee_user_id=assignee_user_id,
                interviewer_user_id=interviewer.id,
                org_id=org_id,
            )
            try:
                await self.ai_service._enqueue_poll_initial(
                    poll.id, chat.id, interviewer.id,
                )
            except Exception as e:
                logger.error(
                    f"Failed to enqueue poll_initial for poll {poll.id}: {e}",
                    exc_info=True,
                )

    # In-app notification
    await self.notification_service.create(
        user_id=assignee_user_id,
        org_id=org_id,
        type="poll",
        title="Утренний опрос",
        content="Расскажите AI про статус ваших задач.",
        reference_type="task_poll",
        reference_id=poll.id,
    )

    return poll
```

(Adapt to current method signature and existing fields.)

- [ ] **Step 4: Wire chat_service / ai_service / user_storage in TaskPollService constructor**

Add to TaskPollService.__init__ optional params with defaults None, set as attributes. In `engine_service.py` after both ChatService and AIService are constructed, add lines:

```python
self.task_poll_service.chat_service = self.chat_service
self.task_poll_service.ai_service = self.ai_service
self.task_poll_service.user_storage = self.user_storage
```

- [ ] **Step 5: Run test, verify pass**

Run: `cd /root/rugpt && venv/bin/pytest tests/test_task_poll_service_chat.py -v`
Expected: 2 passed.

- [ ] **Step 6: Commit**

```bash
cd /root/rugpt && git add src/engine/services/task_poll_service.py src/engine/services/engine_service.py tests/test_task_poll_service_chat.py
git commit -m "feat(poll): create_daily_poll creates poll-chat and enqueues poll_initial"
```

---

## Task 11: Routes — submit modification + today/chat + history filter

**Files:**
- Modify: `src/engine/routes/task_polls.py`
- Create: `tests/test_routes_task_polls_poll_dialog.py`

- [ ] **Step 1: Write failing tests for routes**

Create `tests/integration/test_task_polls_dialog_routes.py` following the existing pattern in `tests/integration/test_routes_task_chat_integration.py` (read that file for fixture imports and async session setup):

```python
"""Integration tests for poll dialog routes: submit, today/chat."""
import pytest
from datetime import date
from uuid import uuid4

# Import the same fixtures other integration tests use:
# - test_client (FastAPI TestClient with mocked auth)
# - test_db (clean PostgreSQL per test)
# - factory: create_user, create_org, create_task, create_poll
from tests.integration.fixtures import (
    test_client, test_db, create_user, create_org, create_task, create_poll,
)


@pytest.mark.asyncio
async def test_submit_404_for_missing_poll(test_client, create_user):
    user = await create_user()
    resp = test_client.post(
        f"/api/v1/task-polls/{uuid4()}/submit",
        headers={"Authorization": f"Bearer {user.token}"},
    )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_submit_403_when_not_owner(test_client, create_user, create_poll):
    owner = await create_user()
    other = await create_user()
    poll = await create_poll(assignee=owner)
    resp = test_client.post(
        f"/api/v1/task-polls/{poll.id}/submit",
        headers={"Authorization": f"Bearer {other.token}"},
    )
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_submit_409_when_no_user_messages(test_client, create_user, create_poll):
    user = await create_user()
    poll = await create_poll(assignee=user)
    # Poll exists, chat exists, but user hasn't written anything
    resp = test_client.post(
        f"/api/v1/task-polls/{poll.id}/submit",
        headers={"Authorization": f"Bearer {user.token}"},
    )
    assert resp.status_code == 409
    assert "ответьте" in resp.json()["detail"].lower()


@pytest.mark.asyncio
async def test_submit_returns_202_and_publishes_kafka(
    test_client, create_user, create_poll, create_message, mock_kafka_producer,
):
    user = await create_user()
    poll = await create_poll(assignee=user)
    chat = await get_chat_for_poll(poll)
    await create_message(chat_id=chat.id, sender_id=user.id, content="task 1 ok")

    resp = test_client.post(
        f"/api/v1/task-polls/{poll.id}/submit",
        headers={"Authorization": f"Bearer {user.token}"},
    )
    assert resp.status_code == 202
    mock_kafka_producer.send.assert_called_once()
    payload = mock_kafka_producer.send.call_args[0][1]
    assert payload["kind"] == "poll_summary"
    assert payload["poll_id"] == str(poll.id)


@pytest.mark.asyncio
async def test_today_chat_returns_chat_id_for_active_poll(
    test_client, create_user, create_poll,
):
    user = await create_user()
    poll = await create_poll(assignee=user, poll_date=date.today())
    resp = test_client.get(
        "/api/v1/task-polls/today/chat",
        headers={"Authorization": f"Bearer {user.token}"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["poll_id"] == str(poll.id)
    assert body["chat_id"]
    assert body["status"] == "pending"


@pytest.mark.asyncio
async def test_today_chat_404_when_no_poll(test_client, create_user):
    user = await create_user()
    resp = test_client.get(
        "/api/v1/task-polls/today/chat",
        headers={"Authorization": f"Bearer {user.token}"},
    )
    assert resp.status_code == 404
```

If `tests/integration/fixtures.py` does not exist or factories are named differently, read the existing integration tests file (e.g. `tests/test_project_chat_integration.py`) and copy the fixture style. Do not write `pass`-only stubs — write real assertions.

- [ ] **Step 2: Modify POST /task-polls/{id}/submit**

In `src/engine/routes/task_polls.py`, find existing `submit` endpoint:

```python
@router.post("/{poll_id}/submit")
async def submit_poll(
    poll_id: UUID,
    current_user: dict = Depends(get_current_user),
):
    """Submit poll: enqueue summary generation. Returns 202 Accepted."""
    engine = get_engine_service()

    poll = await engine.task_poll_service.storage.get_by_id(poll_id)
    if poll is None:
        raise HTTPException(404, "Poll not found")
    if str(poll.assignee_user_id) != current_user["user_id"]:
        raise HTTPException(403, "Not your poll")
    if poll.status != "pending":
        raise HTTPException(409, f"Poll already {poll.status}")

    # Find chat for this poll
    chat = await engine.chat_storage.get_by_poll_id(poll_id)
    if chat is None:
        raise HTTPException(500, "Poll chat not found — did poll creation succeed?")

    # Validate at least one user message from assignee
    messages = await engine.message_storage.list_by_chat(chat.id)
    user_msg_count = sum(
        1 for m in messages
        if m.sender_type.value == "user" and str(m.sender_id) == current_user["user_id"]
    )
    if user_msg_count == 0:
        raise HTTPException(409, "Сначала ответьте AI о ваших задачах")

    # Resolve poll_interviewer_ai (responder for system 'Отчёт сдан' message)
    interviewer = await engine.user_storage.get_by_username("poll_interviewer_ai")
    if interviewer is None:
        raise HTTPException(500, "poll_interviewer_ai not found (migration 029)")

    try:
        await engine.ai_service._enqueue_poll_summary(
            poll_id=poll_id,
            chat_id=chat.id,
            responder_id=interviewer.id,
        )
    except Exception as e:
        logger.error(f"Failed to enqueue poll_summary: {e}", exc_info=True)
        raise HTTPException(503, "Service temporarily unavailable")

    return Response(status_code=202)
```

(Replace any existing sync implementation. Imports: `from fastapi import Response`.)

- [ ] **Step 3: Add GET /task-polls/today/chat endpoint**

```python
@router.get("/today/chat")
async def get_today_chat(
    current_user: dict = Depends(get_current_user),
):
    """Resolve chat_id for today's active poll for current user."""
    engine = get_engine_service()
    user_id = UUID(current_user["user_id"])

    poll = await engine.task_poll_service.storage.get_by_user_and_date(
        user_id, date.today(),
    )
    if poll is None:
        raise HTTPException(404, "No poll for today")

    chat = await engine.chat_storage.get_by_poll_id(poll.id)
    if chat is None:
        raise HTTPException(404, "Poll chat not found")

    return {
        "chat_id": str(chat.id),
        "poll_id": str(poll.id),
        "status": poll.status,
    }
```

- [ ] **Step 4: Extend GET /task-polls list with include_completed**

Find existing list endpoint, add query param `include_completed: bool = False` and pass through to storage. If storage method doesn't support filter, accept and filter in Python (acceptable for low row counts).

- [ ] **Step 5: Run integration tests**

Run: `cd /root/rugpt && venv/bin/pytest tests/test_routes_task_polls_poll_dialog.py -v`
Expected: PASS (or fill in real fixtures from tests/integration if pseudo placeholders).

- [ ] **Step 6: Commit**

```bash
cd /root/rugpt && git add src/engine/routes/task_polls.py tests/test_routes_task_polls_poll_dialog.py
git commit -m "feat(poll): submit (202 + Kafka enqueue), GET today/chat, include_completed list filter"
```

---

## Task 12: Auto-retry for stuck poll_initial in SchedulerService

**Files:**
- Modify: `src/engine/services/scheduler_service.py`
- Create: `tests/test_scheduler_poll_retry.py`

- [ ] **Step 1: Write failing tests**

Create `tests/test_scheduler_poll_retry.py`:

```python
"""SchedulerService bounded retry for stuck poll_initial generations."""
import pytest
from unittest.mock import AsyncMock, MagicMock
from datetime import date
from uuid import uuid4

from src.engine.services.scheduler_service import SchedulerService
from src.engine.models.task_poll import TaskPoll
from src.engine.models.chat import Chat, ChatType


def make_scheduler(
    pending_polls=None,
    chat_for_poll=None,
    msg_count=0,
    failed_count=0,
    interviewer_user_id=None,
):
    task_poll_storage = AsyncMock()
    task_poll_storage.list_pending_today = AsyncMock(return_value=pending_polls or [])

    chat_storage = AsyncMock()
    chat_storage.get_by_poll_id = AsyncMock(return_value=chat_for_poll)

    message_storage = AsyncMock()
    message_storage.count_by_chat = AsyncMock(return_value=msg_count)
    message_storage.create = AsyncMock()

    agent_run_storage = AsyncMock()
    agent_run_storage.count_failed_by_chat_and_kind = AsyncMock(return_value=failed_count)

    user_storage = AsyncMock()
    user_storage.get_by_username = AsyncMock(
        return_value=MagicMock(id=interviewer_user_id or uuid4()),
    )

    ai_service = AsyncMock()
    ai_service._enqueue_poll_initial = AsyncMock(return_value=uuid4())

    in_app = AsyncMock()
    in_app.create = AsyncMock()

    task_poll_service = AsyncMock()
    task_poll_service.storage = task_poll_storage

    sched = SchedulerService(
        calendar_service=AsyncMock(),
        notification_service=AsyncMock(),
        agent_executor=AsyncMock(),
        role_storage=AsyncMock(),
        user_storage=user_storage,
        org_storage=AsyncMock(),
        task_service=AsyncMock(),
        task_poll_service=task_poll_service,
        task_report_service=AsyncMock(),
    )
    sched.chat_storage = chat_storage
    sched.message_storage = message_storage
    sched.agent_run_storage = agent_run_storage
    sched.ai_service = ai_service
    sched.in_app_notification_service = in_app

    return sched, ai_service, message_storage, in_app


@pytest.mark.asyncio
async def test_retry_publishes_new_request_when_chat_empty_and_under_limit():
    poll = TaskPoll(id=uuid4(), assignee_user_id=uuid4(), poll_date=date.today(),
                    status="pending", org_id=uuid4())
    chat = Chat(id=uuid4(), type=ChatType.POLL, poll_id=poll.id)
    sched, ai_service, msg_storage, in_app = make_scheduler(
        pending_polls=[poll], chat_for_poll=chat, msg_count=0, failed_count=1,
    )

    await sched._retry_stuck_poll_initials()

    ai_service._enqueue_poll_initial.assert_awaited_once()
    msg_storage.create.assert_not_awaited()
    in_app.create.assert_not_awaited()


@pytest.mark.asyncio
async def test_no_retry_after_3_failures_writes_fallback_and_notifies():
    poll = TaskPoll(id=uuid4(), assignee_user_id=uuid4(), poll_date=date.today(),
                    status="pending", org_id=uuid4())
    chat = Chat(id=uuid4(), type=ChatType.POLL, poll_id=poll.id)
    sched, ai_service, msg_storage, in_app = make_scheduler(
        pending_polls=[poll], chat_for_poll=chat, msg_count=0, failed_count=3,
    )

    await sched._retry_stuck_poll_initials()

    ai_service._enqueue_poll_initial.assert_not_awaited()
    msg_storage.create.assert_awaited_once()
    in_app.create.assert_awaited_once()


@pytest.mark.asyncio
async def test_no_retry_when_chat_has_messages():
    poll = TaskPoll(id=uuid4(), assignee_user_id=uuid4(), poll_date=date.today(),
                    status="pending", org_id=uuid4())
    chat = Chat(id=uuid4(), type=ChatType.POLL, poll_id=poll.id)
    sched, ai_service, *_ = make_scheduler(
        pending_polls=[poll], chat_for_poll=chat, msg_count=1, failed_count=0,
    )

    await sched._retry_stuck_poll_initials()

    ai_service._enqueue_poll_initial.assert_not_awaited()
```

- [ ] **Step 2: Add retry method to SchedulerService**

In `src/engine/services/scheduler_service.py` add method `_retry_stuck_poll_initials`:

```python
async def _retry_stuck_poll_initials(self) -> None:
    """For each pending poll where chat has 0 messages: count failed agent_runs.
    If < 3 — republish poll_initial. If >= 3 — write fallback message + in-app notif."""
    if not (self.task_poll_service and self.task_poll_service.storage):
        return

    pending_polls = await self.task_poll_service.storage.list_pending_today()
    for poll in pending_polls:
        try:
            chat = await self.chat_storage.get_by_poll_id(poll.id)
            if chat is None:
                continue

            msg_count = await self.message_storage.count_by_chat(chat.id)
            if msg_count > 0:
                continue  # AI already greeted

            # Count failed agent_runs for this chat with kind='poll_initial'
            failed_count = await self.agent_run_storage.count_failed_by_chat_and_kind(
                chat.id, kind="poll_initial",
            )
            if failed_count >= 3:
                # Fallback path: persist template message + in-app notification
                from src.engine.models.message import Message, SenderType
                interviewer = await self.user_storage.get_by_username("poll_interviewer_ai")
                if interviewer is None:
                    continue
                fallback_text = (
                    "Здравствуйте! Произошла ошибка инициализации. "
                    "Расскажите про свои активные задачи самостоятельно."
                )
                await self.message_storage.create(Message(
                    chat_id=chat.id,
                    sender_id=interviewer.id,
                    sender_type=SenderType.AI_ROLE,
                    content=fallback_text,
                    ai_is_valid=True,
                ))
                await self.in_app_notification_service.create(
                    user_id=poll.assignee_user_id,
                    org_id=poll.org_id,
                    type="system",
                    title="AI временно недоступен",
                    content="Сводка опроса будет собрана из вашего диалога.",
                    reference_type="task_poll",
                    reference_id=poll.id,
                )
                continue

            # Re-enqueue with new request_id
            await self.ai_service._enqueue_poll_initial(
                poll.id, chat.id, interviewer.id,
            )
        except Exception as e:
            logger.error(f"retry stuck poll_initial poll={poll.id}: {e}", exc_info=True)
```

Add storage methods needed:
- `task_poll_storage.list_pending_today() -> List[TaskPoll]` — returns polls with status='pending' AND poll_date=today
- `message_storage.count_by_chat(chat_id) -> int` — count messages in chat
- `agent_run_storage.count_failed_by_chat_and_kind(chat_id, kind) -> int` — count failed runs

(If these methods don't exist, add them with simple SQL.)

Call `_retry_stuck_poll_initials` from scheduler tick alongside other periodic jobs.

- [ ] **Step 3: Run tests**

Run: `cd /root/rugpt && venv/bin/pytest tests/test_scheduler_poll_retry.py -v`
Expected: pass after stubs are filled in.

- [ ] **Step 4: Commit**

```bash
cd /root/rugpt && git add src/engine/services/scheduler_service.py src/engine/storage/task_poll_storage.py src/engine/storage/message_storage.py src/engine/storage/agent_run_storage.py tests/test_scheduler_poll_retry.py
git commit -m "feat(poll): scheduler bounded retry for stuck poll_initial (3 attempts → fallback)"
```

---

## Task 13: TaskReportService uses poll.summary

**Files:**
- Modify: `src/engine/services/task_report_service.py`
- Create: `tests/test_task_report_uses_summary.py`

- [ ] **Step 1: Write failing test**

Create `tests/test_task_report_uses_summary.py`:

```python
"""TaskReportService uses poll.summary in LLM input when present."""
import pytest
from unittest.mock import AsyncMock, MagicMock
from datetime import date
from uuid import uuid4

from src.engine.models.task_poll import TaskPoll


@pytest.mark.asyncio
async def test_report_input_uses_poll_summary_when_present():
    from src.engine.services.task_report_service import TaskReportService

    poll = TaskPoll(
        id=uuid4(), org_id=uuid4(), assignee_user_id=uuid4(),
        poll_date=date.today(), status="completed",
        summary="## По задачам\n- Задача 1: в работе.\n- Задача 2: готова.",
    )
    poll_service = AsyncMock()
    poll_service.list_by_org_and_date = AsyncMock(return_value=[poll])

    storage = AsyncMock()
    storage.create = AsyncMock(side_effect=lambda r: r)
    in_app = AsyncMock()
    in_app.create = AsyncMock()

    user_storage = AsyncMock()
    user_storage.get_by_id = AsyncMock(return_value=MagicMock(name="Иван", username="ivan"))

    service = TaskReportService(
        storage=storage,
        task_poll_service=poll_service,
        in_app_notification_service=in_app,
        user_storage=user_storage,
    )
    # No agent_executor wired — falls back to plain text path with summary block
    report = await service.generate_report(
        org_id=poll.org_id,
        manager_user_id=uuid4(),
        report_date=date.today(),
        user_storage=user_storage,
    )
    assert report is not None
    assert "По задачам" in report.content or "Иван" in report.content
```

- [ ] **Step 2: Run to verify fail or update**

Run: `cd /root/rugpt && venv/bin/pytest tests/test_task_report_uses_summary.py -v`

- [ ] **Step 3: Update _build_llm_input and _fallback_plain_text**

In `src/engine/services/task_report_service.py`, modify both methods to check `poll.summary` first:

In `_build_llm_input`, group entries by assignee. For each assignee, if their poll has `summary` — include it as a block:
```
Сотрудник: <имя>
Сводка опроса:
<summary>
```
Else fall back to current per-task formatting.

In `_fallback_plain_text` — same logic.

(Be careful: the existing methods walk `task_summaries` flat. Need to first reorganize: collect per-assignee, then for each assignee use either summary or task lines.)

Concrete implementation in `_fallback_plain_text`:
```python
@staticmethod
def _fallback_plain_text(
    report_date: date,
    task_summaries: list,
    completed_polls: int,
    total_polls: int,
    expired_polls: int,
    polls: list = None,  # NEW: needs full poll list with summary
) -> str:
    lines = [f"Отчёт за {report_date.isoformat()}", ""]
    lines.append(f"Опросов завершено: {completed_polls} из {total_polls}")
    if expired_polls:
        lines.append(f"Опросов просрочено: {expired_polls}")
    lines.append("")

    # If polls provided and have summaries — use them per-assignee
    if polls:
        assignee_summaries = {
            str(p.assignee_user_id): p.summary
            for p in polls if p.summary
        }
    else:
        assignee_summaries = {}

    if assignee_summaries:
        # Group by assignee
        by_assignee: dict[str, list] = {}
        for s in task_summaries:
            uid = s.get("assignee_user_id", "?")
            by_assignee.setdefault(uid, []).append(s)
        for uid, items in by_assignee.items():
            name = items[0].get("assignee_name", "?")
            lines.append(f"## {name}")
            if uid in assignee_summaries:
                lines.append(assignee_summaries[uid])
            else:
                # Fall back to per-task line format
                for s in items:
                    if s.get("poll_completed"):
                        title = s.get("task_title")
                        status = s.get("new_status", "—")
                        comment = s.get("employee_comment", "")
                        line = f"  «{title or '?'}»: {status}"
                        if comment:
                            line += f" — {comment}"
                        lines.append(line)
                    else:
                        lines.append(f"  опрос не пройден")
            lines.append("")
    else:
        # Original logic
        for s in task_summaries:
            ...

    return "\n".join(lines)
```

Pass `polls` from `generate_report` into `_fallback_plain_text` AND `_build_llm_input`.

- [ ] **Step 4: Run tests**

Run: `cd /root/rugpt && venv/bin/pytest tests/test_task_report_uses_summary.py -v`
Expected: PASS.

- [ ] **Step 5: Run regression suite**

Run: `cd /root/rugpt && venv/bin/pytest tests/ -k report -v`
Expected: existing report tests still pass.

- [ ] **Step 6: Commit**

```bash
cd /root/rugpt && git add src/engine/services/task_report_service.py tests/test_task_report_uses_summary.py
git commit -m "feat(poll): TaskReportService uses poll.summary as input when present"
```

---

## Task 14: WebClient backend — types + adapter command

**Files:**
- Modify: `packages/common/src/types/task-poll.ts`
- Modify: `packages/common/src/types/chat.ts`
- Modify: `packages/backend/src/engine/adapters/rugpt.adapter.ts`
- Modify: `packages/backend/src/task-poll/task-poll.service.ts`
- Modify: `packages/backend/src/task-poll/task-poll.controller.ts`

- [ ] **Step 1: Extend TaskPoll TS type**

In `packages/common/src/types/task-poll.ts` add:

```ts
export interface TaskPoll {
  id: string;
  orgId: string;
  assigneeUserId: string;
  pollDate: string;
  status: string;
  responses: PollResponseItem[];
  createdAt: string;
  completedAt?: string;
  expiresAt?: string;
  // NEW (Task 029):
  summary?: string;
  taskIds?: string[];
}

export interface TodayPollChat {
  chatId: string;
  pollId: string;
  status: string;
}
```

- [ ] **Step 2: Extend ChatType**

In `packages/common/src/types/chat.ts` extend ChatType union/enum to include `'poll'`. Locate `'support'` and add `'poll'` alongside.

- [ ] **Step 3: Add adapter command**

In `packages/backend/src/engine/adapters/rugpt.adapter.ts` add a new branch in `execute()`:

```ts
case 'get_today_poll_chat':
  return this.request('GET', `/api/v1/task-polls/today/chat`, {}, headers);
```

- [ ] **Step 4: Add service method**

In `packages/backend/src/task-poll/task-poll.service.ts`:

```ts
async getTodayChat(user: any): Promise<TodayPollChat | null> {
  const headers = this.buildHeaders(user);
  const [success, data] = await this.engineAdapter.execute('get_today_poll_chat', {}, headers);
  if (!success) return null;
  return {
    chatId: data.chat_id,
    pollId: data.poll_id,
    status: data.status,
  };
}
```

- [ ] **Step 5: Add controller route**

In `packages/backend/src/task-poll/task-poll.controller.ts`:

```ts
@Get('today/chat')
async getTodayChat(@Request() req: any) {
  return this.service.getTodayChat(req.user);
}
```

- [ ] **Step 6: Build to verify TS**

Run: `cd /root/webclient_rugpt && npm run build`
Expected: build succeeds.

- [ ] **Step 7: Commit**

```bash
cd /root/webclient_rugpt && git add packages/common/src/types/task-poll.ts packages/common/src/types/chat.ts packages/backend/src/engine/adapters/rugpt.adapter.ts packages/backend/src/task-poll/task-poll.service.ts packages/backend/src/task-poll/task-poll.controller.ts
git commit -m "feat(poll): TS types + GET /api/task-polls/today/chat proxy"
```

---

## Task 15: Frontend — useTodayPoll hook

**Files:**
- Create: `packages/frontend/src/app/hooks/useTodayPoll.ts`

- [ ] **Step 1: Create hook**

```ts
'use client';

import { useState, useEffect, useCallback } from 'react';
import { useAuthStore } from './useAuth';
import { getApiClient } from '../../transport/apiClient';

interface TodayPollChat {
  chatId: string;
  pollId: string;
  status: string;
}

export function useTodayPoll() {
  const [data, setData] = useState<TodayPollChat | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const token = useAuthStore((s) => s.token);
  const user = useAuthStore((s) => s.user);

  const refetch = useCallback(async () => {
    if (!token) {
      setLoading(false);
      return;
    }
    try {
      setLoading(true);
      setError(null);
      const api = getApiClient();
      const res = await api.signedGet<TodayPollChat>('/api/task-polls/today/chat', user?.id);
      setData(res);
    } catch (err: any) {
      // 404 means no poll today — not an error
      if (err?.status === 404 || err?.response?.status === 404) {
        setData(null);
      } else {
        setError(err instanceof Error ? err.message : 'Unknown error');
      }
    } finally {
      setLoading(false);
    }
  }, [token, user?.id]);

  useEffect(() => { refetch(); }, [refetch]);

  return { data, loading, error, refetch };
}
```

- [ ] **Step 2: Verify TS**

Run: `cd /root/webclient_rugpt/packages/frontend && npx tsc --noEmit`
Expected: 0 errors.

- [ ] **Step 3: Commit**

```bash
cd /root/webclient_rugpt && git add packages/frontend/src/app/hooks/useTodayPoll.ts
git commit -m "feat(poll): useTodayPoll hook to fetch today's poll chat resolution"
```

---

## Task 16: Frontend — PollChat component

**Files:**
- Create: `packages/frontend/src/app/components/PollChat.tsx`

- [ ] **Step 1: Create component**

```tsx
'use client';

import { useState, useEffect, useMemo } from 'react';
import { useAuthStore } from '../hooks/useAuth';
import { useTodayPoll } from '../hooks/useTodayPoll';
import { useChatService } from '../../domain/chat';
import { MessageBubble } from './MessageBubble';
import { ChatInput } from './ChatInput';
import { LoadingAnimation } from './LoadingAnimation';
import { getApiClient } from '../../transport/apiClient';

export function PollChat() {
  const user = useAuthStore((s) => s.user);
  const { data: pollChat, loading: pollLoading, refetch } = useTodayPoll();
  const [submitting, setSubmitting] = useState(false);

  const { messages, sendMessage, isLoading: messagesLoading } = useChatService({
    chatId: pollChat?.chatId ?? null,
    currentUserId: user?.id ?? '',
  });

  const isPending = pollChat?.status === 'pending';
  const hasUserMessages = useMemo(
    () => messages.some((m) => (m as any).senderId === user?.id || m.senderType === 'user'),
    [messages, user?.id],
  );

  const handleSubmit = async () => {
    if (!pollChat) return;
    setSubmitting(true);
    try {
      const api = getApiClient();
      await api.signedPost(`/api/task-polls/${pollChat.pollId}/submit`, {}, user?.id);
      // Wait for chat.events to drive UI; explicit refetch in 2s as backup
      setTimeout(() => refetch(), 2000);
    } catch (e) {
      console.error('Submit failed', e);
    } finally {
      setSubmitting(false);
    }
  };

  if (pollLoading) {
    return <div className="p-6 text-center"><LoadingAnimation /></div>;
  }

  if (!pollChat || !isPending) {
    return (
      <div className="p-8 text-center text-neutral-dark-medium dark:text-gray-400">
        Сегодня нет активных опросов для заполнения.
      </div>
    );
  }

  const isAiPreparing = isPending && messages.length === 0;

  return (
    <div className="flex flex-col h-full">
      {/* Header */}
      <div className="p-4 border-b border-neutral-light-dark dark:border-gray-700 bg-primary-lightest/50 dark:bg-primary/10">
        <div className="max-w-3xl mx-auto flex items-center justify-between">
          <div>
            <span className="text-xs uppercase tracking-wide text-primary dark:text-primary-light">Опрос</span>
            <h1 className="text-lg font-bold text-neutral-dark-darkest dark:text-white">Утренний опрос</h1>
          </div>
          <button
            onClick={handleSubmit}
            disabled={!hasUserMessages || submitting}
            title={!hasUserMessages ? 'Сначала расскажите AI про задачи' : undefined}
            className="bg-indigo-600 text-white px-4 py-2 rounded text-sm disabled:opacity-40"
          >
            {submitting ? 'Завершаем...' : 'Завершить отчёт'}
          </button>
        </div>
      </div>

      {/* Messages */}
      <div className="flex-1 overflow-y-auto px-4 py-4">
        {isAiPreparing && (
          <div className="text-center text-neutral-dark-light dark:text-gray-500 mt-8">
            <LoadingAnimation /> AI готовит сообщение...
          </div>
        )}
        {messages.map((m) => (
          <MessageBubble
            key={m.id}
            id={m.id}
            text={m.content}
            sender={{
              id: m.senderId,
              name: m.senderId === user?.id ? user?.name || 'Я' : 'AI-интервьюер',
              isAI: m.senderType === 'ai_role',
            }}
            timestamp={new Date(m.createdAt)}
            isUser={m.senderId === user?.id}
          />
        ))}
      </div>

      {/* Input */}
      <ChatInput onSend={async (text) => { await sendMessage(text); }} />
    </div>
  );
}
```

(Adapt prop names if Message type differs.)

- [ ] **Step 2: Verify TS compiles**

Run: `cd /root/webclient_rugpt/packages/frontend && npx tsc --noEmit`
Expected: 0 errors.

- [ ] **Step 3: Commit**

```bash
cd /root/webclient_rugpt && git add packages/frontend/src/app/components/PollChat.tsx
git commit -m "feat(poll): PollChat component with submit button + AI preparing state"
```

---

## Task 17: Frontend — tabs in MainChat + URL param

**Files:**
- Modify: `packages/frontend/src/app/components/MainChat.tsx`

- [ ] **Step 1: Read current MainChat structure**

Read `packages/frontend/src/app/components/MainChat.tsx`. Note the JSX skeleton.

- [ ] **Step 2: Add tabs above chat content**

Add at top of MainChat render, just inside the main content container:

```tsx
import { useSearchParams, useRouter } from 'next/navigation';
import { PollChat } from './PollChat';
import { useTodayPoll } from '../hooks/useTodayPoll';

// ...inside component...

const router = useRouter();
const searchParams = useSearchParams();
const tabFromUrl = searchParams.get('tab');
const initialTab = tabFromUrl === 'poll' ? 'poll' : 'ai';
const [activeTab, setActiveTab] = useState<'ai' | 'poll'>(initialTab);

const { data: todayPoll } = useTodayPoll();
const showPollTab = !currentUser.isAdmin && todayPoll != null;

const switchTab = (t: 'ai' | 'poll') => {
  setActiveTab(t);
  const params = new URLSearchParams(searchParams.toString());
  if (t === 'poll') params.set('tab', 'poll');
  else params.delete('tab');
  router.replace(`/?${params.toString()}`);
};
```

In the JSX (inside ChatNavigation area or above main content):

```tsx
{showPollTab && (
  <div className="border-b border-neutral-light-dark dark:border-gray-700 px-4">
    <div className="max-w-3xl mx-auto flex gap-4">
      <button
        onClick={() => switchTab('ai')}
        className={`py-3 text-sm border-b-2 transition-colors ${
          activeTab === 'ai'
            ? 'border-primary text-primary dark:text-primary-light'
            : 'border-transparent text-neutral-dark-medium dark:text-gray-400 hover:text-neutral-dark-darkest dark:hover:text-white'
        }`}
      >
        Мой ИИ
      </button>
      <button
        onClick={() => switchTab('poll')}
        className={`py-3 text-sm border-b-2 transition-colors ${
          activeTab === 'poll'
            ? 'border-primary text-primary dark:text-primary-light'
            : 'border-transparent text-neutral-dark-medium dark:text-gray-400 hover:text-neutral-dark-darkest dark:hover:text-white'
        }`}
      >
        Опрос
      </button>
    </div>
  </div>
)}

{activeTab === 'poll' ? <PollChat /> : (/* existing AI chat content */)}
```

- [ ] **Step 3: Verify TS**

Run: `cd /root/webclient_rugpt/packages/frontend && npx tsc --noEmit`
Expected: 0 errors.

- [ ] **Step 4: Commit**

```bash
cd /root/webclient_rugpt && git add packages/frontend/src/app/components/MainChat.tsx
git commit -m "feat(poll): tabs in MainChat — Мой ИИ / Опрос (poll tab for non-admins)"
```

---

## Task 18: Frontend — NotificationDropdown href update

**Files:**
- Modify: `packages/frontend/src/app/components/NotificationDropdown.tsx`

- [ ] **Step 1: Update notificationHref for task_poll**

Locate `notificationHref` function. Change `task_poll` branch:

```ts
case 'task_poll':
  return '/?tab=poll';
```

(Keep `task_report` as `/tasks` — handled in a future iteration.)

- [ ] **Step 2: Verify TS**

Run: `cd /root/webclient_rugpt/packages/frontend && npx tsc --noEmit`
Expected: 0 errors.

- [ ] **Step 3: Commit**

```bash
cd /root/webclient_rugpt && git add packages/frontend/src/app/components/NotificationDropdown.tsx
git commit -m "feat(poll): notificationHref for task_poll → /?tab=poll"
```

---

## Task 19: Tech-debt entry — remove KAFKA_ENABLED=false

**Files:**
- Modify: `docs/tech-debt.md`

- [ ] **Step 1: Append tech-debt entry**

Add new section at the end of `/root/rugpt/docs/tech-debt.md`:

```markdown
## Remove `KAFKA_ENABLED=false` fallback mode

**Priority:** Medium
**Component:** Engine

### Current state

`Config.KAFKA_ENABLED=false` switches `AIService.try_auto_respond`, `AIService.process_ai_mentions`, and other agentic flows into synchronous mode (block HTTP request until LLM finishes). This was introduced for tests and graceful degradation.

### Why remove

1. Doubled maintenance: every agent feature must be implemented in two modes, leading to two test paths per scenario.
2. Dev/prod divergence: dev typically runs without Kafka, prod with — bugs surface only on prod.
3. Sync fallback is a poor fit for proactive scheduler-driven LLM calls (blocks background tasks).
4. False sense of fault tolerance: if Kafka is down, `chat.events` also fails, so AI responses won't reach WS clients regardless. "Graceful degradation" only helps tests.

### Target

- Kafka becomes a hard dependency, like PostgreSQL.
- Local dev brings Kafka up via `docker-compose.kafka.yml` (already present, ~30s startup).
- Tests use testcontainers with KRaft single-node, or moctan kafka_producer at unit level.

### Scope

- Remove sync paths from `AIService.try_auto_respond` and `AIService.process_ai_mentions`.
- Remove `Config.KAFKA_ENABLED` and all branches reading it.
- Update test fixtures to either mock kafka_producer or use testcontainers.
- Update `setup.sh` / `local_restart.sh` to enforce Kafka up.

### When

Separate task. Poll AI dialog feature (spec 2026-04-30) is written Kafka-only and does not regress this debt.
```

- [ ] **Step 2: Commit**

```bash
cd /root/rugpt && git add docs/tech-debt.md
git commit -m "docs(tech-debt): track KAFKA_ENABLED=false removal as separate task"
```

---

## Task 20: Manual smoke test

**Files:**
- None (manual verification)

- [ ] **Step 1: Apply migrations on dev**

Run on dev server: `cd /root/rugpt && ./migrate.sh`
Expected: 029 applied successfully.

- [ ] **Step 2: Restart engine**

Run: `cd /root/rugpt && ./local_restart.sh` (or via systemd)
Expected: engine starts without errors.

- [ ] **Step 3: Manually trigger create_daily_poll for a test user**

Open Python shell with engine env, run:

```python
from src.engine.services.engine_service import EngineService
import asyncio
from uuid import UUID

async def main():
    engine = EngineService()
    await engine.initialize()
    poll = await engine.task_poll_service.create_daily_poll(
        assignee_user_id=UUID('<test-user-id>'),
        org_id=UUID('<test-org-id>'),
    )
    print('Poll:', poll)

asyncio.run(main())
```

Expected: poll created, chat created, agent_run pending in DB.

- [ ] **Step 4: Wait for poll_initial Kafka processing, observe AI greeting in chat**

Query DB:
```sql
SELECT m.content FROM messages m
JOIN chats c ON c.id = m.chat_id
WHERE c.poll_id = '<poll-id>'
ORDER BY m.created_at;
```
Expected: at least one AI greeting message present.

- [ ] **Step 5: Open frontend, navigate to /?tab=poll, verify dialog UI**

Manually browse to `https://rugpt.pro/?tab=poll` as the test user.
Expected: PollChat tab visible, AI message rendered, ChatInput active, "Завершить отчёт" disabled until user types.

- [ ] **Step 6: Submit dialog and verify summary**

Type a few replies. Click "Завершить отчёт". Wait for "Отчёт сдан" message to appear via WS.
Query DB:
```sql
SELECT summary, status, completed_at FROM task_polls WHERE id = '<poll-id>';
```
Expected: `summary` non-null markdown text, `status='completed'`, `completed_at` set.

- [ ] **Step 7: Trigger evening report generation, verify it uses summary**

Manually invoke `task_report_service.generate_report` for a manager. Inspect `task_reports.content` — should reflect the summary content per assignee.

- [ ] **Step 8: Document any issues, file follow-up tasks**

If smoke test reveals bugs — file as separate tasks, fix iteratively.

---

## Self-Review

After completing all tasks above, run final regression suite:

```bash
cd /root/rugpt && venv/bin/pytest tests/ -v
cd /root/webclient_rugpt && npm run build && npm run test
```

Expected: 0 regressions.
