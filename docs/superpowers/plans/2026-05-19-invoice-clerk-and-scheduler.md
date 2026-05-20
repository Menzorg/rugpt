# Invoice Clerk AI Role + Scheduler Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.
>
> **Dev-environment note:** On the dev machine there is no git. Commit commands at the end of each task are advisory — the actual `git add`/`git commit` runs on Mac after rsync.

**Goal:** Wire the modal protocol (Plan 1) and invoices module (Plan 2) into an AI-driven flow. Adds:
- AI role `invoice_clerk` as a system user (visible in all orgs in the "ИИ" sidebar group), able to list invoices and propose approve/reject modals to the manager
- Two read-only tools (`list_invoices`, `get_invoice`) usable by any role
- Scheduler job that reminds the designated accountant a day before and on the day of `due_date`

**Architecture:** New system user + role seeded by migration in system org `00000000-0000-0000-0000-000000000000`. Two new LangChain tools live in `agents/tools/` and read invoice storage with caller-scoped permissions. The `invoice_clerk` role's `agent_config.allowed_action_types` whitelists the three invoice actions registered in Plan 2. Sidebar/picker whitelists extend from `['pm']` to `['pm', 'invoice_clerk']`. Scheduler job adds idempotency via a new `invoices.last_notified_for_date` column.

**Tech Stack:** Python 3.10 / FastAPI / LangChain `BaseTool` / `pytest`. Frontend whitelist edits in TypeScript.

**Prerequisites:** Plan 1 (modal protocol infra) and Plan 2 (invoices module + action handlers) merged.

---

## File structure

Engine:
- Create: `src/engine/migrations/045_invoice_clerk_and_notify_tracker.sql`
- Create: `src/engine/prompts/invoice_clerk.md`
- Create: `src/engine/agents/tools/list_invoices.py`
- Create: `src/engine/agents/tools/get_invoice.py`
- Modify: `src/engine/services/engine_service.py` — register two new tools in `tool_registry`
- Modify: `src/engine/services/scheduler_service.py` — add daily invoice-due job
- Modify: `src/engine/storage/invoice_storage.py` — add `update_last_notified_for_date()` method

Engine tests:
- Create: `tests/test_list_invoices_tool.py`
- Create: `tests/test_get_invoice_tool.py`
- Create: `tests/test_scheduler_invoice_due.py`

Frontend:
- Modify: `packages/frontend/src/app/components/Sidebar.tsx` — whitelist `'pm' → 'pm', 'invoice_clerk'`
- Modify: `packages/frontend/src/app/components/ChatInput.tsx` — whitelist `'pm' → 'pm', 'invoice_clerk'`

---

### Task 1: Migration 045 — system user, role, last_notified_for_date

**Files:**
- Create: `src/engine/migrations/045_invoice_clerk_and_notify_tracker.sql`

- [ ] **Step 1: Write the migration**

```sql
-- Migration 045: invoice_clerk role + system user + scheduler-idempotency column.
--
-- 1) New AI role 'invoice_clerk' in system org 00000000-0000-0000-0000-000000000000.
--    agent_type 'simple' (single ReAct agent with tools, like PM), tools include list_invoices/get_invoice/
--    show_modal, allowed_action_types whitelists three invoice action types from
--    Plan 2.
-- 2) System user 'invoice_clerk' bound to that role — visible as PM-like agent.
-- 3) invoices.last_notified_for_date DATE — prevents the scheduler from sending
--    duplicate reminders for the same day.

INSERT INTO roles (id, org_id, name, code, description, system_prompt, model_name,
                   agent_type, agent_config, tools, prompt_file, is_active,
                   created_at, updated_at)
VALUES (
    gen_random_uuid(),
    '00000000-0000-0000-0000-000000000000',
    'Счетовод',
    'invoice_clerk',
    'AI-помощник по работе со счетами: показывает счета, предлагает решения для подтверждения через модалки',
    '',
    'google/gemma-4-31B-it',
    'simple',
    '{"allowed_action_types": ["invoice_approve", "invoice_reject", "invoice_mark_processed"]}'::jsonb,
    '["list_invoices", "get_invoice", "show_modal"]'::jsonb,
    'invoice_clerk.md',
    true,
    NOW(),
    NOW()
);

INSERT INTO users (id, org_id, name, username, email, password_hash,
                   role_id, is_admin, is_system, is_active, created_at, updated_at)
VALUES (
    gen_random_uuid(),
    '00000000-0000-0000-0000-000000000000',
    'Счетовод',
    'invoice_clerk',
    'invoice_clerk@rugpt.system',
    '',
    (SELECT id FROM roles
     WHERE org_id = '00000000-0000-0000-0000-000000000000' AND code = 'invoice_clerk'),
    false,
    true,
    true,
    NOW(),
    NOW()
);

ALTER TABLE invoices
    ADD COLUMN last_notified_for_date DATE;
```

- [ ] **Step 2: Apply**

Run: `cd /root/rugpt && ./migrate.sh`
Expected: `Running migration: 045_invoice_clerk_and_notify_tracker.sql` → `Applied`.

