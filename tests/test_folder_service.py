"""Unit tests for FolderService with mocked storage."""
import pytest
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import asyncpg

from src.engine.services.folder_service import (
    FolderService,
    FolderNotFound, FolderForbidden, FolderInvalidName,
    FolderMaxDepthExceeded, FolderCyclicMove,
    FolderInvalidParentOwner, FolderParentNotFound, FolderNameConflict,
)
from src.engine.models.user_file_folder import UserFileFolder
from src.engine.models.user import User


pytestmark = pytest.mark.asyncio


def _make_user(user_id=None, org_id=None, is_admin=False) -> User:
    return User(
        id=user_id or uuid4(),
        org_id=org_id or uuid4(),
        name="t", username="t", email="t@x.x",
        password_hash="", is_admin=is_admin,
    )


def _mocks():
    fs = MagicMock()
    fs.get_by_id = AsyncMock()
    fs.list_by_user = AsyncMock()
    fs.list_children = AsyncMock()
    fs.list_subtree_ids = AsyncMock()
    fs.deactivate_subtree = AsyncMock()
    fs.update = AsyncMock()
    fs.create = AsyncMock()
    fs.get_depth = AsyncMock()
    fs.get_subtree_max_depth = AsyncMock()

    files = MagicMock()
    files.list_by_user_in_folder = AsyncMock(return_value=[])
    files.list_by_folder_ids = AsyncMock(return_value=[])
    files.deactivate_by_folder_ids = AsyncMock(return_value=[])

    return fs, files


async def test_create_empty_name_raises():
    fs, files = _mocks()
    svc = FolderService(fs, files)
    with pytest.raises(FolderInvalidName) as ei:
        await svc.create(user_id=uuid4(), org_id=uuid4(), parent_folder_id=None, name="   ")
    assert ei.value.code == "EMPTY_NAME"


async def test_create_name_too_long_raises():
    fs, files = _mocks()
    svc = FolderService(fs, files)
    with pytest.raises(FolderInvalidName) as ei:
        await svc.create(user_id=uuid4(), org_id=uuid4(), parent_folder_id=None, name="x" * 300)
    assert ei.value.code == "NAME_TOO_LONG"


async def test_create_parent_not_found_raises():
    fs, files = _mocks()
    fs.get_by_id.return_value = None
    svc = FolderService(fs, files)
    with pytest.raises(FolderParentNotFound):
        await svc.create(user_id=uuid4(), org_id=uuid4(), parent_folder_id=uuid4(), name="X")


async def test_create_parent_wrong_owner_raises():
    fs, files = _mocks()
    fs.get_by_id.return_value = UserFileFolder(user_id=uuid4(), org_id=uuid4(), name="P")
    svc = FolderService(fs, files)
    with pytest.raises(FolderInvalidParentOwner):
        await svc.create(user_id=uuid4(), org_id=uuid4(), parent_folder_id=uuid4(), name="X")


async def test_create_depth_exceeded_raises():
    fs, files = _mocks()
    user_id, org_id = uuid4(), uuid4()
    fs.get_by_id.return_value = UserFileFolder(user_id=user_id, org_id=org_id, name="P")
    fs.get_depth.return_value = 9
    svc = FolderService(fs, files)
    with pytest.raises(FolderMaxDepthExceeded):
        await svc.create(user_id=user_id, org_id=org_id, parent_folder_id=uuid4(), name="X")


async def test_create_name_conflict_maps_to_folder_name_conflict():
    fs, files = _mocks()
    fs.create.side_effect = asyncpg.UniqueViolationError("dup")
    svc = FolderService(fs, files)
    with pytest.raises(FolderNameConflict):
        await svc.create(user_id=uuid4(), org_id=uuid4(), parent_folder_id=None, name="X")


async def test_create_root_ok_strips_name():
    fs, files = _mocks()
    user_id, org_id = uuid4(), uuid4()
    fs.create.return_value = UserFileFolder(user_id=user_id, org_id=org_id, name="X")
    svc = FolderService(fs, files)
    await svc.create(user_id=user_id, org_id=org_id, parent_folder_id=None, name="  X  ")
    args = fs.create.call_args[0][0]
    assert args.name == "X"


