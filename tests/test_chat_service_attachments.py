"""ChatService.send_message — file_ids validation (Task 6 of chat-attachments).

Covers:
- max-5 attachments per message
- ownership check (file.user_id != sender → PermissionError)
- active check (file.is_active is False → ValueError)
- happy path: validate, persist, attach, hydrate
"""
import pytest
from unittest.mock import AsyncMock
from uuid import uuid4

from src.engine.services.chat_service import ChatService
from src.engine.models.user_file import UserFile


def make_chat_service(
    *,
    user_file_storage=None,
    message_attachment_storage=None,
):
    """Build ChatService with minimal async mocks.

    message_storage.create returns the message it was given so
    `created = await self.message_storage.create(message)` yields a real Message
    (the validation block needs `created.id` to exist).
    """
    chat_storage = AsyncMock()
    message_storage = AsyncMock()
    message_storage.create = AsyncMock(side_effect=lambda m: m)
    return ChatService(
        chat_storage=chat_storage,
        message_storage=message_storage,
        user_file_storage=user_file_storage or AsyncMock(),
        message_attachment_storage=message_attachment_storage or AsyncMock(),
    )


@pytest.mark.asyncio
async def test_send_rejects_more_than_5_attachments():
    """6 file_ids → ValueError before any storage call."""
    svc = make_chat_service()
    with pytest.raises(ValueError, match="Maximum 5"):
        await svc.send_message(
            chat_id=uuid4(),
            sender_id=uuid4(),
            content="hi",
            file_ids=[uuid4() for _ in range(6)],
        )
    # Validation must happen before persist.
    svc.message_storage.create.assert_not_called()


@pytest.mark.asyncio
async def test_send_rejects_foreign_file():
    """File owned by another user → PermissionError."""
    sender = uuid4()
    other = uuid4()
    fid = uuid4()
    foreign = UserFile(id=fid, user_id=other, is_active=True)

    user_file_storage = AsyncMock()
    user_file_storage.get_by_id = AsyncMock(return_value=foreign)

    svc = make_chat_service(user_file_storage=user_file_storage)

    with pytest.raises(PermissionError, match="does not belong to sender"):
        await svc.send_message(
            chat_id=uuid4(),
            sender_id=sender,
            content="hi",
            file_ids=[fid],
        )
    svc.message_storage.create.assert_not_called()


@pytest.mark.asyncio
async def test_send_rejects_inactive_file():
    """Inactive file → ValueError."""
    sender = uuid4()
    fid = uuid4()
    inactive = UserFile(id=fid, user_id=sender, is_active=False)

    user_file_storage = AsyncMock()
    user_file_storage.get_by_id = AsyncMock(return_value=inactive)

    svc = make_chat_service(user_file_storage=user_file_storage)

    with pytest.raises(ValueError, match="not found or inactive"):
        await svc.send_message(
            chat_id=uuid4(),
            sender_id=sender,
            content="hi",
            file_ids=[fid],
        )
    svc.message_storage.create.assert_not_called()


@pytest.mark.asyncio
async def test_send_rejects_missing_file():
    """Storage returns None → ValueError (not found)."""
    sender = uuid4()
    fid = uuid4()

    user_file_storage = AsyncMock()
    user_file_storage.get_by_id = AsyncMock(return_value=None)

    svc = make_chat_service(user_file_storage=user_file_storage)

    with pytest.raises(ValueError, match="not found or inactive"):
        await svc.send_message(
            chat_id=uuid4(),
            sender_id=sender,
            content="hi",
            file_ids=[fid],
        )
    svc.message_storage.create.assert_not_called()


@pytest.mark.asyncio
async def test_send_attaches_and_hydrates_on_happy_path():
    """Valid file_ids → message persisted, attach() called, attachments hydrated."""
    sender = uuid4()
    fid = uuid4()
    valid = UserFile(id=fid, user_id=sender, is_active=True)

    user_file_storage = AsyncMock()
    user_file_storage.get_by_id = AsyncMock(return_value=valid)

    hydrated = ["ATTACHMENT_SENTINEL"]
    message_attachment_storage = AsyncMock()
    message_attachment_storage.attach = AsyncMock(return_value=None)
    message_attachment_storage.get_for_message = AsyncMock(return_value=hydrated)

    svc = make_chat_service(
        user_file_storage=user_file_storage,
        message_attachment_storage=message_attachment_storage,
    )

    result = await svc.send_message(
        chat_id=uuid4(),
        sender_id=sender,
        content="hi",
        file_ids=[fid],
    )

    # Persist happened
    svc.message_storage.create.assert_awaited_once()
    # attach happened with the persisted message id
    message_attachment_storage.attach.assert_awaited_once_with(result.id, [fid])
    # Returned message is hydrated for the caller (no extra round trip)
    assert result.attachments is hydrated


@pytest.mark.asyncio
async def test_send_without_file_ids_skips_attachment_path():
    """No file_ids → no calls to attachment storage; backwards-compatible behaviour."""
    sender = uuid4()
    user_file_storage = AsyncMock()
    user_file_storage.get_by_id = AsyncMock()
    message_attachment_storage = AsyncMock()

    svc = make_chat_service(
        user_file_storage=user_file_storage,
        message_attachment_storage=message_attachment_storage,
    )
    await svc.send_message(
        chat_id=uuid4(), sender_id=sender, content="hi",
    )

    user_file_storage.get_by_id.assert_not_called()
    message_attachment_storage.attach.assert_not_called()


@pytest.mark.asyncio
async def test_send_with_file_ids_but_missing_deps_raises_runtime_error():
    """Two-arg ChatService constructor still works for legacy callers, but if
    those callers pass file_ids the error must be loud, not a silent swallow."""
    chat_storage = AsyncMock()
    message_storage = AsyncMock()
    message_storage.create = AsyncMock(side_effect=lambda m: m)

    svc = ChatService(chat_storage=chat_storage, message_storage=message_storage)

    with pytest.raises(RuntimeError, match="missing"):
        await svc.send_message(
            chat_id=uuid4(),
            sender_id=uuid4(),
            content="hi",
            file_ids=[uuid4()],
        )