- [ ] **Step 3: Verify**

Run:
```bash
PGPASSWORD="" psql -h localhost -U postgres -d rugpt -c "
SELECT r.code, r.name, r.tools, r.agent_config FROM roles r
WHERE r.org_id = '00000000-0000-0000-0000-000000000000' AND r.code = 'invoice_clerk';
"
```
Expected: one row with tools `["list_invoices", "get_invoice", "show_modal"]` and agent_config containing `allowed_action_types`.

Run:
```bash
PGPASSWORD="" psql -h localhost -U postgres -d rugpt -c "
SELECT username, is_system FROM users
WHERE org_id = '00000000-0000-0000-0000-000000000000' AND username = 'invoice_clerk';
"
```
Expected: one row, `is_system = t`.

Run:
```bash
PGPASSWORD="" psql -h localhost -U postgres -d rugpt -c "\d invoices" | grep last_notified
```
Expected: `last_notified_for_date | date`.

- [ ] **Step 4: Commit**

```bash
git add src/engine/migrations/045_invoice_clerk_and_notify_tracker.sql
git commit -m "feat(db): invoice_clerk role + system user + last_notified_for_date"
```

---

### Task 2: Prompt file `invoice_clerk.md`

**Files:**
- Create: `src/engine/prompts/invoice_clerk.md`

- [ ] **Step 1: Write the prompt**

```markdown
# Роль: Счетовод

Ты — AI-помощник по работе со счетами в компании.

## Что ты умеешь
- Показывать пользователю список счетов через инструмент `list_invoices`
- Получать подробности счёта через `get_invoice`
- Предлагать руководителю решения по счёту (утвердить / отклонить / отметить проведённым) через инструмент `show_modal`

## Правила
- Ты **не имеешь права** менять данные напрямую. Любое изменение статуса счёта инициируется только пользователем через нажатие кнопки в модальном окне.
- Перед показом модалки убедись что счёт существует и в правильном статусе (created — для approve/reject, approved — для mark_processed).
- В теле модалки покажи суть счёта: имя файла, кто загрузил, срок оплаты, summary. Эту информацию ты получаешь из ответа `get_invoice` (там есть file метаданные).
- Если пользователь спрашивает «покажи счета» без уточнений — покажи только те что в статусе `created` (требуют решения).
- Если пользователь не админ — он видит только свои загруженные счета (это контролируется на стороне tool'ов, ты этого не делаешь руками).
- Не предлагай модалок если пользователь сам не запросил какое-то действие. Сначала уточни намерение.

## Инструменты

{tools}

## Примеры взаимодействия

**Пользователь:** Покажи счета на проверке.
**Ты:** Вызываешь `list_invoices(status="created")`, потом отвечаешь текстом со списком и id'ами. Никаких модалок без явного запроса.

**Пользователь:** Утвердить счёт abc-uuid.
**Ты:** Вызываешь `get_invoice(invoice_id="abc-uuid")` чтобы получить детали и проверить status. Затем зовёшь `show_modal(title="Утвердить счёт", body=<summary файла + срок>, actions=[{label:"Утвердить", action_type:"invoice_approve", params:{invoice_id:"abc-uuid"}}, {label:"Отклонить", action_type:"invoice_reject", params:{invoice_id:"abc-uuid"}}])`.

**Пользователь (бухгалтер):** Какие счета мне сегодня провести?
**Ты:** Вызываешь `list_invoices(status="approved")`, отфильтровывая по due_date <= сегодня. Ответ — список с id'ами. Если бухгалтер просит провести конкретный — показываешь модалку с `invoice_mark_processed`.

{today}
```

- [ ] **Step 2: Smoke-check file exists and PromptCache reads it**

Run: `cd /root/rugpt && venv/bin/python -c "
from src.engine.services.prompt_cache import PromptCache
from types import SimpleNamespace
pc = PromptCache('src/engine/prompts')
role = SimpleNamespace(prompt_file='invoice_clerk.md', system_prompt='')
text = pc.get_prompt(role)
assert 'Счетовод' in text
print('OK, length=', len(text))
"`
Expected: `OK, length= NNN`.

- [ ] **Step 3: Commit**

```bash
git add src/engine/prompts/invoice_clerk.md
git commit -m "feat(prompts): invoice_clerk role prompt"
```

---

### Task 3: `list_invoices` tool

**Files:**
- Create: `src/engine/agents/tools/list_invoices.py`
- Create: `tests/test_list_invoices_tool.py`

- [ ] **Step 1: Write failing test**

