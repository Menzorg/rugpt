"""PATCH /organizations/{id} smoke test: accountant_user_id field round-trips
through model -> storage -> service -> route -> response.

Pattern matches test_folders_api.py: httpx.AsyncClient + ASGITransport with
module-scoped loop, since asyncpg pools and TestClient's anyio portal conflict.
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
async def admin_ctx(engine_initialized):
    org_s = OrgStorage(Config.get_postgres_dsn())
    user_s = UserStorage(Config.get_postgres_dsn())
    await org_s.init()
    await user_s.init()

    org = await org_s.create(Organization(name=f"o-{uuid4().hex[:6]}", slug=f"o-{uuid4().hex[:6]}"))
    admin = await user_s.create(User(
        org_id=org.id, name="adm", username=f"adm-{uuid4().hex[:6]}",
        email=f"{uuid4().hex[:6]}@x.x", password_hash="", is_admin=True, is_active=True,
    ))
    accountant = await user_s.create(User(
        org_id=org.id, name="acc", username=f"acc-{uuid4().hex[:6]}",
        email=f"{uuid4().hex[:6]}@x.x", password_hash="", is_admin=False, is_active=True,
    ))

    # NB: real JWT-derived dict has stringified UUIDs; PATCH handler uses
    # current_user["user_id"] only via user_storage.get_by_id which tolerates
    # Real get_current_user returns user_id/org_id as UUID instances. Match prod shape.
    async def _override():
        return {"user_id": admin.id, "org_id": org.id, "is_admin": True}
    app.dependency_overrides[get_current_user] = _override

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
        yield {"org_id": org.id, "admin_id": admin.id, "accountant_id": accountant.id, "client": client}

    app.dependency_overrides.clear()
    await user_s.execute("DELETE FROM users WHERE org_id=$1", org.id)
    await org_s.execute("DELETE FROM organizations WHERE id=$1", org.id)
    await org_s.close()
    await user_s.close()


@pytest.mark.asyncio(loop_scope="module")
async def test_patch_sets_accountant_user_id(admin_ctx):
    client = admin_ctx["client"]
    org_id = str(admin_ctx["org_id"])
    acc_id = str(admin_ctx["accountant_id"])

    r = await client.patch(f"/api/v1/organizations/{org_id}", json={"accountant_user_id": acc_id})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["accountant_user_id"] == acc_id

    # Round-trip via GET
    r2 = await client.get(f"/api/v1/organizations/{org_id}")
    assert r2.status_code == 200
    assert r2.json()["accountant_user_id"] == acc_id


@pytest.mark.asyncio(loop_scope="module")
async def test_patch_without_accountant_does_not_clear(admin_ctx):
    """v1 semantics: omitting accountant_user_id leaves existing value intact."""
    client = admin_ctx["client"]
    org_id = str(admin_ctx["org_id"])
    acc_id = str(admin_ctx["accountant_id"])

    # First set it
    await client.patch(f"/api/v1/organizations/{org_id}", json={"accountant_user_id": acc_id})

    # Then PATCH other fields without accountant_user_id
    r = await client.patch(f"/api/v1/organizations/{org_id}", json={"description": "new desc"})
    assert r.status_code == 200, r.text
    assert r.json()["accountant_user_id"] == acc_id  # unchanged
    assert r.json()["description"] == "new desc"


@pytest.mark.asyncio(loop_scope="module")
async def test_patch_invalid_accountant_uuid_400(admin_ctx):
    client = admin_ctx["client"]
    org_id = str(admin_ctx["org_id"])
    r = await client.patch(f"/api/v1/organizations/{org_id}", json={"accountant_user_id": "not-a-uuid"})
    assert r.status_code == 400, r.text
