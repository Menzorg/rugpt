"""Test MessageStorage.find_reply — used by reply-to-mention single-use guard."""
import pytest
from uuid import uuid4
from unittest.mock import AsyncMock

from src.engine.storage.message_storage import MessageStorage


@pytest.mark.asyncio
async def test_find_reply_returns_existing_reply():
    storage = MessageStorage("postgresql://test")
    storage.fetchrow = AsyncMock(return_value={
        "id": uuid4(), "chat_id": uuid4(), "sender_id": uuid4(),
        "sender_type": "user", "content": "ok", "mentions": "[]",
        "reply_to_id": uuid4(), "ai_is_valid": True, "ai_edited": False,
        "is_deleted": False, "created_at": None, "updated_at": None,
    })
    reply_to_id = uuid4()
    sender_id = uuid4()
    result = await storage.find_reply(reply_to_id, sender_id)
    assert result is not None
    args = storage.fetchrow.await_args
    assert args.args[1] == reply_to_id, f"reply_to_id should be $1: {args.args}"
    assert args.args[2] == sender_id, f"sender_id should be $2: {args.args}"


@pytest.mark.asyncio
async def test_find_reply_returns_none_when_no_match():
    storage = MessageStorage("postgresql://test")
    storage.fetchrow = AsyncMock(return_value=None)
    result = await storage.find_reply(uuid4(), uuid4())
    assert result is None
