"""Smoke tests for /folders API routes using FastAPI + async test client + dependency override.

Implementation note: The plan originally specified the sync `fastapi.testclient.TestClient`,
but that spawns its own event loop via anyio portal and conflicts with the asyncpg pools
created on the pytest-asyncio loop ("attached to a different loop").
We use `httpx.AsyncClient + ASGITransport` with `loop_scope="module"` instead — same
pattern as `test_routes_task_participants.py`. Behavior under test is identical;
only the transport differs.
"""
import pytest
import pytest_asyncio
from uuid import uuid4
from httpx import AsyncClient, ASGITransport

from src.engine.app import app
from src.engine.services.engine_service import get_engine_service
from src.engine.routes.auth import get_current_user
from src.engine.models.organization import Organization
from src.engine.models.user import User
from src.engine.config import Config
from src.engine.storage.org_storage import OrgStorage
from src.engine.storage.user_storage import UserStorage


@pytest_asyncio.fixture(scope="module", loop_scope="module")
async def engine_initialized():
    engine = get_engine_service()
    await engine.initialize()
    yield engine
    try:
        await engine.close()
    except Exception:
        pass


@pytest_asyncio.fixture(loop_scope="module")
async def user_ctx(engine_initialized):
    org_s = OrgStorage(Config.get_postgres_dsn())
    user_s = UserStorage(Config.get_postgres_dsn())
    await org_s.init()
    await user_s.init()
    org = await org_s.create(Organization(name=f"o-{uuid4().hex[:6]}", slug=f"o-{uuid4().hex[:6]}"))
    u = await user_s.create(User(
        org_id=org.id, name="t", username=f"u-{uuid4().hex[:6]}",
        email=f"{uuid4().hex[:6]}@x.x", password_hash="", is_admin=False,
    ))

    async def _override():
        return {"user_id": u.id, "org_id": org.id, "is_admin": False}
    app.dependency_overrides[get_current_user] = _override

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
        yield {"user_id": u.id, "org_id": org.id, "client": client}

    app.dependency_overrides.clear()
    await user_s.execute("DELETE FROM user_files WHERE user_id=$1", u.id)
    await user_s.execute("DELETE FROM user_file_folders WHERE user_id=$1", u.id)
    await user_s.execute("DELETE FROM users WHERE id=$1", u.id)
    await org_s.execute("DELETE FROM organizations WHERE id=$1", org.id)
    await org_s.close()
    await user_s.close()


@pytest.mark.asyncio(loop_scope="module")
async def test_create_then_list_then_get_tree(user_ctx):
    client = user_ctx["client"]
    r = await client.post("/api/v1/folders", json={"name": "Docs"})
    assert r.status_code == 200, r.text
    folder = r.json()
    assert folder["name"] == "Docs"
    assert folder["parent_folder_id"] is None

    r = await client.get("/api/v1/folders")
    assert r.status_code == 200
    assert any(f["id"] == folder["id"] for f in r.json())

    r = await client.get("/api/v1/folders/tree")
    assert r.status_code == 200
    tree = r.json()
    assert any(n["id"] == folder["id"] for n in tree)


@pytest.mark.asyncio(loop_scope="module")
async def test_create_duplicate_name_returns_409(user_ctx):
    client = user_ctx["client"]
    await client.post("/api/v1/folders", json={"name": "Dup"})
    r = await client.post("/api/v1/folders", json={"name": "Dup"})
    assert r.status_code == 409
    assert r.json()["detail"]["code"] == "DUPLICATE_NAME"


@pytest.mark.asyncio(loop_scope="module")
async def test_empty_name_returns_400(user_ctx):
    client = user_ctx["client"]
    r = await client.post("/api/v1/folders", json={"name": "   "})
    assert r.status_code == 400
    assert r.json()["detail"]["code"] == "EMPTY_NAME"


@pytest.mark.asyncio(loop_scope="module")
async def test_rename_and_move(user_ctx):
    client = user_ctx["client"]
    parent = (await client.post("/api/v1/folders", json={"name": "Parent"})).json()
    child = (await client.post(
        "/api/v1/folders", json={"name": "Child", "parent_folder_id": parent["id"]}
    )).json()

    r = await client.patch(f"/api/v1/folders/{child['id']}", json={"name": "Renamed"})
    assert r.status_code == 200
    assert r.json()["name"] == "Renamed"

    r = await client.patch(f"/api/v1/folders/{child['id']}", json={"parent_folder_id": None})
    assert r.status_code == 200
    assert r.json()["parent_folder_id"] is None


@pytest.mark.asyncio(loop_scope="module")
async def test_cyclic_move_blocked(user_ctx):
    client = user_ctx["client"]
    a = (await client.post("/api/v1/folders", json={"name": "A"})).json()
    b = (await client.post(
        "/api/v1/folders", json={"name": "B", "parent_folder_id": a["id"]}
    )).json()
    r = await client.patch(f"/api/v1/folders/{a['id']}", json={"parent_folder_id": b["id"]})
    assert r.status_code == 400
    assert r.json()["detail"]["code"] == "CYCLIC_MOVE"


@pytest.mark.asyncio(loop_scope="module")
async def test_delete_cascade_returns_counts(user_ctx):
    client = user_ctx["client"]
    a = (await client.post("/api/v1/folders", json={"name": "ToDelete"})).json()
    await client.post("/api/v1/folders", json={"name": "Child", "parent_folder_id": a["id"]})
    r = await client.delete(f"/api/v1/folders/{a['id']}")
    assert r.status_code == 200
    out = r.json()
    assert out["success"] is True
    assert out["deleted_folders"] == 2
    assert out["deleted_files"] == 0
