"""Test reply-to-mention endpoint and _is_mentioned helper."""
import pytest
from uuid import uuid4
from unittest.mock import AsyncMock, MagicMock

from fastapi import HTTPException

from src.engine.models.message import Mention, MentionType
from src.engine.routes.chats import _is_mentioned, ReplyToMentionRequest, reply_to_mention


def _msg_with_mentions(*mentions):
    m = MagicMock()
    m.mentions = list(mentions)
    return m


def _msg_dict(msg_id):
    """Минимальный dict совместимый с MessageResponse pydantic-моделью.
    Скопируй формат из соседнего теста test_chat_route_mention_notification.py — те же поля.
    """
    return {
        "id": str(msg_id),
        "chat_id": str(uuid4()),
        "sender_type": "user",
        "sender_id": str(uuid4()),
        "content": "ok",
        "mentions": [],
        "ai_is_valid": True,
        "ai_edited": False,
        "is_deleted": False,
        "created_at": "2026-05-03T00:00:00",
        "updated_at": "2026-05-03T00:00:00",
        "reply_to_id": None,
        "attachments": [],
        "references": [],
    }


def test_is_mentioned_user_match():
    sender = MagicMock(id=uuid4(), role_id=None)
    msg = _msg_with_mentions(
        Mention(type=MentionType.USER, user_id=sender.id, username="x", position=0)
    )
    assert _is_mentioned(msg, sender) is True


def test_is_mentioned_ai_role_match():
    sender = MagicMock(id=uuid4(), role_id=uuid4())
    msg = _msg_with_mentions(
        Mention(type=MentionType.AI_ROLE, user_id=sender.id, username="x", position=0)
    )
    assert _is_mentioned(msg, sender) is True


def test_is_mentioned_no_match():
    sender = MagicMock(id=uuid4(), role_id=None)
    other = uuid4()
    msg = _msg_with_mentions(
        Mention(type=MentionType.USER, user_id=other, username="y", position=0)
    )
    assert _is_mentioned(msg, sender) is False


def test_is_mentioned_empty_list():
    sender = MagicMock(id=uuid4(), role_id=None)
    msg = MagicMock()
    msg.mentions = None
    assert _is_mentioned(msg, sender) is False


@pytest.mark.asyncio
async def test_reply_endpoint_403_when_not_mentioned():
    sender_id = uuid4()
    other_id = uuid4()
    msg_id = uuid4()

    fake_engine = MagicMock()
    fake_engine.chat_service.get_message = AsyncMock(return_value=_msg_with_mentions(
        Mention(type=MentionType.USER, user_id=other_id, username="x", position=0)
    ))
    fake_engine.user_storage.get_by_id = AsyncMock(
        return_value=MagicMock(id=sender_id, role_id=None)
    )

    with pytest.raises(HTTPException) as exc:
        await reply_to_mention(
            message_id=msg_id,
            request=ReplyToMentionRequest(content="ok"),
            user_id=sender_id,
            engine=fake_engine,
        )
    assert exc.value.status_code == 403


@pytest.mark.asyncio
async def test_reply_endpoint_409_on_double_reply():
    sender_id = uuid4()
    msg_id = uuid4()
    fake_engine = MagicMock()
    original = _msg_with_mentions(
        Mention(type=MentionType.USER, user_id=sender_id, username="x", position=0)
    )
    original.chat_id = uuid4()
    fake_engine.chat_service.get_message = AsyncMock(return_value=original)
    fake_engine.user_storage.get_by_id = AsyncMock(
        return_value=MagicMock(id=sender_id, role_id=None)
    )
    fake_engine.message_storage.find_reply = AsyncMock(return_value=MagicMock(id=uuid4()))

    with pytest.raises(HTTPException) as exc:
        await reply_to_mention(
            message_id=msg_id,
            request=ReplyToMentionRequest(content="ok"),
            user_id=sender_id,
            engine=fake_engine,
        )
    assert exc.value.status_code == 409


@pytest.mark.asyncio
async def test_reply_endpoint_success_calls_send_message():
    sender_id = uuid4()
    msg_id = uuid4()
    chat_id = uuid4()
    fake_engine = MagicMock()
    original = _msg_with_mentions(
        Mention(type=MentionType.USER, user_id=sender_id, username="x", position=0)
    )
    original.chat_id = chat_id
    fake_engine.chat_service.get_message = AsyncMock(return_value=original)
    fake_engine.user_storage.get_by_id = AsyncMock(
        return_value=MagicMock(id=sender_id, role_id=None)
    )
    fake_engine.message_storage.find_reply = AsyncMock(return_value=None)
    new_msg_id = uuid4()
    new_msg = MagicMock(id=new_msg_id, to_dict=lambda: _msg_dict(new_msg_id))
    fake_engine.chat_service.send_message = AsyncMock(return_value=new_msg)
    fake_engine.kafka_producer = None  # bypass Kafka broadcast in unit test

    await reply_to_mention(
        message_id=msg_id,
        request=ReplyToMentionRequest(content="ответ"),
        user_id=sender_id,
        engine=fake_engine,
    )

    fake_engine.chat_service.send_message.assert_awaited_once()
    kwargs = fake_engine.chat_service.send_message.call_args.kwargs
    assert kwargs["chat_id"] == chat_id
    assert kwargs["sender_id"] == sender_id
    assert kwargs["content"] == "ответ"
    assert kwargs["reply_to_id"] == msg_id
