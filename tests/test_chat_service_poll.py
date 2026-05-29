"""ChatService.create_poll_chat — idempotent creation of poll-scoped chat."""
import pytest
from unittest.mock import AsyncMock
from uuid import uuid4

from src.engine.models.chat import Chat, ChatType
from src.engine.services.chat_service import ChatService


@pytest.mark.asyncio
async def test_create_poll_chat_creates_when_missing():
    chat_storage = AsyncMock()
    chat_storage.get_by_poll_id = AsyncMock(return_value=None)
    chat_storage.create = AsyncMock(side_effect=lambda c: c)
    message_storage = AsyncMock()

    service = ChatService(chat_storage, message_storage)

    poll_id = uuid4()
    assignee_id = uuid4()
    interviewer_id = uuid4()
    org_id = uuid4()

    chat = await service.create_poll_chat(
        poll_id=poll_id,
        assignee_user_id=assignee_id,
        interviewer_user_id=interviewer_id,
        org_id=org_id,
    )

    assert chat.type == ChatType.POLL
    assert chat.poll_id == poll_id
    assert assignee_id in chat.participants
    assert interviewer_id in chat.participants
    chat_storage.create.assert_awaited_once()


@pytest.mark.asyncio
async def test_create_poll_chat_returns_existing():
    existing = Chat(
        id=uuid4(),
        type=ChatType.POLL,
        poll_id=uuid4(),
        participants=[uuid4(), uuid4()],
    )
    chat_storage = AsyncMock()
    chat_storage.get_by_poll_id = AsyncMock(return_value=existing)
    chat_storage.create = AsyncMock()
    message_storage = AsyncMock()

    service = ChatService(chat_storage, message_storage)

    chat = await service.create_poll_chat(
        poll_id=existing.poll_id,
        assignee_user_id=uuid4(),
        interviewer_user_id=uuid4(),
        org_id=uuid4(),
    )

    assert chat.id == existing.id
    chat_storage.create.assert_not_awaited()
