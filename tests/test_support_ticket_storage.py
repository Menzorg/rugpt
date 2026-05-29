"""Tests for SupportTicketStorage — CRUD + atomic CAS take.

Hits real Postgres (rugpt DB) for SQL atomicity verification.
"""
import asyncio
import pytest
from uuid import uuid4

from src.engine.config import Config
from src.engine.models.support_ticket import (
    SupportTicket, SupportTicketCategory, SupportTicketStatus, ClosedByRole,
)
from src.engine.storage.support_ticket_storage import SupportTicketStorage


DSN = Config.get_postgres_dsn()


def _run(coro):
    return asyncio.run(coro)


async def _make_storage() -> SupportTicketStorage:
    storage = SupportTicketStorage(DSN)
    await storage.init()
    return storage


async def _grab_user_and_org(storage: SupportTicketStorage):
    """Grab any existing user (with org) for FK satisfaction."""
    row = await storage.fetchrow(
        "SELECT id, org_id FROM users WHERE is_system = false LIMIT 1"
    )
    if row is None:
        return None, None
    return row["id"], row["org_id"]


async def _grab_operator(storage: SupportTicketStorage):
    """Grab any RuGPT Support operator for take() tests; create one if missing.

    Returns (operator_id, created_by_test_bool). If created_by_test is True,
    the caller must call _cleanup_operator(storage, operator_id) at the end
    of the test to avoid accumulating test_operator_* rows.
    """
    rugpt_support_org_id = "00000001-0000-0000-0000-000000000000"
    row = await storage.fetchrow(
        "SELECT id FROM users WHERE org_id = $1 AND is_active = true LIMIT 1",
        rugpt_support_org_id,
    )
    if row:
        return row["id"], False
    new_op_id = await storage.fetchval(
        """
        INSERT INTO users (org_id, username, name, email, is_active)
        VALUES ($1, $2, $3, $4, true)
        RETURNING id
        """,
        rugpt_support_org_id,
        f"test_operator_{uuid4().hex[:8]}",
        "Test Operator",
        f"test-op-{uuid4().hex[:8]}@rugpt.support",
    )
    return new_op_id, True


async def _cleanup_ticket(storage: SupportTicketStorage, ticket_id):
    await storage.execute(
        "DELETE FROM support_ticket_events WHERE ticket_id = $1", ticket_id
    )
    await storage.execute("DELETE FROM support_tickets WHERE id = $1", ticket_id)


async def _cleanup_operator(storage: SupportTicketStorage, operator_id):
    await storage.execute("DELETE FROM users WHERE id = $1", operator_id)


def test_create_and_get_by_id():
    async def go():
        storage = await _make_storage()
        try:
            user_id, org_id = await _grab_user_and_org(storage)
            if user_id is None:
                pytest.skip("no non-system user in dev DB")

            t = SupportTicket(
                requester_user_id=user_id,
                requester_org_id=org_id,
                category=SupportTicketCategory.HOW_TO,
                title="как создать чат",
            )
            created = await storage.create(t)
            assert created.id == t.id
            assert created.status == SupportTicketStatus.OPEN

            fetched = await storage.get_by_id(t.id)
            assert fetched is not None
            assert fetched.requester_user_id == user_id
            assert fetched.title == "как создать чат"

            await _cleanup_ticket(storage, t.id)
        finally:
            await storage.close()

    _run(go())


def test_get_by_id_returns_none_for_missing():
    async def go():
        storage = await _make_storage()
        try:
            result = await storage.get_by_id(uuid4())
            assert result is None
        finally:
            await storage.close()

    _run(go())


def test_list_queue_returns_only_open_unassigned():
    async def go():
        storage = await _make_storage()
        try:
            user_id, org_id = await _grab_user_and_org(storage)
            if user_id is None:
                pytest.skip("no non-system user in dev DB")

            # Create one open unassigned + one in_progress assigned
            t_queue = SupportTicket(
                requester_user_id=user_id, requester_org_id=org_id,
                category=SupportTicketCategory.BUG, title="queue ticket",
            )
            await storage.create(t_queue)

            operator_id, op_created = await _grab_operator(storage)
            t_assigned = SupportTicket(
                requester_user_id=user_id, requester_org_id=org_id,
                category=SupportTicketCategory.BUG, title="assigned ticket",
                status=SupportTicketStatus.IN_PROGRESS,
                assignee_user_id=operator_id,
            )
            await storage.create(t_assigned)

            queue = await storage.list_queue(limit=100)
            ids = {t.id for t in queue}
            assert t_queue.id in ids
            assert t_assigned.id not in ids

            await _cleanup_ticket(storage, t_queue.id)
            await _cleanup_ticket(storage, t_assigned.id)
            if op_created:
                await _cleanup_operator(storage, operator_id)
        finally:
            await storage.close()

    _run(go())


