"""Integration test: invoice action handlers dispatched via registry."""
import os
import pytest
import pytest_asyncio
from uuid import uuid4

import asyncpg

from src.engine.services.engine_service import get_engine_service
from src.engine.models.invoice import InvoiceStatus

DSN = os.environ.get("DATABASE_URL", "postgresql://postgres@localhost/rugpt")


# Module-scoped event loop: the EngineService singleton's asyncpg pools die
# when the per-function loop closes. All fixtures + tests share one loop per
# module — matches the pattern in test_invoice_service.py and test_folders_api.py.
@pytest_asyncio.fixture(loop_scope="module")
async def env():
    engine = get_engine_service()
    await engine.initialize()
    pool = await asyncpg.create_pool(DSN)
    async with pool.acquire() as conn:
        org = await conn.fetchval(
            "INSERT INTO organizations (id, name, slug) VALUES (gen_random_uuid(), 'invact', $1) RETURNING id",
            f"invact_{uuid4().hex[:8]}"
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
        # Set accountant_user_id to admin for mark_processed tests
        await conn.execute("UPDATE organizations SET accountant_user_id = $1 WHERE id = $2", admin, org)

    inv = await engine.invoice_service.upload(org, worker, "x.pdf", b"x", None)
    admin_user = await engine.user_storage.get_by_id(admin)
    worker_user = await engine.user_storage.get_by_id(worker)
    yield {"engine": engine, "invoice_id": inv.id, "admin": admin_user, "worker": worker_user,
            "org_id": org, "pool": pool}
    async with pool.acquire() as conn:
        await conn.execute("DELETE FROM invoices WHERE org_id = $1", org)
        await conn.execute("DELETE FROM user_files WHERE org_id = $1", org)
        await conn.execute("DELETE FROM users WHERE org_id = $1", org)
        await conn.execute("DELETE FROM organizations WHERE id = $1", org)
    await pool.close()


@pytest.mark.asyncio(loop_scope="module")
async def test_invoice_approve_admin_succeeds(env):
    result = await env["engine"].action_registry.dispatch(
        engine=env["engine"],
        action_type="invoice_approve",
        params={"invoice_id": str(env["invoice_id"])},
        user=env["admin"],
    )
    assert result["ok"] is True


@pytest.mark.asyncio(loop_scope="module")
async def test_invoice_approve_non_admin_denied(env):
    from src.engine.actions.registry import PermissionDeniedError
    with pytest.raises(PermissionDeniedError):
        await env["engine"].action_registry.dispatch(
            engine=env["engine"],
            action_type="invoice_approve",
            params={"invoice_id": str(env["invoice_id"])},
            user=env["worker"],
        )


@pytest.mark.asyncio(loop_scope="module")
async def test_invoice_reject_with_reason(env):
    result = await env["engine"].action_registry.dispatch(
        engine=env["engine"],
        action_type="invoice_reject",
        params={"invoice_id": str(env["invoice_id"]), "reason": "duplicate"},
        user=env["admin"],
    )
    assert result["ok"] is True
    inv = await env["engine"].invoice_service._invoice_storage.get_by_id(env["invoice_id"])
    assert inv.rejection_reason == "duplicate"


@pytest.mark.asyncio(loop_scope="module")
async def test_invoice_mark_processed_by_accountant(env):
    # Admin is also accountant (per fixture). Approve first.
    await env["engine"].action_registry.dispatch(
        engine=env["engine"], action_type="invoice_approve",
        params={"invoice_id": str(env["invoice_id"])}, user=env["admin"],
    )
    result = await env["engine"].action_registry.dispatch(
        engine=env["engine"], action_type="invoice_mark_processed",
        params={"invoice_id": str(env["invoice_id"])}, user=env["admin"],
    )
    assert result["ok"] is True


@pytest.mark.asyncio(loop_scope="module")
async def test_invoice_mark_processed_by_worker_denied(env):
    from src.engine.actions.registry import PermissionDeniedError
    await env["engine"].action_registry.dispatch(
        engine=env["engine"], action_type="invoice_approve",
        params={"invoice_id": str(env["invoice_id"])}, user=env["admin"],
    )
    with pytest.raises(PermissionDeniedError):
        await env["engine"].action_registry.dispatch(
            engine=env["engine"], action_type="invoice_mark_processed",
            params={"invoice_id": str(env["invoice_id"])}, user=env["worker"],
        )