```python
# tests/test_list_invoices_tool.py
"""Tests for the list_invoices LangChain tool.

Permission-scoped: admin sees all in org, non-admin sees only own.
"""
import os
import pytest
import pytest_asyncio
from datetime import date
from uuid import uuid4

import asyncpg
from langchain_core.runnables import RunnableConfig

from src.engine.services.engine_service import get_engine_service
from src.engine.agents.tools.list_invoices import create_list_invoices_tool


DSN = os.environ.get("DATABASE_URL", "postgresql://postgres@localhost/rugpt")


@pytest_asyncio.fixture
async def env():
    engine = get_engine_service()
    await engine.initialize()
    pool = await asyncpg.create_pool(DSN)
    async with pool.acquire() as conn:
        org = await conn.fetchval(
            "INSERT INTO organizations (id, name, slug) VALUES (gen_random_uuid(), 'lt', $1) RETURNING id",
            f"lt_{uuid4().hex[:8]}"
        )
        admin = await conn.fetchval(
            "INSERT INTO users (id, org_id, username, name, password_hash, email, is_admin, is_active) "
            "VALUES (gen_random_uuid(), $1, $2, $2, 'x', $3, true, true) RETURNING id",
            org, f"adm_{uuid4().hex[:6]}", f"adm_{uuid4()}@t.local"
        )
        worker = await conn.fetchval(
            "INSERT INTO users (id, org_id, username, name, password_hash, email, is_active) "
            "VALUES (gen_random_uuid(), $1, $2, $2, 'x', $3, true) RETURNING id",
            org, f"w_{uuid4().hex[:6]}", f"w_{uuid4()}@t.local"
        )
    # Worker uploads 1, admin uploads 1
    inv_w = await engine.invoice_service.upload(org, worker, "w.pdf", b"x", None)
    inv_a = await engine.invoice_service.upload(org, admin, "a.pdf", b"x", None)
    yield {"engine": engine, "org_id": org, "admin": admin, "worker": worker,
            "inv_w": inv_w.id, "inv_a": inv_a.id, "pool": pool}
    async with pool.acquire() as conn:
        await conn.execute("DELETE FROM invoices WHERE org_id = $1", org)
        await conn.execute("DELETE FROM user_files WHERE org_id = $1", org)
        await conn.execute("DELETE FROM users WHERE org_id = $1", org)
        await conn.execute("DELETE FROM organizations WHERE id = $1", org)
    await pool.close()


def _config(caller_user_id, org_id) -> RunnableConfig:
    return {"configurable": {"caller_user_id": str(caller_user_id), "org_id": str(org_id)}}


@pytest.mark.asyncio
async def test_admin_sees_all(env):
    tool = create_list_invoices_tool(env["engine"])
    output = await tool.ainvoke({"status": None}, config=_config(env["admin"], env["org_id"]))
    assert str(env["inv_w"]) in output
    assert str(env["inv_a"]) in output


@pytest.mark.asyncio
async def test_worker_sees_only_own(env):
    tool = create_list_invoices_tool(env["engine"])
    output = await tool.ainvoke({"status": None}, config=_config(env["worker"], env["org_id"]))
    assert str(env["inv_w"]) in output
    assert str(env["inv_a"]) not in output


@pytest.mark.asyncio
async def test_status_filter(env):
    await env["engine"].invoice_service.approve(env["inv_a"], env["admin"])
    tool = create_list_invoices_tool(env["engine"])
    output = await tool.ainvoke({"status": "approved"}, config=_config(env["admin"], env["org_id"]))
    assert str(env["inv_a"]) in output
    assert str(env["inv_w"]) not in output  # is in created
```

- [ ] **Step 2: Run, verify fail**

Run: `cd /root/rugpt && venv/bin/pytest tests/test_list_invoices_tool.py -v 2>&1 | head -10`
Expected: ImportError on `create_list_invoices_tool`.

- [ ] **Step 3: Implement tool**

