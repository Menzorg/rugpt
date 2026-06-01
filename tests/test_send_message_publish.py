"""Test that send_message route publishes the user message to Kafka chat.events.

Slice A: chat message SEND moved off WS onto signed HTTP. The engine is now the
single source of truth for broadcast — after saving the user message it publishes
it to chat.events, where the NestJS consumer fans it out to the chat room.
"""
import pytest
from uuid import uuid4
from unittest.mock import AsyncMock, MagicMock

from src.engine.config import Config


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


def _fake_engine(chat_id, msg_id):
    """Engine mock wired for the no-mention, no-AI happy path."""
    fake_engine = MagicMock()
    fake_engine.support_ticket_service = None
    fake_engine.chat_service.get_chat = AsyncMock(return_value=MagicMock(id=chat_id, participants=[]))
    fake_engine.mention_service.resolve_mentions = AsyncMock(return_value=[])
    fake_engine.chat_service.send_message = AsyncMock(
        return_value=MagicMock(id=msg_id, to_dict=lambda: _msg_dict(msg_id, chat_id))
    )
    fake_engine.user_storage.get_by_id = AsyncMock(return_value=MagicMock(username="bob", id=uuid4()))
    fake_engine.in_app_notification_service.create = AsyncMock()
    fake_engine.ai_service.process_ai_mentions = AsyncMock(return_value=[])
    fake_engine.ai_service._is_async_mode = lambda: False
    fake_engine.ai_service.try_auto_respond = AsyncMock(return_value=None)
    fake_engine.chat_storage.is_ai_direct_chat = AsyncMock(return_value=False)
    return fake_engine


@pytest.mark.asyncio
async def test_send_message_publishes_to_chat_events():
    """A plain user message is published once to chat.events with chat_id + message."""
    from src.engine.routes import chats as chats_route

    bob_id = uuid4()
    org_id = uuid4()
    chat_id = uuid4()
    msg_id = uuid4()

    fake_engine = _fake_engine(chat_id, msg_id)
    fake_engine.kafka_producer = MagicMock()
    fake_engine.kafka_producer.send = AsyncMock()

    request = MagicMock(content="привет", file_ids=None, reply_to_id=None)
    await chats_route.send_message(
        chat_id=chat_id,
        request=request,
        current_user={"user_id": bob_id, "org_id": org_id},
        engine=fake_engine,
    )

    fake_engine.kafka_producer.send.assert_awaited_once()
    args, kwargs = fake_engine.kafka_producer.send.call_args
    # topic is the first positional arg
    assert args[0] == Config.KAFKA_TOPIC_CHAT_EVENTS
    payload = args[1]
    assert payload["chat_id"] == str(chat_id)
    assert payload["message"]["id"] == str(msg_id)
    assert kwargs["key"] == str(chat_id)


@pytest.mark.asyncio
async def test_send_message_no_publish_when_kafka_disabled():
    """When kafka_producer is None the send still succeeds (no publish, no crash)."""
    from src.engine.routes import chats as chats_route

    bob_id = uuid4()
    org_id = uuid4()
    chat_id = uuid4()
    msg_id = uuid4()

    fake_engine = _fake_engine(chat_id, msg_id)
    fake_engine.kafka_producer = None

    request = MagicMock(content="привет", file_ids=None, reply_to_id=None)
    resp = await chats_route.send_message(
        chat_id=chat_id,
        request=request,
        current_user={"user_id": bob_id, "org_id": org_id},
        engine=fake_engine,
    )
    assert resp.user_message.id == str(msg_id)
