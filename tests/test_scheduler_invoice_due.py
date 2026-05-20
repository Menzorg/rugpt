"""Scheduler invoice-due reminder fires exactly once per (invoice, day)."""
import os
import pytest
import pytest_asyncio
from datetime import date
from uuid import uuid4

import asyncpg

from src.engine.services.engine_service import get_engine_service


DSN = os.environ.get("DATABASE_URL", "postgresql://postgres@localhost/rugpt")


@pytest_asyncio.fixture(loop_scope="module")
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


@pytest.mark.asyncio(loop_scope="module")
async def test_notification_created_for_due_invoice(env):
    await env["engine"].scheduler_service._notify_invoice_due(env["org"])
    async with env["pool"].acquire() as conn:
        count = await conn.fetchval(
            "SELECT COUNT(*) FROM in_app_notifications "
            "WHERE user_id = $1 AND type = 'invoice_due'",
            env["accountant"],
        )
    assert count == 1


@pytest.mark.asyncio(loop_scope="module")
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


@pytest.mark.asyncio(loop_scope="module")
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