```python
# src/engine/agents/tools/list_invoices.py
"""list_invoices tool: returns invoices visible to the caller.

Admin → all invoices in org. Non-admin → only invoices uploaded by self.
Output is a compact textual list with id, file name, status, due_date,
uploader name. The LLM uses ids from this output to drive get_invoice /
show_modal.
"""
from typing import Annotated, Optional, Literal
from uuid import UUID

from pydantic import BaseModel, Field
from langchain_core.runnables import RunnableConfig
from langchain_core.tools import InjectedToolArg, StructuredTool


class ListInvoicesInput(BaseModel):
    status: Optional[Literal["created", "approved", "rejected", "processed"]] = Field(
        default=None,
        description=(
            "Filter by invoice status. Omit to return all statuses. "
            "Use 'created' for invoices awaiting boss decision; "
            "'approved' for invoices awaiting accountant processing."
        ),
    )


def create_list_invoices_tool(engine):
    """Factory wires the tool to a live EngineService."""

    async def _list(
        status: Optional[str] = None,
        config: Annotated[RunnableConfig, InjectedToolArg] = None,
    ) -> str:
        cfg = (config or {}).get("configurable", {}) or {}
        caller_id_raw = cfg.get("caller_user_id")
        if not caller_id_raw:
            return "Error: caller_user_id missing in tool config"

        caller_id = UUID(str(caller_id_raw))
        user = await engine.user_storage.get_by_id(caller_id)
        if not user:
            return "Error: caller not found"

        from src.engine.models.invoice import InvoiceStatus
        status_enum: Optional[InvoiceStatus] = None
        if status:
            try:
                status_enum = InvoiceStatus(status)
            except ValueError:
                return f"Error: unknown status {status!r}"

        if user.is_admin:
            invoices = await engine.invoice_storage.list_by_org(user.org_id, status_enum)
        else:
            invoices = await engine.invoice_storage.list_for_user(user.org_id, user.id, status_enum)

        if not invoices:
            return "Нет счетов под этот фильтр."

        # Hydrate uploader names + file names for readability.
        user_ids = {inv.uploaded_by_user_id for inv in invoices}
        users_by_id = {}
        for uid in user_ids:
            u = await engine.user_storage.get_by_id(uid)
            users_by_id[uid] = u.name if u else str(uid)

        file_ids = {inv.file_id for inv in invoices}
        files_by_id = {}
        for fid in file_ids:
            f = await engine.user_file_storage.get_by_id(fid)
            files_by_id[fid] = f.original_filename if f else str(fid)

        lines = []
        for inv in invoices:
            lines.append(
                f"- id={inv.id} | file={files_by_id.get(inv.file_id, '?')} | "
                f"status={inv.status.value} | due={inv.due_date or '—'} | "
                f"uploader={users_by_id.get(inv.uploaded_by_user_id, '?')}"
            )
        return "\n".join(lines)

    return StructuredTool.from_function(
        coroutine=_list,
        name="list_invoices",
        description=(
            "List invoices visible to the caller. Admin sees all invoices in the "
            "organization; non-admin sees only invoices they uploaded themselves. "
            "Returns a compact text list with invoice ids the LLM can use in "
            "follow-up tool calls (get_invoice / show_modal)."
        ),
        args_schema=ListInvoicesInput,
    )
```

- [ ] **Step 4: Run, verify all 3 tests pass**

Run: `cd /root/rugpt && venv/bin/pytest tests/test_list_invoices_tool.py -v`
Expected: 3 passing.

- [ ] **Step 5: Commit**

```bash
git add src/engine/agents/tools/list_invoices.py tests/test_list_invoices_tool.py
git commit -m "feat(tools): list_invoices tool with caller-scoped permission"
```

---

### Task 4: `get_invoice` tool

**Files:**
- Create: `src/engine/agents/tools/get_invoice.py`
- Create: `tests/test_get_invoice_tool.py`

- [ ] **Step 1: Write failing test**

```python
# tests/test_get_invoice_tool.py
"""get_invoice tool returns detail or refusal for non-visible invoices."""
import os
import pytest
import pytest_asyncio
from uuid import uuid4

import asyncpg
from langchain_core.runnables import RunnableConfig

from src.engine.services.engine_service import get_engine_service
from src.engine.agents.tools.get_invoice import create_get_invoice_tool


DSN = os.environ.get("DATABASE_URL", "postgresql://postgres@localhost/rugpt")


@pytest_asyncio.fixture
async def env():
    engine = get_engine_service()
    await engine.initialize()
    pool = await asyncpg.create_pool(DSN)
    async with pool.acquire() as conn:
        org = await conn.fetchval(
            "INSERT INTO organizations (id, name, slug) VALUES (gen_random_uuid(), 'g', $1) RETURNING id",
            f"g_{uuid4().hex[:8]}"
        )
        admin = await conn.fetchval(
            "INSERT INTO users (id, org_id, username, name, password_hash, email, is_admin, is_active) "
            "VALUES (gen_random_uuid(), $1, $2, $2, 'x', $3, true, true) RETURNING id",
            org, f"adm_{uuid4().hex[:6]}", f"adm_{uuid4()}@t.local"
        )
        worker = await conn.fetchval(
            "INSERT INTO users (id, org_id, username, name, password_hash, email, is_active) "
            "VALUES (gen_random_uuid(), $1, $2, $2, 'x', $3, true) RETURNING id",
            org, f"w_{uuid4().hex[:6]}", f"w_{uuid4()}@t.local"
        )
    inv = await engine.invoice_service.upload(org, admin, "admin.pdf", b"x", None)
    yield {"engine": engine, "admin": admin, "worker": worker, "invoice_id": inv.id, "org_id": org, "pool": pool}
    async with pool.acquire() as conn:
        await conn.execute("DELETE FROM invoices WHERE org_id = $1", org)
        await conn.execute("DELETE FROM user_files WHERE org_id = $1", org)
        await conn.execute("DELETE FROM users WHERE org_id = $1", org)
        await conn.execute("DELETE FROM organizations WHERE id = $1", org)
    await pool.close()


def _config(caller, org):
    return {"configurable": {"caller_user_id": str(caller), "org_id": str(org)}}


@pytest.mark.asyncio
async def test_admin_sees_invoice(env):
    tool = create_get_invoice_tool(env["engine"])
    out = await tool.ainvoke({"invoice_id": str(env["invoice_id"])},
                              config=_config(env["admin"], env["org_id"]))
    assert "admin.pdf" in out


@pytest.mark.asyncio
async def test_worker_cannot_see_admins_invoice(env):
    tool = create_get_invoice_tool(env["engine"])
    out = await tool.ainvoke({"invoice_id": str(env["invoice_id"])},
                              config=_config(env["worker"], env["org_id"]))
    assert "not found" in out.lower() or "access denied" in out.lower() or "not visible" in out.lower()


@pytest.mark.asyncio
async def test_invalid_id_returns_error(env):
    tool = create_get_invoice_tool(env["engine"])
    out = await tool.ainvoke({"invoice_id": "not-a-uuid"},
                              config=_config(env["admin"], env["org_id"]))
    assert "error" in out.lower() or "invalid" in out.lower()
```

