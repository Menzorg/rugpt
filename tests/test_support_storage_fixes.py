from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from src.engine.storage.support_ticket_storage import SupportTicketStorage


@pytest.mark.asyncio
async def test_list_queue_requires_ai_handoff():
    storage = SupportTicketStorage("postgresql://test")
    storage.fetch = AsyncMock(return_value=[])
    await storage.list_queue(limit=50)
    sql = storage.fetch.call_args.args[0]
    assert "ai_handoff_at IS NOT NULL" in sql
    assert "status = 'open'" in sql
    assert "assignee_user_id IS NULL" in sql


@pytest.mark.asyncio
async def test_reopen_routes_status_by_assignee():
    storage = SupportTicketStorage("postgresql://test")
    storage.fetchrow = AsyncMock(return_value=None)
    await storage.reopen(uuid4())
    sql = storage.fetchrow.call_args.args[0]
    assert "CASE WHEN assignee_user_id IS NULL THEN 'open' ELSE 'in_progress' END" in sql
    assert "WHERE id = $1 AND status = 'closed'" in sql
    # Reopen must also clear stale close metadata (load-bearing per docstring).
    assert "closed_at = NULL" in sql
    assert "closed_by_user_id = NULL" in sql
    assert "closed_by_role = NULL" in sql
