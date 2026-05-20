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


@pytest_asyncio.fixture(loop_scope="module")
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


@pytest.mark.asyncio(loop_scope="module")
async def test_admin_sees_invoice(env):
    tool = create_get_invoice_tool(env["engine"])
    out = await tool.ainvoke({"invoice_id": str(env["invoice_id"])},
                              config=_config(env["admin"], env["org_id"]))
    assert "admin.pdf" in out


@pytest.mark.asyncio(loop_scope="module")
async def test_worker_cannot_see_admins_invoice(env):
    tool = create_get_invoice_tool(env["engine"])
    out = await tool.ainvoke({"invoice_id": str(env["invoice_id"])},
                              config=_config(env["worker"], env["org_id"]))
    assert "not found" in out.lower() or "access denied" in out.lower() or "not visible" in out.lower()


@pytest.mark.asyncio(loop_scope="module")
async def test_invalid_id_returns_error(env):
    tool = create_get_invoice_tool(env["engine"])
    out = await tool.ainvoke({"invoice_id": "not-a-uuid"},
                              config=_config(env["admin"], env["org_id"]))
    assert "error" in out.lower() or "invalid" in out.lower()