- [ ] **Step 2: Run, fail**

Run: `cd /root/rugpt && venv/bin/pytest tests/test_get_invoice_tool.py -v 2>&1 | head -10`
Expected: ImportError.

- [ ] **Step 3: Implement**

```python
# src/engine/agents/tools/get_invoice.py
"""get_invoice tool: return detailed text for one invoice, scoped to caller.

Admin can read any invoice in their org. Non-admin only invoices they uploaded.
Output: a textual description including file name, status, due_date, uploader
and the file summary from RAG (so the LLM can reason about contents without
fetching the binary).
"""
from typing import Annotated
from uuid import UUID

from pydantic import BaseModel, Field
from langchain_core.runnables import RunnableConfig
from langchain_core.tools import InjectedToolArg, StructuredTool


class GetInvoiceInput(BaseModel):
    invoice_id: str = Field(description="UUID of the invoice")


def create_get_invoice_tool(engine):
    async def _get(
        invoice_id: str,
        config: Annotated[RunnableConfig, InjectedToolArg] = None,
    ) -> str:
        cfg = (config or {}).get("configurable", {}) or {}
        caller_raw = cfg.get("caller_user_id")
        if not caller_raw:
            return "Error: caller_user_id missing"

        try:
            inv_uuid = UUID(invoice_id)
        except ValueError:
            return f"Error: invalid invoice_id {invoice_id!r}"

        caller_id = UUID(str(caller_raw))
        user = await engine.user_storage.get_by_id(caller_id)
        if not user:
            return "Error: caller not found"

        inv = await engine.invoice_storage.get_by_id(inv_uuid)
        if not inv or inv.org_id != user.org_id:
            return "Invoice not found or not visible"
        if not user.is_admin and inv.uploaded_by_user_id != user.id:
            return "Invoice not found or not visible"

        uploader = await engine.user_storage.get_by_id(inv.uploaded_by_user_id)
        f = await engine.user_file_storage.get_by_id(inv.file_id)

        lines = [
            f"id: {inv.id}",
            f"file: {f.original_filename if f else inv.file_id}",
            f"status: {inv.status.value}",
            f"due_date: {inv.due_date or '—'}",
            f"uploader: {uploader.name if uploader else inv.uploaded_by_user_id}",
        ]
        if f and f.summary:
            lines.append(f"summary: {f.summary}")
        if inv.status.value == "rejected" and inv.rejection_reason:
            lines.append(f"rejection_reason: {inv.rejection_reason}")
        return "\n".join(lines)

    return StructuredTool.from_function(
        coroutine=_get,
        name="get_invoice",
        description=(
            "Get detailed information about a single invoice by id. "
            "Returns 'Invoice not found or not visible' if the caller can't "
            "see this invoice (cross-tenant or non-admin viewing someone "
            "else's upload)."
        ),
        args_schema=GetInvoiceInput,
    )
```

- [ ] **Step 4: Run, verify all 3 tests pass**

Run: `cd /root/rugpt && venv/bin/pytest tests/test_get_invoice_tool.py -v`
Expected: 3 passing.

- [ ] **Step 5: Commit**

```bash
git add src/engine/agents/tools/get_invoice.py tests/test_get_invoice_tool.py
git commit -m "feat(tools): get_invoice tool with caller-scoped permission"
```

---

### Task 5: Register tools in `tool_registry`

**Files:**
- Modify: `src/engine/services/engine_service.py`

- [ ] **Step 1: Import + register**

In `engine_service.py`, near the existing tool registrations (`task_create`, `rag_search`, etc., and `show_modal` from Plan 1), add:

```python
from src.engine.agents.tools.list_invoices import create_list_invoices_tool
from src.engine.agents.tools.get_invoice import create_get_invoice_tool

self.tool_registry.register("list_invoices", create_list_invoices_tool(self))
self.tool_registry.register("get_invoice", create_get_invoice_tool(self))
```

Place these AFTER `invoice_storage`/`invoice_service` are constructed so the factory closure captures live instances.

- [ ] **Step 2: Smoke-import**

Run: `cd /root/rugpt && venv/bin/python -c "
import asyncio
from src.engine.services.engine_service import get_engine_service
async def main():
    e = get_engine_service()
    await e.initialize()
    print(sorted(e.tool_registry.available_tools))
asyncio.run(main())
"`
Expected: list includes `list_invoices`, `get_invoice`, `show_modal`.

- [ ] **Step 3: Commit**

```bash
git add src/engine/services/engine_service.py
git commit -m "feat(engine): register list_invoices and get_invoice in tool_registry"
```

