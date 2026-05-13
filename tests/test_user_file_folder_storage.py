"""Integration tests for UserFileFolderStorage against real PostgreSQL."""
import pytest
import pytest_asyncio
from uuid import uuid4

import asyncpg

from src.engine.config import Config
from src.engine.storage.user_file_folder_storage import UserFileFolderStorage
from src.engine.models.user_file_folder import UserFileFolder
from src.engine.storage.org_storage import OrgStorage
from src.engine.storage.user_storage import UserStorage
from src.engine.models.organization import Organization
from src.engine.models.user import User


pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture
async def storage():
    s = UserFileFolderStorage(Config.get_postgres_dsn())
    await s.init()
    yield s
    await s.close()


@pytest_asyncio.fixture
async def fixtures():
    """Create an org + user, return their ids + cleanup."""
    org_storage = OrgStorage(Config.get_postgres_dsn())
    user_storage = UserStorage(Config.get_postgres_dsn())
    await org_storage.init()
    await user_storage.init()

    org = await org_storage.create(Organization(
        name=f"test-org-{uuid4().hex[:8]}",
        slug=f"test-{uuid4().hex[:8]}",
    ))
    user = await user_storage.create(User(
        org_id=org.id, name="Tester", username=f"tester-{uuid4().hex[:8]}",
        email=f"{uuid4().hex[:8]}@test.local", password_hash="", is_admin=False,
    ))
    yield {"org_id": org.id, "user_id": user.id}
    await user_storage.execute("DELETE FROM user_file_folders WHERE user_id = $1", user.id)
    await org_storage.close()
    await user_storage.close()


async def test_create_root_folder(storage, fixtures):
    f = UserFileFolder(user_id=fixtures["user_id"], org_id=fixtures["org_id"], name="Root1")
    saved = await storage.create(f)
    assert saved.id == f.id
    assert saved.parent_folder_id is None
    assert saved.is_active is True


async def test_create_nested_folder(storage, fixtures):
    parent = await storage.create(UserFileFolder(
        user_id=fixtures["user_id"], org_id=fixtures["org_id"], name="Parent"))
    child = await storage.create(UserFileFolder(
        user_id=fixtures["user_id"], org_id=fixtures["org_id"],
        parent_folder_id=parent.id, name="Child"))
    assert child.parent_folder_id == parent.id


async def test_list_subtree_ids_includes_self(storage, fixtures):
    a = await storage.create(UserFileFolder(user_id=fixtures["user_id"], org_id=fixtures["org_id"], name="A"))
    b = await storage.create(UserFileFolder(user_id=fixtures["user_id"], org_id=fixtures["org_id"], parent_folder_id=a.id, name="B"))
    c = await storage.create(UserFileFolder(user_id=fixtures["user_id"], org_id=fixtures["org_id"], parent_folder_id=b.id, name="C"))
    ids = await storage.list_subtree_ids(a.id)
    assert ids == {a.id, b.id, c.id}


async def test_list_subtree_ids_inactive_excluded(storage, fixtures):
    a = await storage.create(UserFileFolder(user_id=fixtures["user_id"], org_id=fixtures["org_id"], name="A"))
    b = await storage.create(UserFileFolder(user_id=fixtures["user_id"], org_id=fixtures["org_id"], parent_folder_id=a.id, name="B"))
    await storage.execute("UPDATE user_file_folders SET is_active = false WHERE id = $1", b.id)
    ids = await storage.list_subtree_ids(a.id)
    assert ids == {a.id}


async def test_deactivate_subtree_returns_ids(storage, fixtures):
    a = await storage.create(UserFileFolder(user_id=fixtures["user_id"], org_id=fixtures["org_id"], name="A"))
    b = await storage.create(UserFileFolder(user_id=fixtures["user_id"], org_id=fixtures["org_id"], parent_folder_id=a.id, name="B"))
    deactivated = await storage.deactivate_subtree(a.id)
    assert set(deactivated) == {a.id, b.id}
    assert await storage.get_by_id(a.id) is None