async def test_rename_not_found_raises():
    fs, files = _mocks()
    fs.get_by_id.return_value = None
    svc = FolderService(fs, files)
    with pytest.raises(FolderNotFound):
        await svc.rename(folder_id=uuid4(), new_name="Y", actor=_make_user())


async def test_rename_forbidden_raises():
    fs, files = _mocks()
    fs.get_by_id.return_value = UserFileFolder(user_id=uuid4(), org_id=uuid4(), name="X")
    svc = FolderService(fs, files)
    with pytest.raises(FolderForbidden):
        await svc.rename(folder_id=uuid4(), new_name="Y", actor=_make_user(user_id=uuid4()))


async def test_rename_admin_same_org_allowed():
    fs, files = _mocks()
    owner, org = uuid4(), uuid4()
    fs.get_by_id.return_value = UserFileFolder(user_id=owner, org_id=org, name="X")
    fs.update.return_value = UserFileFolder(user_id=owner, org_id=org, name="Y")
    svc = FolderService(fs, files)
    actor = _make_user(org_id=org, is_admin=True)
    out = await svc.rename(folder_id=uuid4(), new_name="Y", actor=actor)
    assert out.name == "Y"


async def test_move_into_self_raises():
    fs, files = _mocks()
    owner = uuid4()
    fid = uuid4()
    fs.get_by_id.return_value = UserFileFolder(user_id=owner, org_id=uuid4(), name="X")
    svc = FolderService(fs, files)
    with pytest.raises(FolderCyclicMove):
        await svc.move(folder_id=fid, new_parent_id=fid, actor=_make_user(user_id=owner))


async def test_move_into_descendant_raises():
    fs, files = _mocks()
    owner = uuid4()
    fid, desc_id = uuid4(), uuid4()
    org = uuid4()
    fs.get_by_id.side_effect = [
        UserFileFolder(user_id=owner, org_id=org, name="X"),
        UserFileFolder(user_id=owner, org_id=org, name="D"),
    ]
    fs.list_subtree_ids.return_value = {fid, desc_id}
    svc = FolderService(fs, files)
    with pytest.raises(FolderCyclicMove):
        await svc.move(folder_id=fid, new_parent_id=desc_id, actor=_make_user(user_id=owner))


async def test_move_depth_exceeded():
    fs, files = _mocks()
    owner = uuid4()
    org = uuid4()
    fs.get_by_id.side_effect = [
        UserFileFolder(user_id=owner, org_id=org, name="src"),
        UserFileFolder(user_id=owner, org_id=org, name="parent"),
    ]
    fs.list_subtree_ids.return_value = set()
    fs.get_depth.return_value = 5
    fs.get_subtree_max_depth.return_value = 4
    svc = FolderService(fs, files)
    with pytest.raises(FolderMaxDepthExceeded):
        await svc.move(folder_id=uuid4(), new_parent_id=uuid4(), actor=_make_user(user_id=owner))


async def test_move_to_root_ok():
    fs, files = _mocks()
    owner, org = uuid4(), uuid4()
    fs.get_by_id.return_value = UserFileFolder(user_id=owner, org_id=org, name="X")
    fs.update.return_value = UserFileFolder(user_id=owner, org_id=org, name="X", parent_folder_id=None)
    svc = FolderService(fs, files)
    out = await svc.move(folder_id=uuid4(), new_parent_id=None, actor=_make_user(user_id=owner))
    assert out.parent_folder_id is None


async def test_delete_not_found_raises():
    fs, files = _mocks()
    fs.get_by_id.return_value = None
    svc = FolderService(fs, files)
    with pytest.raises(FolderNotFound):
        await svc.delete(folder_id=uuid4(), actor=_make_user())


async def test_delete_cascade_no_files():
    fs, files = _mocks()
    owner = uuid4()
    fid = uuid4()
    fs.get_by_id.return_value = UserFileFolder(user_id=owner, org_id=uuid4(), name="X")
    fs.list_subtree_ids.return_value = {fid}
    fs.deactivate_subtree.return_value = [fid]
    svc = FolderService(fs, files)
    out = await svc.delete(folder_id=fid, actor=_make_user(user_id=owner))
    assert out == {"deleted_folders": 1, "deleted_files": 0}