def test_take_atomic_cas_exactly_one_winner():
    async def go():
        storage = await _make_storage()
        try:
            user_id, org_id = await _grab_user_and_org(storage)
            if user_id is None:
                pytest.skip("no non-system user in dev DB")

            t = SupportTicket(
                requester_user_id=user_id, requester_org_id=org_id,
                category=SupportTicketCategory.BUG, title="cas race",
            )
            await storage.create(t)

            operator_id, op_created = await _grab_operator(storage)

            # Three concurrent take() calls — exactly one must win
            results = await asyncio.gather(
                storage.take(t.id, operator_id),
                storage.take(t.id, operator_id),
                storage.take(t.id, operator_id),
                return_exceptions=False,
            )
            successes = [r for r in results if r is not None]
            assert len(successes) == 1, f"expected exactly 1 winner, got {len(successes)}"

            # Verify state
            after = await storage.get_by_id(t.id)
            assert after.status == SupportTicketStatus.IN_PROGRESS
            assert after.assignee_user_id == operator_id

            await _cleanup_ticket(storage, t.id)
            if op_created:
                await _cleanup_operator(storage, operator_id)
        finally:
            await storage.close()

    _run(go())


def test_take_returns_none_when_already_taken():
    async def go():
        storage = await _make_storage()
        try:
            user_id, org_id = await _grab_user_and_org(storage)
            if user_id is None:
                pytest.skip("no non-system user in dev DB")
            operator_id, op_created = await _grab_operator(storage)

            t = SupportTicket(
                requester_user_id=user_id, requester_org_id=org_id,
                category=SupportTicketCategory.BUG,
                status=SupportTicketStatus.IN_PROGRESS,
                assignee_user_id=operator_id,
            )
            await storage.create(t)

            # Already taken — second take returns None
            result = await storage.take(t.id, operator_id)
            assert result is None

            await _cleanup_ticket(storage, t.id)
            if op_created:
                await _cleanup_operator(storage, operator_id)
        finally:
            await storage.close()

    _run(go())


def test_close_sets_status_and_role():
    async def go():
        storage = await _make_storage()
        try:
            user_id, org_id = await _grab_user_and_org(storage)
            if user_id is None:
                pytest.skip("no non-system user in dev DB")

            t = SupportTicket(
                requester_user_id=user_id, requester_org_id=org_id,
                category=SupportTicketCategory.HOW_TO,
            )
            await storage.create(t)

            closed = await storage.close_ticket(t.id, user_id, ClosedByRole.REQUESTER)
            assert closed is not None
            assert closed.status == SupportTicketStatus.CLOSED
            assert closed.closed_at is not None
            assert closed.closed_by_role == ClosedByRole.REQUESTER

            # Closing already-closed returns None
            second = await storage.close_ticket(t.id, user_id, ClosedByRole.REQUESTER)
            assert second is None

            await _cleanup_ticket(storage, t.id)
        finally:
            await storage.close()

    _run(go())


def test_reopen_in_closed_state():
    async def go():
        storage = await _make_storage()
        try:
            user_id, org_id = await _grab_user_and_org(storage)
            if user_id is None:
                pytest.skip("no non-system user in dev DB")

            t = SupportTicket(
                requester_user_id=user_id, requester_org_id=org_id,
                category=SupportTicketCategory.HOW_TO,
            )
            await storage.create(t)
            await storage.close_ticket(t.id, user_id, ClosedByRole.REQUESTER)

            reopened = await storage.reopen(t.id)
            assert reopened is not None
            assert reopened.status == SupportTicketStatus.IN_PROGRESS
            assert reopened.closed_at is None
            assert reopened.closed_by_role is None

            await _cleanup_ticket(storage, t.id)
        finally:
            await storage.close()

    _run(go())
