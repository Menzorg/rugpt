"""FileService.clone — idempotency, cross-org guard, missing source.

Task 5 of the chat-attachments feature: «Add to my files» creates a
metadata-only clone of a file the requesting user encountered in a chat.
"""
import pytest
from unittest.mock import AsyncMock
from uuid import uuid4

from src.engine.services.file_service import FileService
from src.engine.models.user_file import UserFile


def make_source(org_id, owner_id, *, status: str = "indexed") -> UserFile:
    """Construct a realistic source UserFile for clone tests."""
    return UserFile(
        id=uuid4(),
        org_id=org_id,
        user_id=owner_id,
        uploaded_by_user_id=owner_id,
        storage_key=f"{org_id}/{owner_id}/source.pdf",
        original_filename="source.pdf",
        file_type="pdf",
        file_size=100,
        content_hash="deadbeef" * 8,
        summary="some summary",
        is_table=False,
        is_public=True,
        rag_status=status,
        is_active=True,
    )


@pytest.mark.asyncio
async def test_clone_returns_existing_when_present():
    """Idempotency: second click on «Add to my files» returns the same clone."""
    org_id = uuid4()
    owner = uuid4()
    other = uuid4()
    source = make_source(org_id, owner)
    existing_clone = UserFile(
        id=uuid4(),
        org_id=org_id,
        user_id=other,
        uploaded_by_user_id=other,
        storage_key=source.storage_key,
        original_filename=source.original_filename,
        file_type=source.file_type,
        file_size=source.file_size,
        content_hash=source.content_hash,
        cloned_from_file_id=source.id,
        is_active=True,
    )

    file_storage = AsyncMock()
    file_storage.get_by_id = AsyncMock(return_value=source)
    file_storage.find_active_clone = AsyncMock(return_value=existing_clone)
    file_storage.create = AsyncMock()

    svc = FileService(file_storage=file_storage, storage_adapter=AsyncMock())
    result = await svc.clone(source.id, requesting_user_id=other, org_id=org_id)

    assert result is existing_clone
    file_storage.create.assert_not_called()
    file_storage.find_active_clone.assert_awaited_once_with(other, source.id)


@pytest.mark.asyncio
async def test_clone_creates_new_when_no_existing():
    """First click: create a new clone that shares storage_key with the source."""
    org_id = uuid4()
    owner = uuid4()
    other = uuid4()
    source = make_source(org_id, owner)

    file_storage = AsyncMock()
    file_storage.get_by_id = AsyncMock(return_value=source)
    file_storage.find_active_clone = AsyncMock(return_value=None)
    file_storage.create = AsyncMock(side_effect=lambda f: f)

    svc = FileService(file_storage=file_storage, storage_adapter=AsyncMock())
    result = await svc.clone(source.id, requesting_user_id=other, org_id=org_id)

    assert result.user_id == other
    assert result.uploaded_by_user_id == other
    assert result.cloned_from_file_id == source.id
    assert result.storage_key == source.storage_key  # bytes shared, no copy
    assert result.rag_status == "not_indexed"        # owner opts in separately
    assert result.is_public is False                 # clones don't re-share by default
    assert result.summary == ""                      # owner-specific, reset
    assert result.file_type == source.file_type
    assert result.content_hash == source.content_hash
    file_storage.create.assert_awaited_once()


@pytest.mark.asyncio
async def test_clone_rejects_cross_org():
    """A user in org B cannot clone a file from org A even with the file_id."""
    org_a = uuid4()
    org_b = uuid4()
    owner = uuid4()
    other = uuid4()
    source = make_source(org_a, owner)

    file_storage = AsyncMock()
    file_storage.get_by_id = AsyncMock(return_value=source)

    svc = FileService(file_storage=file_storage, storage_adapter=AsyncMock())

    with pytest.raises(ValueError, match="Cross-org"):
        await svc.clone(source.id, requesting_user_id=other, org_id=org_b)

    # Must short-circuit before the idempotency lookup or create.
    file_storage.find_active_clone.assert_not_called()


@pytest.mark.asyncio
async def test_clone_raises_when_source_missing():
    """Missing or inactive source -> FileNotFoundError."""
    file_storage = AsyncMock()
    file_storage.get_by_id = AsyncMock(return_value=None)

    svc = FileService(file_storage=file_storage, storage_adapter=AsyncMock())
    with pytest.raises(FileNotFoundError):
        await svc.clone(uuid4(), requesting_user_id=uuid4(), org_id=uuid4())


@pytest.mark.asyncio
async def test_clone_raises_when_source_inactive():
    """Inactive source (soft-deleted) is treated as not-found."""
    org_id = uuid4()
    source = make_source(org_id, uuid4())
    source.is_active = False

    file_storage = AsyncMock()
    file_storage.get_by_id = AsyncMock(return_value=source)

    svc = FileService(file_storage=file_storage, storage_adapter=AsyncMock())
    with pytest.raises(FileNotFoundError):
        await svc.clone(source.id, requesting_user_id=uuid4(), org_id=org_id)