async def test_delete_cascade_with_files_and_rag():
    from src.engine.models.user_file import UserFile
    fs, files = _mocks()
    rag = MagicMock()
    rag.delete_document = AsyncMock()
    adapter = MagicMock()
    adapter.delete = AsyncMock()
    owner = uuid4()
    fid = uuid4()
    file1 = UserFile(user_id=owner, org_id=uuid4(), folder_id=fid,
                     storage_key="k1", rag_status="indexed")
    file2 = UserFile(user_id=owner, org_id=uuid4(), folder_id=fid,
                     storage_key="k2", rag_status="not_indexed")
    fs.get_by_id.return_value = UserFileFolder(user_id=owner, org_id=uuid4(), name="X")
    fs.list_subtree_ids.return_value = {fid}
    fs.deactivate_subtree.return_value = [fid]
    files.list_by_folder_ids.return_value = [file1, file2]
    files.deactivate_by_folder_ids.return_value = [file1.id, file2.id]
    svc = FolderService(fs, files, rag_service=rag, storage_adapter=adapter)
    out = await svc.delete(folder_id=fid, actor=_make_user(user_id=owner))
    assert out == {"deleted_folders": 1, "deleted_files": 2}
    rag.delete_document.assert_awaited_once_with(file1.id)
    assert adapter.delete.await_count == 2


async def test_delete_rag_failure_does_not_rollback():
    from src.engine.models.user_file import UserFile
    fs, files = _mocks()
    rag = MagicMock()
    rag.delete_document = AsyncMock(side_effect=RuntimeError("rag down"))
    owner = uuid4()
    fid = uuid4()
    file1 = UserFile(user_id=owner, org_id=uuid4(), folder_id=fid, rag_status="indexed")
    fs.get_by_id.return_value = UserFileFolder(user_id=owner, org_id=uuid4(), name="X")
    fs.list_subtree_ids.return_value = {fid}
    fs.deactivate_subtree.return_value = [fid]
    files.list_by_folder_ids.return_value = [file1]
    files.deactivate_by_folder_ids.return_value = [file1.id]
    svc = FolderService(fs, files, rag_service=rag)
    out = await svc.delete(folder_id=fid, actor=_make_user(user_id=owner))
    assert out["deleted_folders"] == 1


async def test_verify_folder_owner_ok():
    fs, files = _mocks()
    user_id, org_id = uuid4(), uuid4()
    folder = UserFileFolder(user_id=user_id, org_id=org_id, name="X")
    fs.get_by_id.return_value = folder
    svc = FolderService(fs, files)
    out = await svc.verify_folder_owner(folder_id=uuid4(), user_id=user_id, org_id=org_id)
    assert out == folder


async def test_verify_folder_owner_wrong_user():
    fs, files = _mocks()
    fs.get_by_id.return_value = UserFileFolder(user_id=uuid4(), org_id=uuid4(), name="X")
    svc = FolderService(fs, files)
    with pytest.raises(FolderInvalidParentOwner):
        await svc.verify_folder_owner(folder_id=uuid4(), user_id=uuid4(), org_id=uuid4())


async def test_verify_folder_owner_not_found():
    fs, files = _mocks()
    fs.get_by_id.return_value = None
    svc = FolderService(fs, files)
    with pytest.raises(FolderNotFound):
        await svc.verify_folder_owner(folder_id=uuid4(), user_id=uuid4(), org_id=uuid4())


async def test_get_tree_builds_nested_structure():
    fs, files = _mocks()
    user_id, org_id = uuid4(), uuid4()
    a = UserFileFolder(user_id=user_id, org_id=org_id, name="A")
    b = UserFileFolder(user_id=user_id, org_id=org_id, name="B", parent_folder_id=a.id)
    c = UserFileFolder(user_id=user_id, org_id=org_id, name="C")
    fs.list_by_user.return_value = [a, b, c]
    svc = FolderService(fs, files)
    tree = await svc.get_tree(user_id=user_id, org_id=org_id)
    names = {n["name"] for n in tree}
    assert names == {"A", "C"}
    a_node = next(n for n in tree if n["name"] == "A")
    assert len(a_node["children"]) == 1
    assert a_node["children"][0]["name"] == "B"
