"""Integration tests for UserFileStorage folder-aware methods."""
import pytest
import pytest_asyncio
from uuid import uuid4

from src.engine.config import Config
from src.engine.storage.user_file_storage import UserFileStorage
from src.engine.storage.user_file_folder_storage import UserFileFolderStorage
from src.engine.storage.org_storage import OrgStorage
from src.engine.storage.user_storage import UserStorage
from src.engine.models.user_file import UserFile
from src.engine.models.user_file_folder import UserFileFolder
from src.engine.models.organization import Organization
from src.engine.models.user import User


pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture
async def env():
    org_s = OrgStorage(Config.get_postgres_dsn())
    user_s = UserStorage(Config.get_postgres_dsn())
    file_s = UserFileStorage(Config.get_postgres_dsn())
    folder_s = UserFileFolderStorage(Config.get_postgres_dsn())
    for s in (org_s, user_s, file_s, folder_s):
        await s.init()
    org = await org_s.create(Organization(name=f"o-{uuid4().hex[:6]}", slug=f"o-{uuid4().hex[:6]}"))
    user = await user_s.create(User(
        org_id=org.id, name="t", username=f"u-{uuid4().hex[:6]}",
        email=f"{uuid4().hex[:6]}@x.x", password_hash="", is_admin=False,
    ))
    yield {"org_id": org.id, "user_id": user.id, "file_s": file_s, "folder_s": folder_s}
    await user_s.execute("DELETE FROM user_files WHERE user_id=$1", user.id)
    await user_s.execute("DELETE FROM user_file_folders WHERE user_id=$1", user.id)
    for s in (org_s, user_s, file_s, folder_s):
        await s.close()


async def _make_file(env, folder_id=None, name="f.pdf"):
    return await env["file_s"].create(UserFile(
        user_id=env["user_id"], org_id=env["org_id"], uploaded_by_user_id=env["user_id"],
        storage_key=f"k/{uuid4()}", original_filename=name, file_type="pdf",
        file_size=10, content_hash=uuid4().hex, summary="", is_table=False,
        is_public=False, rag_status="not_indexed", folder_id=folder_id,
    ))


async def test_create_file_with_folder_id(env):
    folder = await env["folder_s"].create(UserFileFolder(
        user_id=env["user_id"], org_id=env["org_id"], name="A"))
    f = await _make_file(env, folder_id=folder.id, name="a.pdf")
    assert f.folder_id == folder.id


async def test_list_by_user_in_folder_root(env):
    root_f = await _make_file(env, folder_id=None, name="root.pdf")
    folder = await env["folder_s"].create(UserFileFolder(
        user_id=env["user_id"], org_id=env["org_id"], name="A"))
    await _make_file(env, folder_id=folder.id, name="nested.pdf")
    roots = await env["file_s"].list_by_user_in_folder(env["user_id"], None)
    assert {f.id for f in roots} == {root_f.id}


async def test_list_by_user_in_folder_specific(env):
    folder = await env["folder_s"].create(UserFileFolder(
        user_id=env["user_id"], org_id=env["org_id"], name="A"))
    f = await _make_file(env, folder_id=folder.id, name="x.pdf")
    in_folder = await env["file_s"].list_by_user_in_folder(env["user_id"], folder.id)
    assert {ff.id for ff in in_folder} == {f.id}


async def test_move_to_folder(env):
    folder = await env["folder_s"].create(UserFileFolder(
        user_id=env["user_id"], org_id=env["org_id"], name="A"))
    f = await _make_file(env)
    moved = await env["file_s"].move_to_folder(f.id, folder.id)
    assert moved.folder_id == folder.id


async def test_move_to_root(env):
    folder = await env["folder_s"].create(UserFileFolder(
        user_id=env["user_id"], org_id=env["org_id"], name="A"))
    f = await _make_file(env, folder_id=folder.id)
    moved = await env["file_s"].move_to_folder(f.id, None)
    assert moved.folder_id is None


async def test_deactivate_by_folder_ids(env):
    folder = await env["folder_s"].create(UserFileFolder(
        user_id=env["user_id"], org_id=env["org_id"], name="A"))
    f1 = await _make_file(env, folder_id=folder.id, name="a.pdf")
    f2 = await _make_file(env, folder_id=folder.id, name="b.pdf")
    affected = await env["file_s"].deactivate_by_folder_ids([folder.id])
    assert set(affected) == {f1.id, f2.id}
    fresh = await env["file_s"].get_by_id(f1.id)
    assert fresh is None
