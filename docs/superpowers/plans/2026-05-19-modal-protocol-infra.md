# Modal Protocol Infrastructure Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.
>
> **Dev-environment note:** On the dev machine there is no git. Commit commands at the end of each task are advisory — when the plan is executed there, treat them as logical groupings; the actual `git add`/`git commit` runs on Mac after rsync.

**Goal:** Build the generic modal-protocol primitive on top of which any "AI prepares, human decides" feature (starting with invoices) can be added. No invoice-specific code in this plan.

**Architecture:** Tool-based ("T-approach") not LangGraph interrupt. AI calls `show_modal(title, body, actions=[{label, action_type, params}])`. Payload attaches to the AI message in `messages.metadata.modal`. Frontend renders it as an overlay on the active chat page. Click → `POST /api/v1/actions/{action_type}` → engine dispatcher → registered handler. Per-role `agent_config.allowed_action_types` whitelist as defence against prompt-injection.

**Tech Stack:** Python 3.10 / FastAPI / asyncpg on engine; NestJS / TypeScript on webclient backend; Next.js 16 / React 19 / Zustand on frontend. LangChain `BaseTool` for the tool. `pytest` + `pytest_asyncio` for engine tests, `vitest` for webclient tests.

---

## File structure

Engine:
- Create: `src/engine/migrations/043_messages_metadata.sql` — add `messages.metadata JSONB`
- Create: `src/engine/actions/__init__.py` — package marker
- Create: `src/engine/actions/registry.py` — `ActionDefinition`, `ActionRegistry`, `ActionError`
- Create: `src/engine/actions/bootstrap.py` — registers all action types at startup (empty in v1; populated by Plan 3 with invoice actions)
- Create: `src/engine/routes/actions.py` — `POST /api/v1/actions/{action_type}` dispatcher
- Create: `src/engine/agents/tools/show_modal.py` — `show_modal` tool
- Modify: `src/engine/services/engine_service.py` — init `ActionRegistry`, call `bootstrap`, register `show_modal` tool, expose `action_registry`
- Modify: `src/engine/services/ai_service.py` — extract `show_modal` payload from agent tool_calls and write to `messages.metadata.modal`
- Modify: `src/engine/storage/message_storage.py` — accept `metadata` in `create()` and `_row_to_message()` reads it
- Modify: `src/engine/models/message.py` — add `metadata: dict = field(default_factory=dict)` field
- Modify: `src/engine/routes/__init__.py` — expose `actions_router`
- Modify: `src/engine/app.py` — include `actions_router`

Engine tests:
- Create: `tests/test_action_registry.py` — unit tests for registry register/lookup/dispatch
- Create: `tests/test_action_route.py` — integration test for `POST /api/v1/actions/dummy_acknowledge`
- Create: `tests/test_show_modal_tool.py` — tool emits payload, rejects unknown action_types, rejects when role lacks action in whitelist
- Create: `tests/test_ai_service_modal_payload.py` — verifies modal payload from a tool call lands in `messages.metadata.modal`

Webclient backend:
- Create: `packages/backend/src/action/action.module.ts`
- Create: `packages/backend/src/action/action.controller.ts` — `POST /api/actions/:action_type`
- Create: `packages/backend/src/action/action.service.ts`
- Modify: `packages/backend/src/engine/adapters/rugpt.adapter.ts` — `case 'dispatch_action'`
- Modify: `packages/backend/src/app.module.ts` — register `ActionModule`

