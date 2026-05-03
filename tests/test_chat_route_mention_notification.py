"""Test that @user mentions trigger in-app notifications via send_message route."""
import pytest
from uuid import uuid4
from unittest.mock import AsyncMock, MagicMock

from src.engine.models.message import Mention, MentionType


def _msg_dict(msg_id, chat_id):
    """Minimal dict satisfying MessageResponse pydantic model."""
    return {
        "id": str(msg_id),
        "chat_id": str(chat_id),
        "sender_type": "user",
        "sender_id": str(uuid4()),
        "content": "x",
        "mentions": [],
        "references": [],
        "reply_to_id": None,
        "ai_is_valid": None,
        "ai_edited": False,
        "is_deleted": False,
        "created_at": "2026-01-01T00:00:00",
        "updated_at": "2026-01-01T00:00:00",
        "attachments": [],
    }


@pytest.mark.asyncio
async def test_user_mention_creates_notification():
    """When @anna is mentioned by Bob, notification with type='mention' is created for Anna."""
    from src.engine.routes import chats as chats_route

    bob_id = uuid4()
    anna_id = uuid4()
    org_id = uuid4()
    msg_id = uuid4()
    chat_id = uuid4()

    fake_engine = MagicMock()
    fake_engine.support_ticket_service = None
    fake_engine.chat_service.get_chat = AsyncMock(return_value=MagicMock(id=chat_id))
    fake_engine.mention_service.resolve_mentions = AsyncMock(
        return_value=[Mention(type=MentionType.USER, user_id=anna_id, username="anna", position=0)]
    )
    fake_engine.chat_service.send_message = AsyncMock(
        return_value=MagicMock(id=msg_id, to_dict=lambda: _msg_dict(msg_id, chat_id))
    )
    fake_engine.user_storage.get_by_id = AsyncMock(
        return_value=MagicMock(username="bob", id=bob_id)
    )
    fake_engine.in_app_notification_service.create = AsyncMock()
    fake_engine.ai_service.process_ai_mentions = AsyncMock(return_value=[])
    fake_engine.ai_service._is_async_mode = lambda: False
    fake_engine.ai_service.try_auto_respond = AsyncMock(return_value=None)
    fake_engine.chat_storage.is_ai_direct_chat = AsyncMock(return_value=False)

    request = MagicMock(content="@anna привет", file_ids=None, reply_to_id=None)
    await chats_route.send_message(
        chat_id=chat_id,
        request=request,
        user_id=bob_id,
        org_id=org_id,
        engine=fake_engine,
    )

    fake_engine.in_app_notification_service.create.assert_awaited_once()
    kwargs = fake_engine.in_app_notification_service.create.call_args.kwargs
    assert kwargs["user_id"] == anna_id
    assert kwargs["type"] == "mention"
    assert kwargs["reference_type"] == "message"
    assert kwargs["reference_id"] == msg_id


@pytest.mark.asyncio
async def test_self_mention_skipped():
    """Bob's @bob in his own message does not notify Bob."""
    from src.engine.routes import chats as chats_route

    bob_id = uuid4()
    org_id = uuid4()
    chat_id = uuid4()

    fake_engine = MagicMock()
    fake_engine.support_ticket_service = None
    fake_engine.chat_service.get_chat = AsyncMock(return_value=MagicMock(id=chat_id))
    fake_engine.mention_service.resolve_mentions = AsyncMock(
        return_value=[Mention(type=MentionType.USER, user_id=bob_id, username="bob", position=0)]
    )
    msg_id = uuid4()
    fake_engine.chat_service.send_message = AsyncMock(
        return_value=MagicMock(id=msg_id, to_dict=lambda: _msg_dict(msg_id, chat_id))
    )
    fake_engine.user_storage.get_by_id = AsyncMock(return_value=MagicMock(username="bob", id=bob_id))
    fake_engine.in_app_notification_service.create = AsyncMock()
    fake_engine.ai_service.process_ai_mentions = AsyncMock(return_value=[])
    fake_engine.ai_service._is_async_mode = lambda: False
    fake_engine.ai_service.try_auto_respond = AsyncMock(return_value=None)
    fake_engine.chat_storage.is_ai_direct_chat = AsyncMock(return_value=False)

    request = MagicMock(content="@bob себе", file_ids=None, reply_to_id=None)
    await chats_route.send_message(
        chat_id=chat_id, request=request, user_id=bob_id, org_id=org_id, engine=fake_engine,
    )
    fake_engine.in_app_notification_service.create.assert_not_awaited()


@pytest.mark.asyncio
async def test_ai_role_mention_does_not_create_notification():
    """@@anna goes through AI flow, not in-app notification."""
    from src.engine.routes import chats as chats_route

    bob_id = uuid4()
    anna_id = uuid4()
    org_id = uuid4()
    chat_id = uuid4()

    fake_engine = MagicMock()
    fake_engine.support_ticket_service = None
    fake_engine.chat_service.get_chat = AsyncMock(return_value=MagicMock(id=chat_id))
    fake_engine.mention_service.resolve_mentions = AsyncMock(
        return_value=[Mention(type=MentionType.AI_ROLE, user_id=anna_id, username="anna", position=0)]
    )
    msg_id = uuid4()
    fake_engine.chat_service.send_message = AsyncMock(
        return_value=MagicMock(id=msg_id, to_dict=lambda: _msg_dict(msg_id, chat_id))
    )
    fake_engine.user_storage.get_by_id = AsyncMock(return_value=MagicMock(username="bob", id=bob_id))
    fake_engine.in_app_notification_service.create = AsyncMock()
    fake_engine.ai_service.process_ai_mentions = AsyncMock(return_value=[])
    fake_engine.ai_service._is_async_mode = lambda: False
    fake_engine.ai_service.try_auto_respond = AsyncMock(return_value=None)
    fake_engine.chat_storage.is_ai_direct_chat = AsyncMock(return_value=False)

    request = MagicMock(content="@@anna договор", file_ids=None, reply_to_id=None)
    await chats_route.send_message(
        chat_id=chat_id, request=request, user_id=bob_id, org_id=org_id, engine=fake_engine,
    )
    fake_engine.in_app_notification_service.create.assert_not_awaited()