async def test_unique_name_in_same_parent(storage, fixtures):
    await storage.create(UserFileFolder(user_id=fixtures["user_id"], org_id=fixtures["org_id"], name="Same"))
    with pytest.raises(asyncpg.UniqueViolationError):
        await storage.create(UserFileFolder(user_id=fixtures["user_id"], org_id=fixtures["org_id"], name="Same"))


async def test_unique_name_case_insensitive(storage, fixtures):
    await storage.create(UserFileFolder(user_id=fixtures["user_id"], org_id=fixtures["org_id"], name="docs"))
    with pytest.raises(asyncpg.UniqueViolationError):
        await storage.create(UserFileFolder(user_id=fixtures["user_id"], org_id=fixtures["org_id"], name="DOCS"))


async def test_same_name_different_parents_allowed(storage, fixtures):
    p1 = await storage.create(UserFileFolder(user_id=fixtures["user_id"], org_id=fixtures["org_id"], name="P1"))
    p2 = await storage.create(UserFileFolder(user_id=fixtures["user_id"], org_id=fixtures["org_id"], name="P2"))
    a = await storage.create(UserFileFolder(user_id=fixtures["user_id"], org_id=fixtures["org_id"], parent_folder_id=p1.id, name="Same"))
    b = await storage.create(UserFileFolder(user_id=fixtures["user_id"], org_id=fixtures["org_id"], parent_folder_id=p2.id, name="Same"))
    assert a.id != b.id


async def test_inactive_name_does_not_block(storage, fixtures):
    f1 = await storage.create(UserFileFolder(user_id=fixtures["user_id"], org_id=fixtures["org_id"], name="X"))
    await storage.execute("UPDATE user_file_folders SET is_active = false WHERE id = $1", f1.id)
    f2 = await storage.create(UserFileFolder(user_id=fixtures["user_id"], org_id=fixtures["org_id"], name="X"))
    assert f2.id != f1.id


async def test_list_by_user(storage, fixtures):
    a = await storage.create(UserFileFolder(user_id=fixtures["user_id"], org_id=fixtures["org_id"], name="A"))
    b = await storage.create(UserFileFolder(user_id=fixtures["user_id"], org_id=fixtures["org_id"], name="B"))
    all_ = await storage.list_by_user(fixtures["user_id"])
    assert {f.id for f in all_} == {a.id, b.id}


async def test_update_folder(storage, fixtures):
    f = await storage.create(UserFileFolder(user_id=fixtures["user_id"], org_id=fixtures["org_id"], name="Old"))
    f.name = "New"
    updated = await storage.update(f)
    assert updated.name == "New"


async def test_list_children_root(storage, fixtures):
    a = await storage.create(UserFileFolder(user_id=fixtures["user_id"], org_id=fixtures["org_id"], name="A"))
    b = await storage.create(UserFileFolder(user_id=fixtures["user_id"], org_id=fixtures["org_id"], name="B"))
    await storage.create(UserFileFolder(user_id=fixtures["user_id"], org_id=fixtures["org_id"], parent_folder_id=a.id, name="Nested"))
    roots = await storage.list_children(fixtures["user_id"], None)
    assert {f.id for f in roots} == {a.id, b.id}


async def test_list_children_under_parent(storage, fixtures):
    p = await storage.create(UserFileFolder(user_id=fixtures["user_id"], org_id=fixtures["org_id"], name="P"))
    c1 = await storage.create(UserFileFolder(user_id=fixtures["user_id"], org_id=fixtures["org_id"], parent_folder_id=p.id, name="C1"))
    c2 = await storage.create(UserFileFolder(user_id=fixtures["user_id"], org_id=fixtures["org_id"], parent_folder_id=p.id, name="C2"))
    children = await storage.list_children(fixtures["user_id"], p.id)
    assert {f.id for f in children} == {c1.id, c2.id}
