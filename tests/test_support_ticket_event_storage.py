"""Tests for SupportTicketEventStorage — insert + list_by_ticket.

Hits real Postgres (rugpt DB).
"""
import asyncio
from uuid import uuid4

import pytest

from src.engine.config import Config
from src.engine.models.support_ticket import (
    SupportTicket, SupportTicketCategory,
)
from src.engine.models.support_ticket_event import (
    SupportTicketEvent, SupportTicketEventType, SupportTicketActorRole,
)
from src.engine.storage.support_ticket_storage import SupportTicketStorage
from src.engine.storage.support_ticket_event_storage import SupportTicketEventStorage


DSN = Config.get_postgres_dsn()


def _run(coro):
    return asyncio.run(coro)


async def _make_storages():
    ticket = SupportTicketStorage(DSN)
    await ticket.init()
    events = SupportTicketEventStorage(DSN)
    await events.init()
    return ticket, events


async def _grab_user_and_org(storage):
    row = await storage.fetchrow(
        "SELECT id, org_id FROM users WHERE is_system = false LIMIT 1"
    )
    if row is None:
        return None, None
    return row["id"], row["org_id"]


async def _seed_ticket(ticket_storage, user_id, org_id):
    t = SupportTicket(
        requester_user_id=user_id,
        requester_org_id=org_id,
        category=SupportTicketCategory.HOW_TO,
        title="event-storage test",
    )
    await ticket_storage.create(t)
    return t


async def _cleanup_ticket(ticket_storage, ticket_id):
    await ticket_storage.execute(
        "DELETE FROM support_ticket_events WHERE ticket_id = $1", ticket_id
    )
    await ticket_storage.execute(
        "DELETE FROM support_tickets WHERE id = $1", ticket_id
    )


def test_insert_and_list_single_event():
    async def go():
        ticket_storage, event_storage = await _make_storages()
        try:
            user_id, org_id = await _grab_user_and_org(ticket_storage)
            if user_id is None:
                pytest.skip("no non-system user")
            t = await _seed_ticket(ticket_storage, user_id, org_id)
            try:
                e = SupportTicketEvent(
                    ticket_id=t.id,
                    actor_user_id=user_id,
                    actor_role=SupportTicketActorRole.REQUESTER,
                    event_type=SupportTicketEventType.CREATED,
                    payload={"category": "how_to"},
                )
                inserted = await event_storage.insert(e)
                assert inserted.id == e.id
                assert inserted.event_type == SupportTicketEventType.CREATED

                events = await event_storage.list_by_ticket(t.id)
                assert len(events) == 1
                assert events[0].id == e.id
                assert events[0].payload == {"category": "how_to"}
            finally:
                await _cleanup_ticket(ticket_storage, t.id)
        finally:
            await ticket_storage.close()
            await event_storage.close()

    _run(go())


def test_list_by_ticket_returns_events_in_descending_order():
    """Events ordered by created_at DESC — most recent first."""
    async def go():
        ticket_storage, event_storage = await _make_storages()
        try:
            user_id, org_id = await _grab_user_and_org(ticket_storage)
            if user_id is None:
                pytest.skip("no non-system user")
            t = await _seed_ticket(ticket_storage, user_id, org_id)
            try:
                # Insert 3 events sequentially (each gets later created_at)
                first = SupportTicketEvent(
                    ticket_id=t.id, actor_user_id=user_id,
                    actor_role=SupportTicketActorRole.REQUESTER,
                    event_type=SupportTicketEventType.CREATED,
                )
                await event_storage.insert(first)
                # Tiny sleep to ensure created_at ordering is unambiguous
                await asyncio.sleep(0.01)

                second = SupportTicketEvent(
                    ticket_id=t.id, actor_user_id=user_id,
                    actor_role=SupportTicketActorRole.AI,
                    event_type=SupportTicketEventType.AI_RESPONDED,
                )
                await event_storage.insert(second)
                await asyncio.sleep(0.01)

                third = SupportTicketEvent(
                    ticket_id=t.id, actor_user_id=user_id,
                    actor_role=SupportTicketActorRole.REQUESTER,
                    event_type=SupportTicketEventType.AI_HANDOFF,
                )
                await event_storage.insert(third)

                events = await event_storage.list_by_ticket(t.id)
                assert len(events) == 3
                # DESC by created_at
                assert events[0].id == third.id
                assert events[1].id == second.id
                assert events[2].id == first.id
            finally:
                await _cleanup_ticket(ticket_storage, t.id)
        finally:
            await ticket_storage.close()
            await event_storage.close()

    _run(go())


def test_list_by_ticket_empty_for_unknown_ticket():
    async def go():
        ticket_storage, event_storage = await _make_storages()
        try:
            events = await event_storage.list_by_ticket(uuid4())
            assert events == []
        finally:
            await ticket_storage.close()
            await event_storage.close()

    _run(go())


def test_list_by_ticket_respects_limit():
    async def go():
        ticket_storage, event_storage = await _make_storages()
        try:
            user_id, org_id = await _grab_user_and_org(ticket_storage)
            if user_id is None:
                pytest.skip("no non-system user")
            t = await _seed_ticket(ticket_storage, user_id, org_id)
            try:
                for _ in range(5):
                    await event_storage.insert(SupportTicketEvent(
                        ticket_id=t.id, actor_user_id=user_id,
                        actor_role=SupportTicketActorRole.SYSTEM,
                        event_type=SupportTicketEventType.MESSAGE,
                    ))

                limited = await event_storage.list_by_ticket(t.id, limit=2)
                assert len(limited) == 2
            finally:
                await _cleanup_ticket(ticket_storage, t.id)
        finally:
            await ticket_storage.close()
            await event_storage.close()

    _run(go())


def test_cascade_delete_when_ticket_removed():
    """FK ON DELETE CASCADE — events disappear when ticket is deleted."""
    async def go():
        ticket_storage, event_storage = await _make_storages()
        try:
            user_id, org_id = await _grab_user_and_org(ticket_storage)
            if user_id is None:
                pytest.skip("no non-system user")
            t = await _seed_ticket(ticket_storage, user_id, org_id)
            try:
                await event_storage.insert(SupportTicketEvent(
                    ticket_id=t.id, actor_user_id=user_id,
                    actor_role=SupportTicketActorRole.SYSTEM,
                    event_type=SupportTicketEventType.MESSAGE,
                ))
                # Direct DELETE of the ticket — events should cascade
                await ticket_storage.execute(
                    "DELETE FROM support_tickets WHERE id = $1", t.id
                )
                events = await event_storage.list_by_ticket(t.id)
                assert events == []
            finally:
                # cleanup defensive (already deleted via cascade)
                await ticket_storage.execute(
                    "DELETE FROM support_ticket_events WHERE ticket_id = $1", t.id
                )
                await ticket_storage.execute(
                    "DELETE FROM support_tickets WHERE id = $1", t.id
                )
        finally:
            await ticket_storage.close()
            await event_storage.close()

    _run(go())
