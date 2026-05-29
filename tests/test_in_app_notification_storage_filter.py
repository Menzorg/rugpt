"""Test InAppNotificationStorage.list_by_user with optional type filter."""
import pytest
from uuid import uuid4
from unittest.mock import AsyncMock

from src.engine.storage.in_app_notification_storage import InAppNotificationStorage


@pytest.mark.asyncio
async def test_list_by_user_passes_type_to_sql():
    storage = InAppNotificationStorage("postgresql://test")
    storage.fetch = AsyncMock(return_value=[])
    user_id = uuid4()
    await storage.list_by_user(user_id, type="mention", limit=10, offset=0, unread_only=False)
    args = storage.fetch.call_args
    assert "mention" in args.args, f"type 'mention' not passed to fetch: {args}"


@pytest.mark.asyncio
async def test_list_by_user_without_type_skips_filter():
    storage = InAppNotificationStorage("postgresql://test")
    storage.fetch = AsyncMock(return_value=[])
    user_id = uuid4()
    await storage.list_by_user(user_id, type=None, limit=10, offset=0, unread_only=False)
    args = storage.fetch.call_args
    assert "mention" not in args.args


@pytest.mark.asyncio
async def test_list_by_user_combined_filters_no_param_collision():
    """Когда type и unread_only оба заданы — нумерация $N не должна коллидировать."""
    storage = InAppNotificationStorage("postgresql://test")
    storage.fetch = AsyncMock(return_value=[])
    user_id = uuid4()
    await storage.list_by_user(user_id, type="mention", limit=10, offset=0, unread_only=True)
    args = storage.fetch.call_args
    query = args.args[0]
    assert "is_read = FALSE" in query
    assert "type = $2" in query, f"type должен быть $2 (после user_id $1, без сдвига от is_read): {query}"
    assert args.args == (query, user_id, "mention", 10, 0)
