"""Tests for SupportTicketEvent model — round-trip serialization."""
from datetime import datetime
from uuid import UUID, uuid4

from src.engine.models.support_ticket_event import (
    SupportTicketEvent,
    SupportTicketEventType,
    SupportTicketActorRole,
)


def test_round_trip_minimal():
    """Default-constructed event round-trips through dict cleanly."""
    e = SupportTicketEvent(
        ticket_id=uuid4(),
        actor_user_id=uuid4(),
        actor_role=SupportTicketActorRole.REQUESTER,
        event_type=SupportTicketEventType.CREATED,
        payload={"category": "how_to"},
    )
    d = e.to_dict()
    assert d["actor_role"] == "requester"
    assert d["event_type"] == "created"
    assert d["payload"] == {"category": "how_to"}

    e2 = SupportTicketEvent.from_dict(d)
    assert e2.ticket_id == e.ticket_id
    assert e2.actor_role == SupportTicketActorRole.REQUESTER
    assert e2.event_type == SupportTicketEventType.CREATED
    assert e2.payload == {"category": "how_to"}


def test_all_event_types_and_actor_roles():
    """Cover every enum value to lock down the contract with SQL CHECK."""
    expected_event_types = {
        "created", "ai_responded", "ai_handoff",
        "taken", "closed", "reopened", "message",
    }
    actual_event_types = {t.value for t in SupportTicketEventType}
    assert actual_event_types == expected_event_types

    expected_actor_roles = {"requester", "operator", "ai", "system"}
    actual_actor_roles = {r.value for r in SupportTicketActorRole}
    assert actual_actor_roles == expected_actor_roles


def test_default_payload_is_empty_dict():
    """payload defaults to {} so callers don't have to pass it explicitly."""
    e = SupportTicketEvent(
        ticket_id=uuid4(),
        actor_user_id=uuid4(),
        actor_role=SupportTicketActorRole.SYSTEM,
        event_type=SupportTicketEventType.MESSAGE,
    )
    assert e.payload == {}
    # Mutating one event's payload must NOT affect another (no shared mutable default)
    e.payload["a"] = 1
    e2 = SupportTicketEvent(
        ticket_id=uuid4(),
        actor_user_id=uuid4(),
        actor_role=SupportTicketActorRole.SYSTEM,
        event_type=SupportTicketEventType.MESSAGE,
    )
    assert e2.payload == {}


def test_from_dict_accepts_string_uuids_and_iso_timestamps():
    raw = {
        "id": str(uuid4()),
        "ticket_id": str(uuid4()),
        "actor_user_id": str(uuid4()),
        "actor_role": "operator",
        "event_type": "taken",
        "payload": {"note": "взял"},
        "created_at": "2026-04-26T10:00:00",
    }
    e = SupportTicketEvent.from_dict(raw)
    assert isinstance(e.id, UUID)
    assert e.actor_role == SupportTicketActorRole.OPERATOR
    assert e.event_type == SupportTicketEventType.TAKEN
    assert e.payload == {"note": "взял"}
    assert e.created_at == datetime(2026, 4, 26, 10, 0, 0)


def test_from_dict_accepts_native_python_types():
    raw = {
        "id": uuid4(),
        "ticket_id": uuid4(),
        "actor_user_id": uuid4(),
        "actor_role": "ai",
        "event_type": "ai_responded",
        "payload": {"message_id": "abc"},
        "created_at": datetime(2026, 4, 26, 10, 0, 0),
    }
    e = SupportTicketEvent.from_dict(raw)
    assert e.actor_role == SupportTicketActorRole.AI
    assert e.event_type == SupportTicketEventType.AI_RESPONDED
    assert e.created_at == datetime(2026, 4, 26, 10, 0, 0)
