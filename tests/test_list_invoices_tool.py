"""Tests for the list_invoices LangChain tool.

Permission-scoped: admin sees all in org, non-admin sees only own.
"""
import os
import pytest
import pytest_asyncio
from datetime import date, datetime
from uuid import uuid4

import asyncpg
from langchain_core.runnables import RunnableConfig

from src.engine.services.engine_service import get_engine_service
from src.engine.agents.tools.list_invoices import (
    _filter_invoices,
    _format_invoice_page,
    _page_items,
    create_list_invoices_tool,
)
from src.engine.agents.tools.util import summary_budget
from src.engine.models.invoice import Invoice, InvoiceStatus
from src.engine.models.user_file import UserFile


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


@pytest.mark.asyncio(loop_scope="module")
async def test_list_displays_file_summary(env):
    inv = await env["engine"].invoice_storage.get_by_id(env["inv_w"])
    async with env["pool"].acquire() as conn:
        await conn.execute(
            "UPDATE user_files SET summary = $2, rag_status = 'indexed' WHERE id = $1",
            inv.file_id,
            "Поставка оборудования, сумма 125000 рублей, оплата до конца недели.",
        )

    tool = create_list_invoices_tool(env["engine"])
    output = await tool.ainvoke({"status": None}, config=_config(env["worker"], env["org_id"]))
    assert "summary:" in output
    assert "125000" in output


def _invoice(file_id, uploader_id, created_at, *, status=InvoiceStatus.CREATED):
    return Invoice(
        id=uuid4(),
        org_id=uuid4(),
        file_id=file_id,
        uploaded_by_user_id=uploader_id,
        due_date=date(2026, 5, 21),
        status=status,
        approved_by_user_id=None,
        approved_at=None,
        rejected_at=None,
        rejection_reason=None,
        processed_at=None,
        processed_by_user_id=None,
        is_active=True,
        created_at=created_at,
        updated_at=created_at,
    )


def test_invoice_pagination_page_size():
    uploader_id = uuid4()
    invoices = [_invoice(uuid4(), uploader_id, datetime(2026, 5, 21)) for _ in range(31)]

    first_page, page, total_pages, start, end = _page_items(invoices, 1)
    assert len(first_page) == 30
    assert (page, total_pages, start, end) == (1, 2, 0, 30)

    second_page, page, total_pages, start, end = _page_items(invoices, 2)
    assert len(second_page) == 1
    assert (page, total_pages, start, end) == (2, 2, 30, 31)


def test_invoice_filters_intersect_uploader_due_and_created_dates():
    uploader_a = uuid4()
    uploader_b = uuid4()
    inv_a = _invoice(
        uuid4(),
        uploader_a,
        datetime(2026, 5, 20, 12, 0, 0),
    )
    inv_b = _invoice(
        uuid4(),
        uploader_b,
        datetime(2026, 5, 21, 12, 0, 0),
    )
    inv_b.due_date = date(2026, 5, 22)

    filtered = _filter_invoices(
        [inv_a, inv_b],
        uploaded_by_user_id=uploader_a,
        due_from=date(2026, 5, 20),
        due_to=date(2026, 5, 21),
        created_from=date(2026, 5, 20),
        created_to=date(2026, 5, 20),
    )

    assert filtered == [inv_a]


def test_invoice_due_filter_excludes_empty_due_date():
    uploader_id = uuid4()
    inv = _invoice(uuid4(), uploader_id, datetime(2026, 5, 20, 12, 0, 0))
    inv.due_date = None

    filtered = _filter_invoices(
        [inv],
        uploaded_by_user_id=None,
        due_from=date(2026, 5, 20),
        due_to=None,
        created_from=None,
        created_to=None,
    )

    assert filtered == []


def test_invoice_summary_budget_truncates_long_summary(monkeypatch):
    monkeypatch.setattr(summary_budget, "count_tokens", lambda text: len(text.split()))
    monkeypatch.setattr(
        summary_budget,
        "cut_text_by_token_count",
        lambda text, limit: " ".join(text.split()[:limit]),
    )

    uploader_id = uuid4()
    file_id = uuid4()
    inv = _invoice(file_id, uploader_id, datetime(2026, 5, 21))
    file = UserFile(
        id=file_id,
        user_id=uploader_id,
        uploaded_by_user_id=uploader_id,
        original_filename="invoice.pdf",
        summary=" ".join(f"word{i}" for i in range(300)),
        rag_status="indexed",
    )

    output, tokens_spent = _format_invoice_page(
        [inv],
        page=1,
        total_pages=1,
        start=0,
        end=1,
        total=1,
        users_by_id={uploader_id: "Uploader"},
        files_by_id={file_id: file},
        summary_tokens_spent_before=0,
    )

    assert "Invoices 1-1 of 1 (page 1/1):" in output
    assert "summary: \"" in output
    assert "..." in output
    assert tokens_spent <= 80