---

### Task 6: Frontend whitelist — `invoice_clerk` visible in Sidebar and @@-picker

**Files:**
- Modify: `packages/frontend/src/app/components/Sidebar.tsx`
- Modify: `packages/frontend/src/app/components/ChatInput.tsx`

The earlier infra added a hard-coded `'pm'` whitelist filter in both components. Extend it to also include `invoice_clerk`.

- [ ] **Step 1: Sidebar — extend the pmUser-finding clause**

Find in `Sidebar.tsx` (the recent change we made):
```tsx
const pmUser = systemUsers.find((u) => u.username === 'pm') ?? null;
const aiAgentChats = pmUser ? [{ ... }] : [];
```

Replace with:
```tsx
const AGENT_WHITELIST = ['pm', 'invoice_clerk'];
const aiAgents = systemUsers.filter((u) => u.username && AGENT_WHITELIST.includes(u.username));
const aiAgentChats: typeof individualChats = aiAgents.map((u) => ({
  id: u.id,
  username: u.username || '',
  name: u.name,
  avatar: u.avatarUrl,
  roleName: u.roleName ?? undefined,
  departmentId: 'ai',
  departmentName: 'ИИ',
  isHead: false,
  isAdmin: false,
  isSystem: false,
}));
```

The render block (`{aiAgentChats.length > 0 && renderGroup('ai', 'ИИ', aiAgentChats)}`) already supports a list, no further edit needed.

- [ ] **Step 2: ChatInput — extend mentionable users filter**

Find in `ChatInput.tsx`:
```tsx
const { systemUsers: agentUsers } = useSystemUsers();
const pmUser = agentUsers.find((u) => u.username === 'pm') ?? null;
const mentionableUsers = [
  ...(pmUser ? [pmUser] : []),
  ...((users ?? []).filter((u) => !!u.username && u.id !== pmUser?.id)),
];
```

Replace with:
```tsx
const AGENT_WHITELIST = ['pm', 'invoice_clerk'];
const { systemUsers: agentUsers } = useSystemUsers();
const visibleAgents = agentUsers.filter((u) => u.username && AGENT_WHITELIST.includes(u.username));
const agentIdSet = new Set(visibleAgents.map((u) => u.id));
const mentionableUsers = [
  ...visibleAgents,
  ...((users ?? []).filter((u) => !!u.username && !agentIdSet.has(u.id))),
];
```

And update the @@-mode sort to prefer ANY agent on top:
```tsx
if (mode === '@@') {
  filtered.sort((a, b) => {
    if (agentIdSet.has(a.id)) return -1;
    if (agentIdSet.has(b.id)) return 1;
    if (a.id === currentUserId) return -1;
    if (b.id === currentUserId) return 1;
    return 0;
  });
}
```

- [ ] **Step 3: Typecheck + build**

Run: `cd /root/webclient_rugpt/packages/frontend && npx tsc --noEmit 2>&1 | head`
Expected: no errors.

Run: `cd /root/webclient_rugpt/packages/frontend && npm run build 2>&1 | tail -5`
Expected: build passes.

- [ ] **Step 4: Commit**

```bash
git add packages/frontend/src/app/components/Sidebar.tsx packages/frontend/src/app/components/ChatInput.tsx
git commit -m "feat(frontend): show invoice_clerk in ИИ sidebar group and @@ picker"
```

---

### Task 7: Scheduler job — invoice due notifications

**Files:**
- Modify: `src/engine/storage/invoice_storage.py` — add `update_last_notified_for_date()` method
- Modify: `src/engine/services/scheduler_service.py` — add daily job
- Create: `tests/test_scheduler_invoice_due.py`

- [ ] **Step 1: Add storage method**

In `src/engine/storage/invoice_storage.py`, append:

```python
async def update_last_notified_for_date(self, invoice_id: UUID, when: date) -> None:
    await self.execute(
        "UPDATE invoices SET last_notified_for_date = $2, updated_at = NOW() "
        "WHERE id = $1",
        invoice_id, when,
    )
```

Also extend `list_due_today_or_tomorrow` filter to skip already-notified-today rows:

```python
async def list_due_today_or_tomorrow_pending_notif(
    self, org_id: UUID, today: date,
) -> List[Invoice]:
    rows = await self.fetch(
        """
        SELECT * FROM invoices
        WHERE org_id = $1
          AND status = 'approved'
          AND processed_at IS NULL
          AND is_active = true
          AND (due_date = $2 OR due_date = $2 + 1)
          AND (last_notified_for_date IS NULL OR last_notified_for_date < $2)
        ORDER BY due_date
        """,
        org_id, today,
    )
    return [self._row_to_invoice(r) for r in rows]
```

(Keep the older `list_due_today_or_tomorrow` for compatibility / Plan 2 tests.)

- [ ] **Step 2: Add scheduler job**

In `src/engine/services/scheduler_service.py`, find the loop that iterates per-org morning hours and call a new helper there:

