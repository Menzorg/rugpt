"""ChatService.user_can_access_attached_file — permission gate for /clone."""
import pytest
from unittest.mock import AsyncMock
from uuid import uuid4

from src.engine.services.chat_service import ChatService


@pytest.mark.asyncio
async def test_returns_false_when_storage_missing():
    """Backward compat: 2-arg ChatService construction (no attachment storage)."""
    svc = ChatService(chat_storage=AsyncMock(), message_storage=AsyncMock())
    result = await svc.user_can_access_attached_file(
        user_id=uuid4(), file_id=uuid4(), org_id=uuid4(),
    )
    assert result is False


@pytest.mark.asyncio
async def test_delegates_to_storage_method():
    """When storage is wired, delegates to is_file_visible_to_user."""
    attachment_storage = AsyncMock()
    attachment_storage.is_file_visible_to_user = AsyncMock(return_value=True)

    svc = ChatService(
        chat_storage=AsyncMock(),
        message_storage=AsyncMock(),
        user_file_storage=AsyncMock(),
        message_attachment_storage=attachment_storage,
    )
    user_id = uuid4()
    file_id = uuid4()
    org_id = uuid4()
    result = await svc.user_can_access_attached_file(
        user_id=user_id, file_id=file_id, org_id=org_id,
    )
    assert result is True
    attachment_storage.is_file_visible_to_user.assert_awaited_once_with(
        file_id=file_id, user_id=user_id, org_id=org_id,
    )


@pytest.mark.asyncio
async def test_returns_false_when_storage_returns_false():
    """Negative path."""
    attachment_storage = AsyncMock()
    attachment_storage.is_file_visible_to_user = AsyncMock(return_value=False)

    svc = ChatService(
        chat_storage=AsyncMock(),
        message_storage=AsyncMock(),
        user_file_storage=AsyncMock(),
        message_attachment_storage=attachment_storage,
    )
    result = await svc.user_can_access_attached_file(
        user_id=uuid4(), file_id=uuid4(), org_id=uuid4(),
    )
    assert result is False
