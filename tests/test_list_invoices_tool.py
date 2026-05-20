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


@pytest_asyncio.fixture(loop_scope="module")
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


@pytest.mark.asyncio(loop_scope="module")
async def test_admin_sees_all(env):
    tool = create_list_invoices_tool(env["engine"])
    output = await tool.ainvoke({"status": None}, config=_config(env["admin"], env["org_id"]))
    assert str(env["inv_w"]) in output
    assert str(env["inv_a"]) in output


@pytest.mark.asyncio(loop_scope="module")
async def test_worker_sees_only_own(env):
    tool = create_list_invoices_tool(env["engine"])
    output = await tool.ainvoke({"status": None}, config=_config(env["worker"], env["org_id"]))
    assert str(env["inv_w"]) in output
    assert str(env["inv_a"]) not in output


@pytest.mark.asyncio(loop_scope="module")
async def test_status_filter(env):
    await env["engine"].invoice_service.approve(env["inv_a"], env["admin"])
    tool = create_list_invoices_tool(env["engine"])
    output = await tool.ainvoke({"status": "approved"}, config=_config(env["admin"], env["org_id"]))
    assert str(env["inv_a"]) in output
    assert str(env["inv_w"]) not in output  # is in created
