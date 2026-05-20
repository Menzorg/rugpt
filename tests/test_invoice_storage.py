"""Integration tests for InvoiceStorage. Real DB."""
import os
import pytest
import pytest_asyncio
from datetime import date
from uuid import uuid4

import asyncpg

from src.engine.storage.invoice_storage import InvoiceStorage
from src.engine.models.invoice import Invoice, InvoiceStatus


DSN = os.environ.get("DATABASE_URL", "postgresql://postgres@localhost/rugpt")


@pytest_asyncio.fixture
async def env():
    pool = await asyncpg.create_pool(DSN)
    async with pool.acquire() as conn:
        org = await conn.fetchval(
            "INSERT INTO organizations (id, name, slug) VALUES (gen_random_uuid(), 'inv', $1) RETURNING id",
            f"inv_{uuid4().hex[:8]}"
        )
        u1 = await conn.fetchval(
            "INSERT INTO users (id, org_id, username, name, password_hash, email, is_active) "
            "VALUES (gen_random_uuid(), $1, $2, $2, 'x', $3, true) RETURNING id",
            org, f"u1_{uuid4().hex[:6]}", f"u1_{uuid4()}@t.local"
        )
        u2 = await conn.fetchval(
            "INSERT INTO users (id, org_id, username, name, password_hash, email, is_active) "
            "VALUES (gen_random_uuid(), $1, $2, $2, 'x', $3, true) RETURNING id",
            org, f"u2_{uuid4().hex[:6]}", f"u2_{uuid4()}@t.local"
        )
        # one stub user_file per user
        f1 = await conn.fetchval(
            "INSERT INTO user_files (id, user_id, org_id, uploaded_by_user_id, storage_key, "
            "original_filename, file_type, file_size, content_hash, summary, is_table, is_public, rag_status) "
            "VALUES (gen_random_uuid(), $1, $2, $1, 'k1', 'a.pdf', 'pdf', 1, 'h1', '', false, false, 'pending') "
            "RETURNING id",
            u1, org
        )
        f2 = await conn.fetchval(
            "INSERT INTO user_files (id, user_id, org_id, uploaded_by_user_id, storage_key, "
            "original_filename, file_type, file_size, content_hash, summary, is_table, is_public, rag_status) "
            "VALUES (gen_random_uuid(), $1, $2, $1, 'k2', 'b.pdf', 'pdf', 1, 'h2', '', false, false, 'pending') "
            "RETURNING id",
            u2, org
        )
    storage = InvoiceStorage(DSN)
    await storage.init()
    yield {"storage": storage, "org_id": org, "u1": u1, "u2": u2, "f1": f1, "f2": f2, "pool": pool}
    async with pool.acquire() as conn:
        await conn.execute("DELETE FROM invoices WHERE org_id = $1", org)
        await conn.execute("DELETE FROM user_files WHERE org_id = $1", org)
        await conn.execute("DELETE FROM users WHERE org_id = $1", org)
        await conn.execute("DELETE FROM organizations WHERE id = $1", org)
    await storage.close()
    await pool.close()


@pytest.mark.asyncio
async def test_create_and_get(env):
    inv = await env["storage"].create(
        org_id=env["org_id"], file_id=env["f1"],
        uploaded_by_user_id=env["u1"], due_date=date(2026, 6, 1),
    )
    fetched = await env["storage"].get_by_id(inv.id)
    assert fetched is not None
    assert fetched.status == InvoiceStatus.CREATED
    assert fetched.due_date == date(2026, 6, 1)


@pytest.mark.asyncio
async def test_list_admin_sees_all_in_org(env):
    await env["storage"].create(env["org_id"], env["f1"], env["u1"], date(2026, 6, 1))
    await env["storage"].create(env["org_id"], env["f2"], env["u2"], date(2026, 6, 2))
    rows = await env["storage"].list_by_org(env["org_id"])
    assert len(rows) == 2


@pytest.mark.asyncio
async def test_list_user_sees_only_own(env):
    await env["storage"].create(env["org_id"], env["f1"], env["u1"], date(2026, 6, 1))
    await env["storage"].create(env["org_id"], env["f2"], env["u2"], date(2026, 6, 2))
    rows = await env["storage"].list_for_user(env["org_id"], env["u1"])
    assert len(rows) == 1
    assert rows[0].uploaded_by_user_id == env["u1"]


@pytest.mark.asyncio
async def test_update_status(env):
    inv = await env["storage"].create(env["org_id"], env["f1"], env["u1"], None)
    updated = await env["storage"].update_status(
        invoice_id=inv.id,
        status=InvoiceStatus.APPROVED,
        actor_user_id=env["u2"],
    )
    assert updated.status == InvoiceStatus.APPROVED
    assert updated.approved_by_user_id == env["u2"]
    assert updated.approved_at is not None


@pytest.mark.asyncio
async def test_list_due_today_or_tomorrow_scheduler_query(env):
    await env["storage"].create(env["org_id"], env["f1"], env["u1"], date(2026, 6, 1))
    inv2 = await env["storage"].create(env["org_id"], env["f2"], env["u2"], date(2026, 6, 1))
    await env["storage"].update_status(inv2.id, InvoiceStatus.APPROVED, env["u1"])
    rows = await env["storage"].list_due_today_or_tomorrow(env["org_id"], date(2026, 5, 31))
    # Only inv2 is approved+pending+due_date in (today, today+1); the unapproved one is excluded.
    assert len(rows) == 1
    assert rows[0].id == inv2.id