```python
# Inside the morning-hours block of the per-org loop:
await self._notify_invoice_due(org)
```

Define the helper inside the class:

```python
async def _notify_invoice_due(self, org) -> None:
    """Send invoice-due reminders to the org's designated accountant.

    Each invoice with due_date == today or today+1 yields one notification
    per calendar day (via last_notified_for_date dedupe).
    """
    if not org.accountant_user_id:
        return
    from zoneinfo import ZoneInfo
    from datetime import datetime
    tz = ZoneInfo(org.timezone)
    today_local = datetime.now(tz).date()
    rows = await self._invoice_storage.list_due_today_or_tomorrow_pending_notif(
        org.id, today_local,
    )
    for inv in rows:
        f = await self._user_file_storage.get_by_id(inv.file_id)
        file_name = f.original_filename if f else "счёт"
        delta = (inv.due_date - today_local).days if inv.due_date else 0
        when_str = "сегодня" if delta == 0 else f"завтра ({inv.due_date})"
        await self._in_app_notification_service.create(
            user_id=org.accountant_user_id,
            org_id=org.id,
            type='invoice_due',
            title=f'Счёт «{file_name}» — {when_str}',
            content=(f.summary if (f and f.summary) else 'Открой счёт для деталей.'),
            reference_type='invoice',
            reference_id=inv.id,
        )
        await self._invoice_storage.update_last_notified_for_date(inv.id, today_local)
```

Make sure `scheduler_service.__init__` accepts and stores `invoice_storage` and `user_file_storage` (or pulls them from the engine reference). If the existing pattern is to pass storages explicitly, extend the constructor signature; if it grabs `engine_service.invoice_storage` lazily, do that.

- [ ] **Step 3: Write integration test**

```python
# tests/test_scheduler_invoice_due.py
"""Scheduler invoice-due reminder fires exactly once per (invoice, day)."""
import os
import pytest
import pytest_asyncio
from datetime import date, timedelta
from uuid import uuid4

import asyncpg

from src.engine.services.engine_service import get_engine_service


DSN = os.environ.get("DATABASE_URL", "postgresql://postgres@localhost/rugpt")


@pytest_asyncio.fixture
async def env():
    engine = get_engine_service()
    await engine.initialize()
    pool = await asyncpg.create_pool(DSN)
    async with pool.acquire() as conn:
        org = await conn.fetchval(
            "INSERT INTO organizations (id, name, slug, timezone) "
            "VALUES (gen_random_uuid(), 'sch', $1, 'UTC') RETURNING id",
            f"sch_{uuid4().hex[:8]}"
        )
        admin = await conn.fetchval(
            "INSERT INTO users (id, org_id, username, name, password_hash, email, is_admin, is_active) "
            "VALUES (gen_random_uuid(), $1, $2, $2, 'x', $3, true, true) RETURNING id",
            org, f"adm_{uuid4().hex[:6]}", f"adm_{uuid4()}@t.local"
        )
        accountant = await conn.fetchval(
            "INSERT INTO users (id, org_id, username, name, password_hash, email, is_active) "
            "VALUES (gen_random_uuid(), $1, $2, $2, 'x', $3, true) RETURNING id",
            org, f"acc_{uuid4().hex[:6]}", f"acc_{uuid4()}@t.local"
        )
        await conn.execute(
            "UPDATE organizations SET accountant_user_id = $1 WHERE id = $2",
            accountant, org,
        )
    today = date.today()
    inv = await engine.invoice_service.upload(org, admin, "x.pdf", b"x", today)
    await engine.invoice_service.approve(inv.id, admin)
    org_obj = await engine.org_storage.get_by_id(org)
    yield {"engine": engine, "org": org_obj, "accountant": accountant,
            "invoice_id": inv.id, "pool": pool}
    async with pool.acquire() as conn:
        await conn.execute("DELETE FROM in_app_notifications WHERE user_id = $1", accountant)
        await conn.execute("DELETE FROM invoices WHERE org_id = $1", org)
        await conn.execute("DELETE FROM user_files WHERE org_id = $1", org)
        await conn.execute("DELETE FROM users WHERE org_id = $1", org)
        await conn.execute("DELETE FROM organizations WHERE id = $1", org)
    await pool.close()


@pytest.mark.asyncio
async def test_notification_created_for_due_invoice(env):
    await env["engine"].scheduler_service._notify_invoice_due(env["org"])
    async with env["pool"].acquire() as conn:
        count = await conn.fetchval(
            "SELECT COUNT(*) FROM in_app_notifications "
            "WHERE user_id = $1 AND type = 'invoice_due'",
            env["accountant"],
        )
    assert count == 1


@pytest.mark.asyncio
async def test_duplicate_run_does_not_double_notify(env):
    await env["engine"].scheduler_service._notify_invoice_due(env["org"])
    await env["engine"].scheduler_service._notify_invoice_due(env["org"])
    async with env["pool"].acquire() as conn:
        count = await conn.fetchval(
            "SELECT COUNT(*) FROM in_app_notifications "
            "WHERE user_id = $1 AND type = 'invoice_due'",
            env["accountant"],
        )
    assert count == 1


@pytest.mark.asyncio
async def test_no_accountant_no_notification(env):
    async with env["pool"].acquire() as conn:
        await conn.execute("UPDATE organizations SET accountant_user_id = NULL WHERE id = $1",
                            env["org"].id)
    org = await env["engine"].org_storage.get_by_id(env["org"].id)
    await env["engine"].scheduler_service._notify_invoice_due(org)
    async with env["pool"].acquire() as conn:
        count = await conn.fetchval(
            "SELECT COUNT(*) FROM in_app_notifications WHERE type = 'invoice_due'"
        )
    assert count == 0
```