Frontend:
- Create: `packages/frontend/src/app/components/ModalRenderer.tsx` — overlay modal renderer
- Create: `packages/frontend/src/app/components/ChatModalOverlay.tsx` — drop-in widget: `useEffect` finds latest unresolved modal payload in `messages`, renders `<ModalRenderer />`
- Create: `packages/frontend/src/app/hooks/useModalAction.ts` — POST to backend, return result
- Modify: `packages/frontend/src/app/chat/[username]/page.tsx` — mount `<ChatModalOverlay messages={messages} />` once (only this page for v1 — other chat pages get the same widget when refactor described in `webclient_rugpt/doc/TECH_DEBT.md` "Унификация чат-страниц" lands, but invoice_clerk's direct chat lives here so v1 only needs this one mount)

---

### Task 1: Migration 043 — add `messages.metadata` JSONB

**Files:**
- Create: `src/engine/migrations/043_messages_metadata.sql`

- [ ] **Step 1: Write the migration**

```sql
-- Migration 043: add metadata JSONB to messages.
-- Used by the modal protocol (messages.metadata.modal) and reusable for future
-- per-message extension fields.

ALTER TABLE messages
    ADD COLUMN metadata JSONB NOT NULL DEFAULT '{}'::jsonb;
```

- [ ] **Step 2: Apply migration on dev**

Run: `cd /root/rugpt && ./migrate.sh`
Expected output: `Running migration: 043_messages_metadata.sql` followed by `Applied`.

- [ ] **Step 3: Verify the column exists**

Run: `PGPASSWORD="" psql -h localhost -U postgres -d rugpt -c "\d messages" | grep metadata`
Expected: `metadata | jsonb | not null | default '{}'::jsonb`

- [ ] **Step 4: Commit**

```bash
git add src/engine/migrations/043_messages_metadata.sql
git commit -m "feat(db): add messages.metadata jsonb for modal payloads"
```

---

### Task 2: Action registry module — tests first

**Files:**
- Test: `tests/test_action_registry.py`
- Create: `src/engine/actions/__init__.py`
- Create: `src/engine/actions/registry.py`

- [ ] **Step 1: Write failing tests**

```python
# tests/test_action_registry.py
"""Unit tests for the action registry."""
import pytest
from pydantic import BaseModel
from src.engine.actions.registry import (
    ActionRegistry,
    ActionDefinition,
    ActionError,
    UnknownActionError,
    PermissionDeniedError,
)


class DummyParams(BaseModel):
    target_id: str


async def dummy_handler(engine, user, params: DummyParams) -> dict:
    return {"ok": True, "target_id": params.target_id}


def always_true(user, params):
    return True


def admin_only(user, params):
    return bool(getattr(user, "is_admin", False))


@pytest.fixture
def registry():
    r = ActionRegistry()
    r.register(ActionDefinition(
        action_type="dummy_open",
        handler=dummy_handler,
        params_schema=DummyParams,
        permission=always_true,
    ))
    r.register(ActionDefinition(
        action_type="dummy_admin",
        handler=dummy_handler,
        params_schema=DummyParams,
        permission=admin_only,
    ))
    return r


def test_register_and_get(registry):
    d = registry.get("dummy_open")
    assert d is not None
    assert d.action_type == "dummy_open"


def test_get_unknown_returns_none(registry):
    assert registry.get("nope") is None


@pytest.mark.asyncio
async def test_dispatch_unknown_raises(registry):
    with pytest.raises(UnknownActionError):
        await registry.dispatch(engine=None, action_type="nope", params={}, user=None)


@pytest.mark.asyncio
async def test_dispatch_permission_denied_raises(registry):
    class U:
        is_admin = False
    with pytest.raises(PermissionDeniedError):
        await registry.dispatch(engine=None, action_type="dummy_admin",
                                 params={"target_id": "x"}, user=U())


@pytest.mark.asyncio
async def test_dispatch_invalid_params_raises(registry):
    class U:
        is_admin = True
    with pytest.raises(ActionError):
        await registry.dispatch(engine=None, action_type="dummy_admin",
                                 params={}, user=U())  # missing target_id


@pytest.mark.asyncio
async def test_dispatch_happy_path(registry):
    class U:
        is_admin = True
    result = await registry.dispatch(engine=None, action_type="dummy_admin",
                                      params={"target_id": "abc"}, user=U())
    assert result == {"ok": True, "target_id": "abc"}


def test_register_duplicate_raises(registry):
    with pytest.raises(ValueError):
        registry.register(ActionDefinition(
            action_type="dummy_open",
            handler=dummy_handler,
            params_schema=DummyParams,
            permission=always_true,
        ))
```

- [ ] **Step 2: Run tests, verify they fail with ImportError**

Run: `cd /root/rugpt && venv/bin/pytest tests/test_action_registry.py -v 2>&1 | head -30`
Expected: `ImportError: cannot import name 'ActionRegistry' from 'src.engine.actions.registry'`

- [ ] **Step 3: Create package marker**

```python
# src/engine/actions/__init__.py
"""Action registry for modal-protocol dispatching."""
```

- [ ] **Step 4: Implement registry**

```python
# src/engine/actions/registry.py
"""Action registry: registers action_type → handler/permission/params_schema.

The registry is the single dispatcher used by `POST /api/v1/actions/{action_type}`.
Action handlers mutate target resources (e.g., invoice_approve flips an invoice
status). All actions go through this single chokepoint so authorization,
parameter validation and audit can be enforced uniformly.
"""
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Dict, Optional

from pydantic import BaseModel, ValidationError


class ActionError(Exception):
    """Base error for action dispatch failures."""


class UnknownActionError(ActionError):
    """Raised when an unknown action_type is requested."""


class PermissionDeniedError(ActionError):
    """Raised when the user doesn't pass the permission check."""


@dataclass
class ActionDefinition:
    action_type: str
    handler: Callable[..., Awaitable[Dict[str, Any]]]   # async (engine, user, params) -> dict
    params_schema: type[BaseModel]
    permission: Callable[[Any, BaseModel], bool]


class ActionRegistry:
    def __init__(self):
        self._defs: Dict[str, ActionDefinition] = {}

    def register(self, definition: ActionDefinition) -> None:
        if definition.action_type in self._defs:
            raise ValueError(f"action_type {definition.action_type!r} already registered")
        self._defs[definition.action_type] = definition

    def get(self, action_type: str) -> Optional[ActionDefinition]:
        return self._defs.get(action_type)

    def list_action_types(self) -> list[str]:
        return list(self._defs.keys())

    async def dispatch(self, *, engine, action_type: str, params: dict, user) -> Dict[str, Any]:
        definition = self.get(action_type)
        if definition is None:
            raise UnknownActionError(f"unknown action_type: {action_type}")

        try:
            parsed = definition.params_schema(**(params or {}))
        except ValidationError as exc:
            raise ActionError(f"invalid params for {action_type}: {exc}") from exc

        if not definition.permission(user, parsed):
            raise PermissionDeniedError(f"user not allowed to invoke {action_type}")

        return await definition.handler(engine, user, parsed)
```

- [ ] **Step 5: Run tests, verify they pass**

Run: `cd /root/rugpt && venv/bin/pytest tests/test_action_registry.py -v`
Expected: all 7 tests pass.

- [ ] **Step 6: Commit**

```bash
git add src/engine/actions/ tests/test_action_registry.py
git commit -m "feat(actions): introduce action registry with permission and params validation"
```

---

### Task 3: Bootstrap module (empty in v1)

**Files:**
- Create: `src/engine/actions/bootstrap.py`

`register_all` is the single function called at engine startup to populate the registry. In v1 it registers no actions — production has no "demo" handlers in the registry. Plan 3 will add `invoice_approve` / `invoice_reject` / `invoice_mark_processed` to it.

Tests use the registry's `register()` directly in fixtures (so a test can wire its own action_type without polluting the global production list).

- [ ] **Step 1: Write the bootstrap module**

```python
# src/engine/actions/bootstrap.py
"""Registers all production action types at engine startup.

In v1 the registry is empty. Tests use ActionRegistry.register() in fixtures
to wire test-only actions. Production action types (invoice_approve, etc.)
are added here by subsequent feature plans.
"""
from src.engine.actions.registry import ActionRegistry


def register_all(registry: ActionRegistry) -> None:
    # No production action types in this plan; populated by feature plans.
    return
```

- [ ] **Step 2: Smoke-import**

Run: `cd /root/rugpt && venv/bin/python -c "from src.engine.actions.bootstrap import register_all; from src.engine.actions.registry import ActionRegistry; r = ActionRegistry(); register_all(r); print(r.list_action_types())"`
Expected: `[]`

- [ ] **Step 3: Commit**

```bash
git add src/engine/actions/bootstrap.py
git commit -m "feat(actions): bootstrap stub for action registry"
```

---

### Task 4: Wire registry into `EngineService.initialize`

**Files:**
- Modify: `src/engine/services/engine_service.py`

- [ ] **Step 1: Add registry attribute and bootstrap call**

Find the section of `EngineService.__init__` where other in-memory components are initialised (such as `tool_registry`). Add:

```python
# in __init__:
from src.engine.actions.registry import ActionRegistry
self.action_registry = ActionRegistry()
```

In `EngineService.initialize`, after services are constructed (before scheduler start), add:

```python
from src.engine.actions.bootstrap import register_all as _register_actions
_register_actions(self.action_registry)
logger.info("Action registry initialized: %s", self.action_registry.list_action_types())
```

- [ ] **Step 2: Smoke-import engine_service**

Run: `cd /root/rugpt && venv/bin/python -c "from src.engine.services.engine_service import EngineService; print('ok')"`
Expected: `ok`.

- [ ] **Step 3: Commit**

```bash
git add src/engine/services/engine_service.py
git commit -m "feat(engine): wire action registry into engine service init"
```

---

### Task 5: Dispatcher route `POST /api/v1/actions/{action_type}`

**Files:**
- Create: `src/engine/routes/actions.py`
- Modify: `src/engine/routes/__init__.py`
- Modify: `src/engine/app.py`
- Create: `tests/test_action_route.py`

- [ ] **Step 1: Write failing integration test (uses fixture-registered test action)**

```python
# tests/test_action_route.py
"""Integration test for the action dispatcher route.

Real DB. Fixture registers a `__test_action` directly on the engine's
action_registry (production registry stays empty per bootstrap stub).
Verifies 200/400/401/403/404 contracts.
"""
import os
import pytest
import pytest_asyncio
from uuid import uuid4

import asyncpg
from httpx import AsyncClient, ASGITransport
from pydantic import BaseModel

from src.engine.app import app
from src.engine.services.engine_service import get_engine_service
from src.engine.actions.registry import ActionDefinition
from src.engine.routes.auth import create_token


DSN = os.environ.get("DATABASE_URL", "postgresql://postgres@localhost/rugpt")


class _TestActionParams(BaseModel):
    target_id: str


async def _test_handler(engine, user, params: _TestActionParams) -> dict:
    return {"ok": True, "id": params.target_id, "user_id": str(user.id)}


def _allow_active(user, params) -> bool:
    return bool(getattr(user, "is_active", True))


def _admin_only(user, params) -> bool:
    return bool(getattr(user, "is_admin", False))


@pytest_asyncio.fixture
async def env():
    pool = await asyncpg.create_pool(DSN)
    async with pool.acquire() as conn:
        org_id = await conn.fetchval(
            "INSERT INTO organizations (id, name, slug) VALUES (gen_random_uuid(), 'a', $1) "
            "RETURNING id", f"action_{uuid4().hex[:8]}"
        )
        user_id = await conn.fetchval(
            "INSERT INTO users (id, org_id, username, name, password_hash, email, is_active) "
            "VALUES (gen_random_uuid(), $1, $2, $2, 'x', $3, true) RETURNING id",
            org_id, f"u_{uuid4().hex[:6]}", f"u_{uuid4()}@t.local"
        )

    engine = get_engine_service()
    await engine.initialize()

    # Register test-only action types (cleaned up at end of fixture).
    engine.action_registry.register(ActionDefinition(
        action_type="__test_action",
        handler=_test_handler,
        params_schema=_TestActionParams,
        permission=_allow_active,
    ))
    engine.action_registry.register(ActionDefinition(
        action_type="__test_admin_only",
        handler=_test_handler,
        params_schema=_TestActionParams,
        permission=_admin_only,
    ))

    token = create_token(user_id, org_id, is_admin=False)
    yield {"user_id": str(user_id), "org_id": str(org_id), "token": token, "pool": pool}

    # Cleanup
    engine.action_registry._defs.pop("__test_action", None)
    engine.action_registry._defs.pop("__test_admin_only", None)
    async with pool.acquire() as conn:
        await conn.execute("DELETE FROM users WHERE id = $1", user_id)
        await conn.execute("DELETE FROM organizations WHERE id = $1", org_id)
    await pool.close()


@pytest.mark.asyncio
async def test_dispatch_happy_path(env):
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        r = await client.post(
            "/api/v1/actions/__test_action",
            json={"target_id": "abc-123"},
            headers={"Authorization": f"Bearer {env['token']}"},
        )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True
    assert body["id"] == "abc-123"


@pytest.mark.asyncio
async def test_dispatch_unknown_returns_404(env):
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        r = await client.post(
            "/api/v1/actions/does_not_exist",
            json={},
            headers={"Authorization": f"Bearer {env['token']}"},
        )
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_dispatch_invalid_params_returns_400(env):
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        r = await client.post(
            "/api/v1/actions/__test_action",
            json={},  # missing target_id
            headers={"Authorization": f"Bearer {env['token']}"},
        )
    assert r.status_code == 400


@pytest.mark.asyncio
async def test_dispatch_permission_denied_returns_403(env):
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        r = await client.post(
            "/api/v1/actions/__test_admin_only",
            json={"target_id": "abc"},
            headers={"Authorization": f"Bearer {env['token']}"},
        )
    assert r.status_code == 403


@pytest.mark.asyncio
async def test_dispatch_unauthenticated_returns_401(env):
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        r = await client.post(
            "/api/v1/actions/__test_action",
            json={"target_id": "abc"},
            # no Authorization header
        )
    assert r.status_code == 401
```

- [ ] **Step 2: Run tests, verify they fail (route not found)**

Run: `cd /root/rugpt && venv/bin/pytest tests/test_action_route.py -v 2>&1 | head -30`
Expected: 404 / route not registered. Tests fail.

- [ ] **Step 3: Write the route**

```python
# src/engine/routes/actions.py
"""Generic dispatcher for action_types registered in EngineService.action_registry.

This is the single backend chokepoint for "AI prepares, human decides" flows:
the frontend modal renderer POSTs here with the action_type chosen by the user.
All authorization, parameter validation and side effects live in handler
modules under `src/engine/actions/`.
"""
from fastapi import APIRouter, Depends, HTTPException
from uuid import UUID

from src.engine.actions.registry import (
    ActionError,
    PermissionDeniedError,
    UnknownActionError,
)
from src.engine.routes.auth import get_current_user
from src.engine.services.engine_service import get_engine_service
from src.engine.unified_logger import get_logger

logger = get_logger("routes")

router = APIRouter(prefix="/actions", tags=["actions"])


@router.post("/{action_type}")
async def dispatch_action(
    action_type: str,
    params: dict,
    current_user: dict = Depends(get_current_user),
):
    engine = get_engine_service()
    user = await engine.user_storage.get_by_id(UUID(current_user["user_id"]))
    if not user:
        raise HTTPException(status_code=401, detail="User not found")

    try:
        result = await engine.action_registry.dispatch(
            engine=engine,
            action_type=action_type,
            params=params or {},
            user=user,
        )
        return result
    except UnknownActionError as exc:
        logger.warning("action dispatch unknown_type=%s user=%s", action_type, user.id)
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except PermissionDeniedError as exc:
        logger.warning("action dispatch denied type=%s user=%s", action_type, user.id)
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except ActionError as exc:
        logger.warning("action dispatch bad_params type=%s user=%s err=%s", action_type, user.id, exc)
        raise HTTPException(status_code=400, detail=str(exc)) from exc
```

- [ ] **Step 4: Expose router from routes package**

In `src/engine/routes/__init__.py` add:
```python
from src.engine.routes.actions import router as actions_router
```
And in `__all__` (or whatever export list exists), add `"actions_router"`.

- [ ] **Step 5: Register router in app**

In `src/engine/app.py`, add to the imports block:
```python
from .routes import (
    ...,  # existing
    actions_router,
)
```
And add the include:
```python
app.include_router(actions_router, prefix="/api/v1", tags=["actions"])
```

- [ ] **Step 6: Run tests, verify they pass**

Run: `cd /root/rugpt && venv/bin/pytest tests/test_action_route.py -v`
Expected: all 5 tests pass.

- [ ] **Step 7: Commit**

```bash
git add src/engine/routes/actions.py src/engine/routes/__init__.py src/engine/app.py tests/test_action_route.py
git commit -m "feat(routes): generic action dispatcher endpoint"
```

---

### Task 6: `show_modal` tool with per-role whitelist

**Files:**
- Create: `src/engine/agents/tools/show_modal.py`
- Create: `tests/test_show_modal_tool.py`

- [ ] **Step 1: Write failing tests**

```python
# tests/test_show_modal_tool.py
"""Unit tests for the show_modal tool: input validation + per-role whitelist."""
import pytest
from unittest.mock import MagicMock
from langchain_core.runnables import RunnableConfig

from src.engine.agents.tools.show_modal import create_show_modal_tool


@pytest.fixture
def registry_stub():
    """Stub action registry that knows about 'invoice_approve' only."""
    from pydantic import BaseModel
    class Params(BaseModel):
        invoice_id: str

    reg = MagicMock()
    def get(action_type):
        if action_type == "invoice_approve":
            d = MagicMock()
            d.params_schema = Params
            return d
        return None
    reg.get.side_effect = get
    return reg


@pytest.fixture
def role_stub(allowed):
    r = MagicMock()
    r.agent_config = {"allowed_action_types": list(allowed)}
    return r


def make_config(role) -> RunnableConfig:
    return {"configurable": {"role": role, "caller_user_id": "u-1", "org_id": "o-1"}}


@pytest.mark.asyncio
@pytest.mark.parametrize("allowed", [["invoice_approve"]])
async def test_show_modal_emits_payload_for_allowed_action(registry_stub, role_stub):
    tool = create_show_modal_tool(registry_stub)
    payload = await tool.ainvoke(
        {
            "title": "Утвердить счёт",
            "body": "Поставщик X, сумма 1000",
            "actions": [
                {"label": "Утвердить", "action_type": "invoice_approve",
                 "params": {"invoice_id": "abc-uuid"}}
            ],
        },
        config=make_config(role_stub),
    )
    # tool returns the payload string the LLM sees; the real payload is stashed
    # in tool_output_metadata for ai_service to pluck out later.
    assert "shown" in payload.lower() or "modal" in payload.lower()


@pytest.mark.asyncio
@pytest.mark.parametrize("allowed", [["invoice_approve"]])
async def test_show_modal_rejects_action_type_not_in_role_whitelist(registry_stub, role_stub):
    tool = create_show_modal_tool(registry_stub)
    result = await tool.ainvoke(
        {
            "title": "X",
            "body": "Y",
            "actions": [
                {"label": "L", "action_type": "delete_all_users",
                 "params": {}}
            ],
        },
        config=make_config(role_stub),
    )
    assert "not allowed" in result.lower()


@pytest.mark.asyncio
@pytest.mark.parametrize("allowed", [["invoice_approve"]])
async def test_show_modal_rejects_unknown_action_type(registry_stub, role_stub):
    role_stub.agent_config = {"allowed_action_types": ["never_registered"]}
    tool = create_show_modal_tool(registry_stub)
    result = await tool.ainvoke(
        {
            "title": "X",
            "body": "Y",
            "actions": [
                {"label": "L", "action_type": "never_registered", "params": {}}
            ],
        },
        config=make_config(role_stub),
    )
    assert "unknown" in result.lower() or "not registered" in result.lower()


@pytest.mark.asyncio
@pytest.mark.parametrize("allowed", [["invoice_approve"]])
async def test_show_modal_rejects_bad_params_schema(registry_stub, role_stub):
    tool = create_show_modal_tool(registry_stub)
    result = await tool.ainvoke(
        {
            "title": "X",
            "body": "Y",
            "actions": [
                {"label": "L", "action_type": "invoice_approve",
                 "params": {}}   # missing invoice_id
            ],
        },
        config=make_config(role_stub),
    )
    assert "invalid" in result.lower() or "missing" in result.lower()
```

- [ ] **Step 2: Run tests, verify they fail (import error)**

Run: `cd /root/rugpt && venv/bin/pytest tests/test_show_modal_tool.py -v 2>&1 | head -10`
Expected: ImportError on `create_show_modal_tool`.

- [ ] **Step 3: Implement the tool**

```python
# src/engine/agents/tools/show_modal.py
"""show_modal: generic modal-protocol tool.

The tool is read-only: it does not write to DB. It validates that the calling
role is allowed to emit the requested action_types (per Role.agent_config.
allowed_action_types) and that each action's params match the registered
params_schema. On success it returns a brief confirmation string to the LLM;
the structured payload is stashed in the runnable's tool metadata for the
ai_service to pick up and write into messages.metadata.modal during message
persistence.
"""
from typing import Annotated, Optional
from pydantic import BaseModel, Field, ValidationError

from langchain_core.runnables import RunnableConfig
from langchain_core.tools import InjectedToolArg, StructuredTool, ToolException


class ShowModalAction(BaseModel):
    label: str = Field(description="Button label visible to the user")
    action_type: str = Field(description="Registered action_type the click will dispatch")
    params: dict = Field(default_factory=dict, description="Params for the action handler")


class ShowModalInput(BaseModel):
    title: str = Field(description="Modal title")
    body: str = Field(description="Modal body (markdown supported)")
    actions: list[ShowModalAction] = Field(description="One or more action buttons")
    target: Optional[dict] = Field(
        default=None,
        description=(
            "Optional hint to the frontend about which resource this modal "
            "targets, e.g. {\"type\": \"invoice\", \"id\": \"<uuid>\"}. "
            "Frontend uses it to derive resolved-state on refresh."
        ),
    )


def _validate(role, registry, actions: list[ShowModalAction]) -> Optional[str]:
    """Return error string if any action is invalid, else None."""
    allowed = (role.agent_config or {}).get("allowed_action_types", []) if role else []
    for a in actions:
        if a.action_type not in allowed:
            return (
                f"action_type {a.action_type!r} is not allowed for this role "
                f"(allowed: {allowed})"
            )
        definition = registry.get(a.action_type)
        if definition is None:
            return f"action_type {a.action_type!r} is unknown / not registered on the engine"
        try:
            definition.params_schema(**(a.params or {}))
        except ValidationError as exc:
            return f"invalid params for {a.action_type}: {exc}"
    return None


def create_show_modal_tool(action_registry):
    """Factory: returns a LangChain StructuredTool bound to the given registry."""

    async def _show_modal(
        title: str,
        body: str,
        actions: list[ShowModalAction],
        target: Optional[dict] = None,
        config: Annotated[RunnableConfig, InjectedToolArg] = None,
    ) -> str:
        cfg = (config or {}).get("configurable", {}) or {}
        role = cfg.get("role")
        if role is None:
            raise ToolException("role not provided in runnable config")

        err = _validate(role, action_registry, actions)
        if err:
            # Returning an error string lets the LLM see it in the tool result
            # and reason about it (or retry with different params).
            return err

        # Stash payload for ai_service to attach to the persisted message.
        # We use the standard LangChain "tool output metadata" channel: when
        # the tool returns a string, the ai_service inspects `intermediate_steps`
        # of the agent run and pulls the modal payload from there. See
        # ai_service modal payload handling.
        payload = {
            "title": title,
            "body": body,
            "actions": [a.model_dump() for a in actions],
            "target": target,
        }
        # We tag the success string deterministically so ai_service can find it
        # in the tool log when extracting payloads.
        import json
        return f"<<MODAL_EMITTED>>{json.dumps(payload, ensure_ascii=False)}<</MODAL_EMITTED>>"

    return StructuredTool.from_function(
        coroutine=_show_modal,
        name="show_modal",
        description=(
            "Display a confirmation modal to the user with one or more action "
            "buttons. The tool itself does not mutate any data — it only "
            "renders a card. When the user clicks a button, the chosen "
            "action_type's handler runs on the backend. Use this whenever you "
            "need a human-in-the-loop decision."
        ),
        args_schema=ShowModalInput,
    )
```

- [ ] **Step 4: Run tests, verify they pass**

Run: `cd /root/rugpt && venv/bin/pytest tests/test_show_modal_tool.py -v`
Expected: all 4 tests pass.

- [ ] **Step 5: Commit**

```bash
git add src/engine/agents/tools/show_modal.py tests/test_show_modal_tool.py
git commit -m "feat(tools): show_modal tool with per-role allowed_action_types whitelist"
```

---

### Task 7: Register `show_modal` in tool registry + inject `role` into RunnableConfig

**Files:**
- Modify: `src/engine/services/engine_service.py`
- Modify: `src/engine/agents/executor.py`

- [ ] **Step 1: Register tool in EngineService.initialize**

In `engine_service.py`, find the block that registers other tools (e.g., `task_create_tool`, `rag_search`). Add:

```python
from src.engine.agents.tools.show_modal import create_show_modal_tool
show_modal_tool = create_show_modal_tool(self.action_registry)
self.tool_registry.register("show_modal", show_modal_tool)
```

Place this AFTER `register_all(self.action_registry)` so the registry is populated when the tool wraps it.

- [ ] **Step 2: Inject role into RunnableConfig in AgentExecutor**

In `src/engine/agents/executor.py`, find where the LangChain `RunnableConfig` is built (the `configurable` dict that already contains `org_id`, `user_id`, etc.). Add:

```python
# Existing configurable dict, ensure it has:
configurable = {
    "org_id": str(org_id),
    "caller_user_id": str(user_id),
    # ... existing fields ...
    "role": role,           # <-- new: pass the Role object so show_modal can read agent_config
}
```

If the dict is built in multiple places (simple/chain/supervisor graphs each construct their own), add `role` to each. The Role object is already in scope in `AgentExecutor.execute`.

- [ ] **Step 3: Smoke-import**

Run: `cd /root/rugpt && venv/bin/python -c "from src.engine.services.engine_service import EngineService; print('ok')"`
Expected: `ok`.

- [ ] **Step 4: Commit**

```bash
git add src/engine/services/engine_service.py src/engine/agents/executor.py
git commit -m "feat(engine): register show_modal tool and pass role into runnable config"
```

---

### Task 8: Extract modal payload from tool calls into `messages.metadata.modal`

**Files:**
- Modify: `src/engine/models/message.py` — add `metadata: dict` field
- Modify: `src/engine/storage/message_storage.py` — read/write `metadata`
- Modify: `src/engine/services/ai_service.py` — extract payload, set on message
- Create: `tests/test_ai_service_modal_payload.py`

- [ ] **Step 1: Update Message model**

In `src/engine/models/message.py`, add a field to the dataclass:

```python
from dataclasses import field

@dataclass
class Message:
    ...  # existing fields
    metadata: dict = field(default_factory=dict)
```

- [ ] **Step 2: Update MessageStorage to read/write metadata**

In `src/engine/storage/message_storage.py`:

In the INSERT statement of `create()`, add `metadata` column and value:
```python
# Build the INSERT param list - add metadata at appropriate position
# (it's NOT NULL DEFAULT '{}' so passing None is fine, asyncpg will coerce to empty)
import json
# in the VALUES list pass: json.dumps(message.metadata or {})
```

Concretely, find the existing `INSERT INTO messages (...)` query and add the column. Example:
```python
query = """
INSERT INTO messages (
    id, chat_id, sender_type, sender_id, content,
    mentions, reply_to_id, ai_is_valid, ai_edited,
    metadata,
    is_deleted, created_at, updated_at
) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13)
"""
```

In `_row_to_message()`, read:
```python
import json
metadata_raw = row.get("metadata") if hasattr(row, "get") else row["metadata"]
metadata = json.loads(metadata_raw) if isinstance(metadata_raw, str) else (metadata_raw or {})
```
And include `metadata=metadata` in the returned `Message(...)`.

- [ ] **Step 3: Write failing test for ai_service modal payload attachment**

```python
# tests/test_ai_service_modal_payload.py
"""Verify that when an agent run produces a show_modal tool call, the resulting
AI message has its modal payload attached to messages.metadata.modal."""
import os
import json
import pytest
import pytest_asyncio
from uuid import uuid4
import asyncpg

from src.engine.services.engine_service import get_engine_service
from src.engine.agents.result import AgentResult, ToolCall


DSN = os.environ.get("DATABASE_URL", "postgresql://postgres@localhost/rugpt")


@pytest_asyncio.fixture
async def env():
    engine = get_engine_service()
    await engine.initialize()
    pool = await asyncpg.create_pool(DSN)
    async with pool.acquire() as conn:
        org_id = await conn.fetchval(
            "INSERT INTO organizations (id, name, slug) VALUES (gen_random_uuid(), 'm', $1) RETURNING id",
            f"modal_{uuid4().hex[:8]}",
        )
        user_id = await conn.fetchval(
            "INSERT INTO users (id, org_id, username, name, password_hash, email, is_active) "
            "VALUES (gen_random_uuid(), $1, $2, $2, 'x', $3, true) RETURNING id",
            org_id, f"u_{uuid4().hex[:6]}", f"u_{uuid4()}@t.local"
        )
        chat_id = await conn.fetchval(
            "INSERT INTO chats (id, org_id, type, participants) VALUES (gen_random_uuid(), $1, 'direct', ARRAY[$2::text]) RETURNING id",
            org_id, str(user_id)
        )
    yield {"engine": engine, "org_id": org_id, "user_id": user_id,
            "chat_id": chat_id, "pool": pool}
    async with pool.acquire() as conn:
        await conn.execute("DELETE FROM messages WHERE chat_id = $1", chat_id)
        await conn.execute("DELETE FROM chats WHERE id = $1", chat_id)
        await conn.execute("DELETE FROM users WHERE id = $1", user_id)
        await conn.execute("DELETE FROM organizations WHERE id = $1", org_id)
    await pool.close()


@pytest.mark.asyncio
async def test_modal_payload_attached_to_message(env):
    """ai_service.persist_ai_message_with_modal extracts <<MODAL_EMITTED>> tag
    from a tool call result and writes payload to messages.metadata.modal."""
    engine = env["engine"]

    # Simulate an AgentResult that called show_modal once.
    modal_payload = {
        "title": "Утвердить?",
        "body": "test body",
        "actions": [{"label": "Да", "action_type": "dummy_acknowledge", "params": {"target_id": "x"}}],
        "target": {"type": "test", "id": "x"},
    }
    tool_result_str = f"<<MODAL_EMITTED>>{json.dumps(modal_payload, ensure_ascii=False)}<</MODAL_EMITTED>>"
    result = AgentResult(
        content="Готово.",
        model="test",
        agent_type="simple",
        tool_calls=[ToolCall(name="show_modal", input={}, output=tool_result_str)],
        tokens_used=0,
        finish_reason="stop",
        error=None,
    )

    msg = await engine.ai_service.persist_ai_message_with_modal(
        chat_id=env["chat_id"],
        sender_id=env["user_id"],
        agent_result=result,
        role_id=None,
    )
    assert msg.metadata.get("modal") == modal_payload
```

- [ ] **Step 4: Run test, verify it fails (method missing)**

Run: `cd /root/rugpt && venv/bin/pytest tests/test_ai_service_modal_payload.py -v 2>&1 | head -15`
Expected: AttributeError on `persist_ai_message_with_modal`.

- [ ] **Step 5: Implement extraction + persist helper in ai_service**

In `src/engine/services/ai_service.py`, add a helper method on `AIService`:

```python
import json
import re
from src.engine.models.message import Message, SenderType
from src.engine.agents.result import AgentResult


_MODAL_PATTERN = re.compile(
    r"<<MODAL_EMITTED>>(?P<payload>.*?)<</MODAL_EMITTED>>",
    re.DOTALL,
)


def _extract_modal_payload(result: AgentResult) -> Optional[dict]:
    """Walk tool calls; return last show_modal payload (or None)."""
    last_payload: Optional[dict] = None
    for tc in result.tool_calls or []:
        if tc.name != "show_modal":
            continue
        output = tc.output or ""
        if not isinstance(output, str):
            continue
        m = _MODAL_PATTERN.search(output)
        if not m:
            continue
        try:
            last_payload = json.loads(m.group("payload"))
        except json.JSONDecodeError:
            continue
    return last_payload


class AIService:
    ...
    async def persist_ai_message_with_modal(
        self, chat_id, sender_id, agent_result: AgentResult, role_id=None
    ) -> Message:
        modal = _extract_modal_payload(agent_result)
        metadata = {"modal": modal} if modal else {}
        msg = Message(
            id=None,
            chat_id=chat_id,
            sender_type=SenderType.AI_ROLE,
            sender_id=sender_id,
            content=agent_result.content,
            mentions=[],
            reply_to_id=None,
            ai_is_valid=None,
            ai_edited=False,
            is_deleted=False,
            created_at=None,
            updated_at=None,
            metadata=metadata,
        )
        return await self._message_storage.create(msg)
```

Wire this into the existing AI response persistence path: find where `_message_storage.create(...)` is called for AI messages in `ai_service.py` and replace that single call with `await self.persist_ai_message_with_modal(...)`. Keep behavior identical for non-modal calls (empty metadata).

- [ ] **Step 6: Run test, verify it passes**

Run: `cd /root/rugpt && venv/bin/pytest tests/test_ai_service_modal_payload.py -v`
Expected: 1 passing.

- [ ] **Step 7: Run all engine tests to catch regressions**

Run: `cd /root/rugpt && venv/bin/pytest tests/ -x --timeout=60 2>&1 | tail -10`
Expected: no new failures (some existing tests may be already-broken; only fail this step if a *new* test fails).

- [ ] **Step 8: Commit**

```bash
git add src/engine/models/message.py src/engine/storage/message_storage.py src/engine/services/ai_service.py tests/test_ai_service_modal_payload.py
git commit -m "feat(ai): extract show_modal payload into messages.metadata.modal"
```

---

### Task 9: Webclient backend action proxy

**Files:**
- Create: `packages/backend/src/action/action.module.ts`
- Create: `packages/backend/src/action/action.controller.ts`
- Create: `packages/backend/src/action/action.service.ts`
- Modify: `packages/backend/src/engine/adapters/rugpt.adapter.ts`
- Modify: `packages/backend/src/app.module.ts`

- [ ] **Step 1: Create the action.service.ts**

```typescript
// packages/backend/src/action/action.service.ts
import { Inject, Injectable, Logger, NotFoundException, ForbiddenException, BadRequestException } from '@nestjs/common';
import { ENGINE_ADAPTER, IEngineAdapter } from '../engine/engine-adapter.interface';

interface CurrentUser {
  id: string;
  orgId: string;
  engineToken?: string;
}

@Injectable()
export class ActionService {
  private readonly logger = new Logger(ActionService.name);

  constructor(@Inject(ENGINE_ADAPTER) private readonly engineAdapter: IEngineAdapter) {}

  async dispatch(actionType: string, params: Record<string, unknown>, currentUser: CurrentUser): Promise<any> {
    const [success, data, statusCode] = await this.engineAdapter.execute('dispatch_action', {
      action_type: actionType,
      params,
      token: currentUser.engineToken,
    });
    if (!success) {
      const message = typeof data === 'string' ? data : data?.detail || 'Action dispatch failed';
      if (statusCode === 404) throw new NotFoundException(message);
      if (statusCode === 403) throw new ForbiddenException(message);
      throw new BadRequestException(message);
    }
    return data;
  }
}
```

- [ ] **Step 2: Create the controller**

```typescript
// packages/backend/src/action/action.controller.ts
import { Body, Controller, Param, Post, Request, UseGuards } from '@nestjs/common';
import { JwtAuthGuard } from '../auth/jwt-auth.guard';
import { ApiTags, ApiBearerAuth, ApiOperation } from '@nestjs/swagger';
import { ActionService } from './action.service';

@ApiTags('actions')
@Controller('actions')
@UseGuards(JwtAuthGuard)
@ApiBearerAuth()
export class ActionController {
  constructor(private readonly svc: ActionService) {}

  @Post(':actionType')
  @ApiOperation({ summary: 'Dispatch a modal-protocol action' })
  async dispatch(
    @Param('actionType') actionType: string,
    @Body() body: Record<string, unknown>,
    @Request() req: any,
  ) {
    return this.svc.dispatch(actionType, body || {}, req.user);
  }
}
```

- [ ] **Step 3: Create the module**

```typescript
// packages/backend/src/action/action.module.ts
import { Module } from '@nestjs/common';
import { ActionController } from './action.controller';
import { ActionService } from './action.service';
import { EngineModule } from '../engine/engine.module';

@Module({
  imports: [EngineModule],
  controllers: [ActionController],
  providers: [ActionService],
  exports: [ActionService],
})
export class ActionModule {}
```

- [ ] **Step 4: Add adapter case**

In `packages/backend/src/engine/adapters/rugpt.adapter.ts`, in the big `switch (command)` statement, add a case (next to other generic commands):

```typescript
case 'dispatch_action':
  return this.request(
    'POST',
    `/api/v1/actions/${encodeURIComponent(payload.action_type)}`,
    payload.params || {},
    headers,
  );
```

- [ ] **Step 5: Register module**

In `packages/backend/src/app.module.ts`, add `ActionModule` to the imports array.

```typescript
import { ActionModule } from './action/action.module';

@Module({
  imports: [
    // ... existing
    ActionModule,
  ],
})
export class AppModule {}
```

- [ ] **Step 6: Build + typecheck**

Run: `cd /root/webclient_rugpt/packages/backend && npx tsc --noEmit 2>&1 | head`
Expected: no errors.

- [ ] **Step 7: Commit**

```bash
git add packages/backend/src/action/ packages/backend/src/engine/adapters/rugpt.adapter.ts packages/backend/src/app.module.ts
git commit -m "feat(webclient/backend): proxy POST /api/actions/:type to engine"
```

---

### Task 10: Frontend `ModalRenderer` + `ChatModalOverlay` + `useModalAction`

**Files:**
- Create: `packages/frontend/src/app/components/ModalRenderer.tsx`
- Create: `packages/frontend/src/app/components/ChatModalOverlay.tsx`
- Create: `packages/frontend/src/app/hooks/useModalAction.ts`

- [ ] **Step 1: Create useModalAction hook**

```tsx
// packages/frontend/src/app/hooks/useModalAction.ts
'use client';
import { useState, useCallback } from 'react';
import { getApiClient } from '../../transport/apiClient';
import { useAuthStore } from './useAuth';

export interface ModalAction {
  label: string;
  action_type: string;
  params: Record<string, unknown>;
}

export interface ModalPayload {
  title: string;
  body: string;
  actions: ModalAction[];
  target?: { type: string; id: string };
}

export function useModalAction() {
  const [submitting, setSubmitting] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const currentUserId = useAuthStore((s) => s.user?.id);

  const dispatch = useCallback(
    async (action: ModalAction): Promise<unknown> => {
      setSubmitting(action.action_type);
      setError(null);
      try {
        const api = getApiClient();
        const result = await api.signedPost(
          `/api/actions/${encodeURIComponent(action.action_type)}`,
          action.params,
          currentUserId,
        );
        return result;
      } catch (e) {
        const msg = e instanceof Error ? e.message : 'Action failed';
        setError(msg);
        throw e;
      } finally {
        setSubmitting(null);
      }
    },
    [currentUserId],
  );

  return { dispatch, submitting, error };
}
```

- [ ] **Step 2: Create ModalRenderer component**

```tsx
// packages/frontend/src/app/components/ModalRenderer.tsx
'use client';
import { useEffect, useState } from 'react';
import { useModalAction, ModalPayload, ModalAction } from '../hooks/useModalAction';

/**
 * Renders an overlay modal for AI messages carrying metadata.modal.
 * Receives the parsed payload from the parent chat page. Resolved state
 * derivation (greyed-out after target status changes) is the responsibility
 * of the page providing the payload — once target is no longer in the
 * pending state, the page should stop passing this payload here.
 */
export function ModalRenderer({
  payload,
  onClose,
}: {
  payload: ModalPayload | null;
  onClose: () => void;
}) {
  const { dispatch, submitting, error } = useModalAction();
  const [resolvedLabel, setResolvedLabel] = useState<string | null>(null);

  useEffect(() => {
    setResolvedLabel(null);
  }, [payload]);

  if (!payload) return null;

  const handleClick = async (a: ModalAction) => {
    try {
      await dispatch(a);
      setResolvedLabel(a.label);
      setTimeout(onClose, 600);
    } catch {
      // error already in state
    }
  };

  return (
    <div className="fixed inset-0 z-[100] flex items-center justify-center bg-black/40 p-4">
      <div className="bg-white dark:bg-gray-800 rounded-card shadow-xl max-w-lg w-full p-6">
        <h2 className="text-lg font-semibold mb-2 text-neutral-dark-darkest dark:text-white">
          {payload.title}
        </h2>
        <div className="text-sm text-neutral-dark-medium dark:text-gray-300 whitespace-pre-wrap mb-4">
          {payload.body}
        </div>
        {error && (
          <div className="text-sm text-red-600 dark:text-red-400 mb-3">{error}</div>
        )}
        <div className="flex flex-wrap gap-2 justify-end">
          <button
            type="button"
            onClick={onClose}
            className="px-3 py-2 rounded-button text-sm text-neutral-dark-medium dark:text-gray-300 hover:bg-gray-100 dark:hover:bg-gray-700"
            disabled={submitting !== null}
          >
            Закрыть
          </button>
          {payload.actions.map((a) => (
            <button
              key={a.action_type + a.label}
              type="button"
              onClick={() => handleClick(a)}
              disabled={submitting !== null || resolvedLabel !== null}
              className="px-3 py-2 rounded-button text-sm bg-primary text-white hover:bg-primary-dark disabled:opacity-50"
            >
              {submitting === a.action_type ? '...' : resolvedLabel === a.label ? '✓' : a.label}
            </button>
          ))}
        </div>
      </div>
    </div>
  );
}
```

- [ ] **Step 3: Create ChatModalOverlay drop-in widget**

```tsx
// packages/frontend/src/app/components/ChatModalOverlay.tsx
'use client';
import { useEffect, useState } from 'react';
import { ModalRenderer } from './ModalRenderer';
import type { ModalPayload } from '../hooks/useModalAction';

/**
 * Drop-in overlay: scans `messages` for the latest AI message carrying
 * a `metadata.modal` payload and renders it through `<ModalRenderer />`.
 *
 * Encapsulates all the "which modal is active" logic so chat pages just
 * mount this component once and forget about it. When the chat-page
 * refactor (TECH_DEBT.md: "Унификация чат-страниц в общий ChatShell")
 * lands, this widget moves into ChatShell and pages stop mounting it
 * directly.
 */
export function ChatModalOverlay({
  messages,
}: {
  messages: Array<any>;
}) {
  const [active, setActive] = useState<ModalPayload | null>(null);

  useEffect(() => {
    // Walk newest → oldest, pick latest AI message with a non-empty payload.
    for (let i = messages.length - 1; i >= 0; i--) {
      const m = messages[i];
      const modal: ModalPayload | undefined = m?.metadata?.modal;
      if (modal && Array.isArray(modal.actions) && modal.actions.length > 0) {
        setActive(modal);
        return;
      }
    }
    setActive(null);
  }, [messages]);

  return <ModalRenderer payload={active} onClose={() => setActive(null)} />;
}
```

- [ ] **Step 4: Typecheck**

Run: `cd /root/webclient_rugpt/packages/frontend && npx tsc --noEmit 2>&1 | head`
Expected: no errors.

- [ ] **Step 5: Commit**

```bash
git add packages/frontend/src/app/components/ModalRenderer.tsx packages/frontend/src/app/components/ChatModalOverlay.tsx packages/frontend/src/app/hooks/useModalAction.ts
git commit -m "feat(frontend): ModalRenderer + ChatModalOverlay + useModalAction hook"
```

---

### Task 11: Mount `<ChatModalOverlay>` in direct-chat page

**Files:**
- Modify: `packages/frontend/src/app/chat/[username]/page.tsx`

Only this page mounts the overlay in v1 — invoice_clerk is a system user accessed via direct chat by username. The other three chat pages (`task/[id]`, `project/[id]`, `support/[id]`) will get the widget for free when the shared `ChatShell` refactor lands (see `webclient_rugpt/doc/TECH_DEBT.md` "Унификация чат-страниц"). Mounting in each of them now duplicates work that the refactor will undo.

- [ ] **Step 1: Add import**

In `packages/frontend/src/app/chat/[username]/page.tsx`, add to imports:
```tsx
import { ChatModalOverlay } from '../../components/ChatModalOverlay';
```

- [ ] **Step 2: Mount in JSX**

Right before the closing root `</div>` of the page component, add:
```tsx
<ChatModalOverlay messages={messages} />
```

(The `messages` variable already exists in this file — it's the array fed to the `messages.map(... <MessageBubble />)` loop.)

- [ ] **Step 3: Typecheck**

Run: `cd /root/webclient_rugpt/packages/frontend && npx tsc --noEmit 2>&1 | head`
Expected: no errors.

- [ ] **Step 4: Build**

Run: `cd /root/webclient_rugpt/packages/frontend && npm run build 2>&1 | tail -10`
Expected: 16 static pages generated, no errors.

- [ ] **Step 5: Commit**

```bash
git add packages/frontend/src/app/chat/\[username\]/page.tsx
git commit -m "feat(frontend): mount ChatModalOverlay in direct chat page"
```

---

### Task 12: (removed)

End-to-end manual smoke is deferred to Plan 3 where the first real action (`invoice_approve`) lands. Plan 1 ships only the infrastructure; unit + integration tests in Tasks 2, 5, 6, 8 cover the protocol mechanics.

---

## Self-review summary

1. **Spec coverage:** All spec sections covered by tasks — modal protocol (Tasks 2, 5, 6, 7, 8, 10, 11), action registry (Tasks 2, 3, 4), state derivation (Task 10 / 11 via ChatModalOverlay), per-role whitelist (Task 6), end-to-end smoke deferred to Plan 3. Invoice-specific sections of the spec intentionally NOT covered here — they belong to Plan 2 and Plan 3.
2. **Placeholder scan:** No "TBD", no "TODO", every step contains executable code or a concrete command.
3. **Type consistency:** Names match across tasks — `action_type` (snake_case throughout engine), `actionType` (camelCase in TS), `metadata.modal`, `allowed_action_types`, `<<MODAL_EMITTED>>`/`<</MODAL_EMITTED>>` markers used identically in Task 6 and Task 8.
4. **Scope:** Self-contained — produces a modal-protocol skeleton with empty production registry. End-to-end test arrives in Plan 3 when `invoice_approve` is the first real action.
