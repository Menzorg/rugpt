"""Test that group-B review actions publish to Kafka chat.events.

Group B WS→HTTP (Zero Trust): message:validate / message:reject / message:read
moved off WS onto signed HTTP. After each mutation the engine publishes a
discriminated (`kind`) event to chat.events; the NestJS consumer rebroadcasts the
same WS events as before (message:validated / message:rejected / chat:unread-cleared).
"""
import pytest
from uuid import uuid4
from unittest.mock import AsyncMock, MagicMock

from src.engine.config import Config


def _msg_dict(msg_id, chat_id, sender_id):
    return {
        "id": str(msg_id),
        "chat_id": str(chat_id),
        "sender_type": "ai_role",
        "sender_id": str(sender_id),
        "content": "ok",
        "mentions": [],
        "references": [],
        "reply_to_id": None,
        "ai_is_valid": True,
        "ai_edited": False,
        "is_deleted": False,
        "created_at": "2026-01-01T00:00:00",
        "updated_at": "2026-01-01T00:00:00",
        "attachments": [],
    }


@pytest.mark.asyncio
async def test_validate_message_publishes_message_validated():
    from src.engine.routes import chats as chats_route

    user_id = uuid4()
    chat_id = uuid4()
    msg_id = uuid4()

    target = MagicMock(sender_id=user_id, chat_id=chat_id)
    validated = MagicMock(chat_id=chat_id, to_dict=lambda: _msg_dict(msg_id, chat_id, user_id))

    engine = MagicMock()
    engine.chat_service.get_message = AsyncMock(return_value=target)
    engine.user_storage.get_by_id = AsyncMock(return_value=MagicMock(is_admin=True))
    engine.chat_service.validate_ai_message = AsyncMock(return_value=validated)
    engine.kafka_producer = MagicMock()
    engine.kafka_producer.send = AsyncMock()

    await chats_route.validate_message(
        message_id=msg_id,
        request=MagicMock(edited_content=None),
        current_user={"user_id": user_id},
        engine=engine,
    )

    engine.kafka_producer.send.assert_awaited_once()
    args, kwargs = engine.kafka_producer.send.call_args
    assert args[0] == Config.KAFKA_TOPIC_CHAT_EVENTS
    payload = args[1]
    assert payload["kind"] == "message_validated"
    assert payload["chat_id"] == str(chat_id)
    assert payload["message"]["id"] == str(msg_id)
    assert kwargs["key"] == str(chat_id)


@pytest.mark.asyncio
async def test_reject_message_publishes_message_rejected():
    from src.engine.routes import chats as chats_route

    user_id = uuid4()
    chat_id = uuid4()
    msg_id = uuid4()

    target = MagicMock(sender_id=user_id, chat_id=chat_id)
    rule = MagicMock(to_dict=lambda: {
        "id": str(uuid4()), "role_id": str(uuid4()), "mem_id": None,
        "src_user_message_id": None, "src_ai_response_id": None,
        "user_correction_text": "fix", "extracted_lesson": None, "is_active": True,
    })

    engine = MagicMock()
    engine.chat_service.get_message = AsyncMock(return_value=target)
    engine.user_storage.get_by_id = AsyncMock(return_value=MagicMock(is_admin=True))
    engine.correction_rule_service.reject_and_create_rule = AsyncMock(return_value=rule)
    engine.kafka_producer = MagicMock()
    engine.kafka_producer.send = AsyncMock()

    await chats_route.reject_message(
        message_id=msg_id,
        request=MagicMock(correction_text="fix"),
        current_user={"user_id": user_id},
        engine=engine,
    )

    engine.kafka_producer.send.assert_awaited_once()
    args, kwargs = engine.kafka_producer.send.call_args
    assert args[0] == Config.KAFKA_TOPIC_CHAT_EVENTS
    payload = args[1]
    assert payload["kind"] == "message_rejected"
    assert payload["chat_id"] == str(chat_id)
    assert payload["message_id"] == str(msg_id)
    assert payload["rule"]["user_correction_text"] == "fix"
    assert kwargs["key"] == str(chat_id)


@pytest.mark.asyncio
async def test_mark_read_publishes_unread_cleared():
    from src.engine.routes import chats as chats_route

    user_id = uuid4()
    chat_id = uuid4()
    msg_id = uuid4()

    engine = MagicMock()
    engine.chat_service.mark_chat_read = AsyncMock()
    engine.kafka_producer = MagicMock()
    engine.kafka_producer.send = AsyncMock()

    await chats_route.mark_chat_read(
        chat_id=chat_id,
        body=MagicMock(message_id=msg_id),
        current_user={"user_id": user_id},
        engine=engine,
    )

    engine.kafka_producer.send.assert_awaited_once()
    args, kwargs = engine.kafka_producer.send.call_args
    assert args[0] == Config.KAFKA_TOPIC_CHAT_EVENTS
    payload = args[1]
    assert payload["kind"] == "unread_cleared"
    assert payload["user_id"] == str(user_id)
    assert payload["chat_id"] == str(chat_id)
    assert kwargs["key"] == str(chat_id)


@pytest.mark.asyncio
async def test_no_publish_when_kafka_disabled():
    from src.engine.routes import chats as chats_route

    user_id = uuid4()
    chat_id = uuid4()
    msg_id = uuid4()

    engine = MagicMock()
    engine.chat_service.mark_chat_read = AsyncMock()
    engine.kafka_producer = None

    # Should not raise.
    await chats_route.mark_chat_read(
        chat_id=chat_id,
        body=MagicMock(message_id=msg_id),
        current_user={"user_id": user_id},
        engine=engine,
    )
