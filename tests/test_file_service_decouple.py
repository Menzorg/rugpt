"""Verify FileService.upload no longer triggers RAG indexing.

Task 5 of the chat-attachments feature decouples upload from auto-RAG:
freshly uploaded files start in 'not_indexed' state and the owner must
explicitly call index_for_rag to enqueue them.
"""
import pytest
from unittest.mock import AsyncMock, patch
from uuid import uuid4

from src.engine.services.file_service import FileService


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