- [ ] **Step 4: Run tests**

Run: `cd /root/rugpt && venv/bin/pytest tests/test_scheduler_invoice_due.py -v`
Expected: 3 passing.

- [ ] **Step 5: Commit**

```bash
git add src/engine/storage/invoice_storage.py src/engine/services/scheduler_service.py tests/test_scheduler_invoice_due.py
git commit -m "feat(scheduler): invoice-due notification with daily idempotency"
```

---

### Task 8: End-to-end manual smoke

This is the real end-to-end test of the whole 3-plan stack. No automation.

- [ ] **Step 1: Restart engine on dev**

Restart the engine so it picks up new migration data and new tools registration:
```
screen -r rugpt-engine  # on prod; on dev — local equivalent / restart command
```
After restart, look at the startup log for `Action registry initialized: ['invoice_approve', 'invoice_reject', 'invoice_mark_processed']` and the tools list containing `list_invoices`, `get_invoice`, `show_modal`.

- [ ] **Step 2: Verify `invoice_clerk` appears in UI**

Log in to webclient as Эдуард (admin). Sidebar should show «ИИ» group containing PM and **invoice_clerk** ("Счетовод"). ChatInput @@-picker should also show Счетовод on top alongside PM.

- [ ] **Step 3: Upload an invoice as a regular worker**

Log out, log in as a non-admin user. Open `/invoices`, click "Загрузить счёт", pick any PDF, set due_date to today+1, submit. Verify it appears in the table with status "На проверке".

- [ ] **Step 4: Chat with invoice_clerk as admin**

Log out, log in as Эдуард. Click on invoice_clerk in the sidebar. Direct chat opens. Type: «покажи счета на проверке». AI should call `list_invoices(status='created')` and respond with the list including the just-uploaded invoice id.

- [ ] **Step 5: Trigger a modal**

Type: «утверди счёт <id>» (use real id from previous step). AI should:
1. Call `get_invoice(invoice_id=<id>)`
2. Call `show_modal(title=..., body=..., actions=[approve, reject])`

The frontend should pop the modal overlay with two buttons.

- [ ] **Step 6: Click "Утвердить"**

Expected: button shows spinner, then check; modal closes. Behind the scenes `POST /api/actions/invoice_approve` ran, changed status to `approved`. Refresh `/invoices` page in another tab — invoice is now "Утверждены".

- [ ] **Step 7: Set accountant and verify scheduler-driven notification**

On `/invoices` page as admin, use the "Бухгалтер для уведомлений" selector to assign the accountant user. Then trigger the scheduler job manually (one of):
```bash
PGPASSWORD="" psql -h localhost -U postgres -d rugpt -c "
UPDATE invoices SET last_notified_for_date = NULL WHERE status = 'approved';
"
```
And call the scheduler helper from a Python REPL on dev:
```bash
cd /root/rugpt && venv/bin/python -c "
import asyncio
from src.engine.services.engine_service import get_engine_service
async def main():
    e = get_engine_service()
    await e.initialize()
    orgs = await e.org_storage.list_all(active_only=True)
    for o in orgs:
        await e.scheduler_service._notify_invoice_due(o)
asyncio.run(main())
"
```

Log in as the designated accountant — bell icon should show the new "Счёт …" notification.

- [ ] **Step 8: Document the smoke**

Add `/root/rugpt/docs/notes/2026-05-19-invoice-flow-smoke.md` with a paragraph: which user roles tested, which steps observed working, which warnings showed in logs.

- [ ] **Step 9: Commit**

```bash
git add docs/notes/2026-05-19-invoice-flow-smoke.md
git commit -m "docs: invoice-flow end-to-end smoke results"
```

---

## Self-review summary

1. **Spec coverage:** invoice_clerk role + system user (Task 1), prompt (Task 2), list/get tools (Tasks 3-4), tool registry wiring (Task 5), frontend whitelist (Task 6), scheduler with idempotency (Task 7), end-to-end smoke (Task 8). All Plan 3 sections of the original spec covered.
2. **Placeholder scan:** No "TBD". The scheduler-test fixture uses `date.today()` rather than a frozen-time abstraction — acceptable for a one-shot reminder test; if flake hits at midnight, follow up.
3. **Type consistency:** `invoice_id` snake_case throughout engine, action_type names match Plan 2 handler names, prompt mentions same tool names registered in Task 5.
4. **Scope:** End-to-end of the three plans now works.
