"""Verify FileService.upload no longer triggers RAG indexing.

Task 5 of the chat-attachments feature decouples upload from auto-RAG:
freshly uploaded files start in 'not_indexed' state and the owner must
explicitly call index_for_rag to enqueue them.
"""
import pytest
from unittest.mock import AsyncMock, patch
from uuid import uuid4

from src.engine.models.user_file import UserFile
from src.engine.services.file_service import FileService


def make_rag_file(org_id, owner_id, *, file_type: str = "pdf", rag_status: str = "not_indexed") -> UserFile:
    return UserFile(
        id=uuid4(),
        org_id=org_id,
        user_id=owner_id,
        uploaded_by_user_id=owner_id,
        storage_key=f"{org_id}/{owner_id}/doc.{file_type}",
        original_filename=f"doc.{file_type}",
        file_type=file_type,
        file_size=123,
        rag_status=rag_status,
        is_active=True,
    )


@pytest.mark.asyncio
async def test_upload_does_not_auto_index_rag():
    """upload() must construct a 'not_indexed' UserFile and never enqueue ingest."""
    file_storage = AsyncMock()
    file_storage.find_duplicate = AsyncMock(return_value=None)
    file_storage.create = AsyncMock(side_effect=lambda f: f)

    storage_adapter = AsyncMock()
    storage_adapter.save = AsyncMock(return_value=None)

    svc = FileService(file_storage=file_storage, storage_adapter=storage_adapter)

    org_id = uuid4()
    user_id = uuid4()

    # Patch where the ingest_queue would be invoked from inside FileService
    # (lazy import target inside index_for_rag) AND the routes module that
    # historically chained upload -> ingest. upload() must touch neither.
    with patch("src.engine.tasks.ingest_queue.ingest_queue.submit") as ingest_submit:
        result = await svc.upload(
            org_id=org_id,
            user_id=user_id,
            uploaded_by_user_id=user_id,
            filename="doc.pdf",
            data=b"%PDF-1.0 minimal payload",
        )

    assert ingest_submit.called is False, "upload() must NOT call ingest_queue.submit"
    assert result.rag_status == "not_indexed", (
        f"new uploads must start in 'not_indexed' state, got {result.rag_status!r}"
    )
    # Adapter still saves bytes; that's the upload's only side-effect.
    storage_adapter.save.assert_awaited_once()
    file_storage.create.assert_awaited_once()


@pytest.mark.asyncio
async def test_index_for_rag_owner_still_allowed_without_admin_context():
    """Backwards-compatible owner path: existing callers only pass requesting_user_id."""
    org_id = uuid4()
    owner_id = uuid4()
    file = make_rag_file(org_id, owner_id)

    file_storage = AsyncMock()
    file_storage.get_by_id = AsyncMock(return_value=file)
    file_storage.update_rag_status = AsyncMock(return_value=file)

    storage_adapter = AsyncMock()
    storage_adapter.read = AsyncMock(return_value=b"%PDF-1.0")

    svc = FileService(file_storage=file_storage, storage_adapter=storage_adapter)

    with patch("src.engine.tasks.ingest_queue.ingest_queue.submit", return_value="future") as submit:
        result, future = await svc.index_for_rag(file.id, requesting_user_id=owner_id)

    assert result is file
    assert future == "future"
    storage_adapter.read.assert_awaited_once_with(file.storage_key)
    submit.assert_called_once()


@pytest.mark.asyncio
async def test_index_for_rag_same_org_admin_can_index_other_users_file():
    """Org admins may enqueue RAG indexing for files owned by another user in their org."""
    org_id = uuid4()
    owner_id = uuid4()
    admin_id = uuid4()
    file = make_rag_file(org_id, owner_id)

    file_storage = AsyncMock()
    file_storage.get_by_id = AsyncMock(return_value=file)
    file_storage.update_rag_status = AsyncMock(return_value=file)

    storage_adapter = AsyncMock()
    storage_adapter.read = AsyncMock(return_value=b"%PDF-1.0")

    svc = FileService(file_storage=file_storage, storage_adapter=storage_adapter)

    with patch("src.engine.tasks.ingest_queue.ingest_queue.submit", return_value="future") as submit:
        result, future = await svc.index_for_rag(
            file.id,
            requesting_user_id=admin_id,
            requesting_org_id=org_id,
            requesting_is_admin=True,
        )

    assert result is file
    assert future == "future"
    submit.assert_called_once()


@pytest.mark.asyncio
async def test_index_for_rag_rejects_other_user_when_not_admin():
    """Non-admin users cannot index files owned by someone else."""
    org_id = uuid4()
    owner_id = uuid4()
    other_id = uuid4()
    file = make_rag_file(org_id, owner_id)

    file_storage = AsyncMock()
    file_storage.get_by_id = AsyncMock(return_value=file)

    storage_adapter = AsyncMock()
    svc = FileService(file_storage=file_storage, storage_adapter=storage_adapter)

    with pytest.raises(PermissionError, match="owner or an organization admin"):
        await svc.index_for_rag(
            file.id,
            requesting_user_id=other_id,
            requesting_org_id=org_id,
            requesting_is_admin=False,
        )

    storage_adapter.read.assert_not_called()


@pytest.mark.asyncio
async def test_index_for_rag_rejects_admin_from_another_org():
    """Admin override is scoped to the file's organization."""
    org_a = uuid4()
    org_b = uuid4()
    owner_id = uuid4()
    admin_id = uuid4()
    file = make_rag_file(org_a, owner_id)

    file_storage = AsyncMock()
    file_storage.get_by_id = AsyncMock(return_value=file)

    storage_adapter = AsyncMock()
    svc = FileService(file_storage=file_storage, storage_adapter=storage_adapter)

    with pytest.raises(PermissionError, match="owner or an organization admin"):
        await svc.index_for_rag(
            file.id,
            requesting_user_id=admin_id,
            requesting_org_id=org_b,
            requesting_is_admin=True,
        )

    storage_adapter.read.assert_not_called()
