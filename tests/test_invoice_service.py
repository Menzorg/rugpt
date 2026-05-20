"""InvoiceService orchestrates file upload + invoice row creation."""
import os
import pytest
import pytest_asyncio
from datetime import date
from uuid import uuid4

import asyncpg

from src.engine.services.engine_service import get_engine_service
from src.engine.models.invoice import InvoiceStatus


DSN = os.environ.get("DATABASE_URL", "postgresql://postgres@localhost/rugpt")


# Module-scoped event loop is required: the EngineService singleton creates
# asyncpg pools on first use and those pools die when the per-function loop
# closes. All fixtures + tests share one loop per module.
@pytest_asyncio.fixture(loop_scope="module")
async def env():
    engine = get_engine_service()
    await engine.initialize()
    pool = await asyncpg.create_pool(DSN)
    async with pool.acquire() as conn:
        org = await conn.fetchval(
            "INSERT INTO organizations (id, name, slug) VALUES (gen_random_uuid(), 'invsvc', $1) RETURNING id",
            f"invsvc_{uuid4().hex[:8]}"
        )
        u = await conn.fetchval(
            "INSERT INTO users (id, org_id, username, name, password_hash, email, is_active) "
            "VALUES (gen_random_uuid(), $1, $2, $2, 'x', $3, true) RETURNING id",
            org, f"u_{uuid4().hex[:6]}", f"u_{uuid4()}@t.local"
        )
    yield {"engine": engine, "org_id": org, "user_id": u, "pool": pool}
    async with pool.acquire() as conn:
        await conn.execute("DELETE FROM invoices WHERE org_id = $1", org)
        await conn.execute("DELETE FROM user_files WHERE org_id = $1", org)
        await conn.execute("DELETE FROM users WHERE org_id = $1", org)
        await conn.execute("DELETE FROM organizations WHERE id = $1", org)
    await pool.close()


@pytest.mark.asyncio(loop_scope="module")
async def test_upload_invoice_creates_user_file_and_invoice(env):
    data = b"%PDF-1.4 fake-invoice-content"
    inv = await env["engine"].invoice_service.upload(
        org_id=env["org_id"],
        uploader_user_id=env["user_id"],
        filename="invoice-123.pdf",
        data=data,
        due_date=date(2026, 7, 1),
    )
    assert inv.status == InvoiceStatus.CREATED
    assert inv.uploaded_by_user_id == env["user_id"]
    assert inv.due_date == date(2026, 7, 1)
    # user_file row exists
    async with env["pool"].acquire() as conn:
        f = await conn.fetchrow("SELECT * FROM user_files WHERE id = $1", inv.file_id)
        assert f is not None
        assert f["original_filename"] == "invoice-123.pdf"


@pytest.mark.asyncio(loop_scope="module")
async def test_approve_transition(env):
    inv = await env["engine"].invoice_service.upload(
        env["org_id"], env["user_id"], "x.pdf", b"x", date(2026, 7, 1),
    )
    updated = await env["engine"].invoice_service.approve(inv.id, env["user_id"])
    assert updated.status == InvoiceStatus.APPROVED


@pytest.mark.asyncio(loop_scope="module")
async def test_reject_transition(env):
    inv = await env["engine"].invoice_service.upload(
        env["org_id"], env["user_id"], "x.pdf", b"x", None,
    )
    updated = await env["engine"].invoice_service.reject(inv.id, env["user_id"], "not ours")
    assert updated.status == InvoiceStatus.REJECTED
    assert updated.rejection_reason == "not ours"


@pytest.mark.asyncio(loop_scope="module")
async def test_mark_processed_only_after_approved(env):
    inv = await env["engine"].invoice_service.upload(
        env["org_id"], env["user_id"], "x.pdf", b"x", None,
    )
    with pytest.raises(ValueError):
        await env["engine"].invoice_service.mark_processed(inv.id, env["user_id"])
    await env["engine"].invoice_service.approve(inv.id, env["user_id"])
    processed = await env["engine"].invoice_service.mark_processed(inv.id, env["user_id"])
    assert processed.status == InvoiceStatus.PROCESSED
