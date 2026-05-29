"""Integration tests for invoice routes (upload + list + get)."""
import io
import os
import pytest
import pytest_asyncio
from uuid import uuid4

import asyncpg
from httpx import AsyncClient, ASGITransport

from src.engine.app import app
from src.engine.services.engine_service import get_engine_service
from src.engine.routes.auth import create_token

DSN = os.environ.get("DATABASE_URL", "postgresql://postgres@localhost/rugpt")


@pytest_asyncio.fixture(loop_scope="module")
async def env():
    engine = get_engine_service()
    await engine.initialize()
    pool = await asyncpg.create_pool(DSN)
    async with pool.acquire() as conn:
        org = await conn.fetchval(
            "INSERT INTO organizations (id, name, slug) VALUES (gen_random_uuid(), 'r', $1) RETURNING id",
            f"r_{uuid4().hex[:8]}"
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
    admin_tok = create_token(admin, org, is_admin=True)
    worker_tok = create_token(worker, org, is_admin=False)
    yield {"admin_tok": admin_tok, "worker_tok": worker_tok, "org_id": org,
            "admin": admin, "worker": worker, "pool": pool}
    async with pool.acquire() as conn:
        await conn.execute("DELETE FROM invoices WHERE org_id = $1", org)
        await conn.execute("DELETE FROM user_files WHERE org_id = $1", org)
        await conn.execute("DELETE FROM users WHERE org_id = $1", org)
        await conn.execute("DELETE FROM organizations WHERE id = $1", org)
    await pool.close()


def _multipart(file_bytes: bytes, filename: str, due_date: str):
    return {
        "file": (filename, io.BytesIO(file_bytes), "application/pdf"),
    }, {"due_date": due_date}


@pytest.mark.asyncio(loop_scope="module")
async def test_worker_upload_then_list_returns_one(env):
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        files, data = _multipart(b"%PDF-1.4", "a.pdf", "2026-08-01")
        r = await client.post(
            "/api/v1/invoices/", files=files, data=data,
            headers={"Authorization": f"Bearer {env['worker_tok']}"},
        )
        assert r.status_code == 200, r.text
        inv_id = r.json()["id"]

        r2 = await client.get(
            "/api/v1/invoices/",
            headers={"Authorization": f"Bearer {env['worker_tok']}"},
        )
        assert r2.status_code == 200
        items = r2.json()
        assert len(items) == 1
        assert items[0]["id"] == inv_id


@pytest.mark.asyncio(loop_scope="module")
async def test_admin_sees_all_worker_only_own(env):
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # Worker uploads one
        files, data = _multipart(b"%PDF-1.4", "w.pdf", "2026-08-01")
        await client.post("/api/v1/invoices/", files=files, data=data,
                           headers={"Authorization": f"Bearer {env['worker_tok']}"})
        # Admin uploads one
        files, data = _multipart(b"%PDF-1.4", "a.pdf", "2026-08-02")
        await client.post("/api/v1/invoices/", files=files, data=data,
                           headers={"Authorization": f"Bearer {env['admin_tok']}"})

        r_admin = await client.get("/api/v1/invoices/",
                                    headers={"Authorization": f"Bearer {env['admin_tok']}"})
        r_worker = await client.get("/api/v1/invoices/",
                                     headers={"Authorization": f"Bearer {env['worker_tok']}"})
        assert len(r_admin.json()) == 2
        assert len(r_worker.json()) == 1


@pytest.mark.asyncio(loop_scope="module")
async def test_get_unauthenticated_returns_401(env):
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        r = await client.get("/api/v1/invoices/")
    assert r.status_code == 401


@pytest.mark.asyncio(loop_scope="module")
async def test_worker_cannot_view_others_invoice_by_id(env):
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # Admin uploads
        files, data = _multipart(b"%PDF-1.4", "a.pdf", "2026-08-01")
        r = await client.post("/api/v1/invoices/", files=files, data=data,
                               headers={"Authorization": f"Bearer {env['admin_tok']}"})
        inv_id = r.json()["id"]
        # Worker tries to get
        r2 = await client.get(f"/api/v1/invoices/{inv_id}",
                               headers={"Authorization": f"Bearer {env['worker_tok']}"})
    assert r2.status_code == 404
