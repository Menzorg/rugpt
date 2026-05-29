"""Tests for SupportTicket model — round-trip serialization."""
from datetime import datetime
from uuid import uuid4, UUID

from src.engine.models.support_ticket import (
    SupportTicket,
    SupportTicketCategory,
    SupportTicketStatus,
    ClosedByRole,
)


def test_round_trip_to_dict_from_dict_minimal():
    """Default-constructed ticket round-trips through dict cleanly."""
    t = SupportTicket(
        requester_user_id=uuid4(),
        requester_org_id=uuid4(),
        category=SupportTicketCategory.HOW_TO,
        status=SupportTicketStatus.OPEN,
        title="как создать чат",
    )
    d = t.to_dict()
    assert d["category"] == "how_to"
    assert d["status"] == "open"
    assert d["title"] == "как создать чат"
    assert d["assignee_user_id"] is None
    assert d["closed_by_role"] is None

    t2 = SupportTicket.from_dict(d)
    assert t2.id == t.id
    assert t2.requester_user_id == t.requester_user_id
    assert t2.category == SupportTicketCategory.HOW_TO
    assert t2.status == SupportTicketStatus.OPEN
    assert t2.title == "как создать чат"


def test_round_trip_with_all_optional_fields():
    """Ticket with all timestamps/fk fields filled round-trips correctly."""
    requester_id = uuid4()
    operator_id = uuid4()
    org_id = uuid4()
    t = SupportTicket(
        requester_user_id=requester_id,
        requester_org_id=org_id,
        category=SupportTicketCategory.BUG,
        status=SupportTicketStatus.CLOSED,
        assignee_user_id=operator_id,
        ai_handoff_at=datetime(2026, 4, 26, 10, 0, 0),
        ai_first_response_at=None,  # bug never had AI
        closed_at=datetime(2026, 4, 26, 12, 0, 0),
        closed_by_user_id=operator_id,
        closed_by_role=ClosedByRole.OPERATOR,
        title="ошибка 500",
    )
    d = t.to_dict()
    assert d["category"] == "bug"
    assert d["status"] == "closed"
    assert d["closed_by_role"] == "operator"
    assert d["assignee_user_id"] == str(operator_id)
    assert d["ai_handoff_at"] == "2026-04-26T10:00:00"
    assert d["ai_first_response_at"] is None
    assert d["closed_at"] == "2026-04-26T12:00:00"

    t2 = SupportTicket.from_dict(d)
    assert t2.assignee_user_id == operator_id
    assert t2.ai_handoff_at == datetime(2026, 4, 26, 10, 0, 0)
    assert t2.ai_first_response_at is None
    assert t2.closed_at == datetime(2026, 4, 26, 12, 0, 0)
    assert t2.closed_by_role == ClosedByRole.OPERATOR


def test_enum_values_are_strings():
    """Enums must be (str, Enum) for transparent JSON serialization."""
    assert SupportTicketCategory.HOW_TO.value == "how_to"
    assert SupportTicketCategory.BUG.value == "bug"
    assert SupportTicketCategory.OTHER.value == "other"
    assert SupportTicketStatus.OPEN.value == "open"
    assert SupportTicketStatus.IN_PROGRESS.value == "in_progress"
    assert SupportTicketStatus.CLOSED.value == "closed"
    assert ClosedByRole.REQUESTER.value == "requester"
    assert ClosedByRole.OPERATOR.value == "operator"


def test_from_dict_accepts_string_uuids_and_iso_timestamps():
    """from_dict must accept the wire format (strings) AND native types."""
    raw = {
        "id": str(uuid4()),
        "requester_user_id": str(uuid4()),
        "requester_org_id": str(uuid4()),
        "category": "how_to",
        "status": "in_progress",
        "assignee_user_id": str(uuid4()),
        "ai_handoff_at": "2026-04-26T10:00:00",
        "ai_first_response_at": "2026-04-26T10:01:00",
        "closed_at": None,
        "closed_by_user_id": None,
        "closed_by_role": None,
        "title": "тест",
        "created_at": "2026-04-26T09:55:00",
        "updated_at": "2026-04-26T10:01:00",
    }
    t = SupportTicket.from_dict(raw)
    assert isinstance(t.id, UUID)
    assert t.assignee_user_id is not None
    assert t.ai_handoff_at == datetime(2026, 4, 26, 10, 0, 0)
    assert t.closed_at is None


def test_from_dict_accepts_native_python_types():
    """from_dict must also accept native types (UUID, datetime, enum) directly,
    not only wire-format strings."""
    requester_id = uuid4()
    org_id = uuid4()
    raw = {
        "id": uuid4(),
        "requester_user_id": requester_id,
        "requester_org_id": org_id,
        "category": "how_to",
        "status": "open",
        "assignee_user_id": None,
        "ai_handoff_at": datetime(2026, 4, 26, 10, 0, 0),
        "ai_first_response_at": None,
        "closed_at": None,
        "closed_by_user_id": None,
        "closed_by_role": None,
        "title": "native types",
        "created_at": datetime(2026, 4, 26, 9, 0, 0),
        "updated_at": datetime(2026, 4, 26, 9, 0, 0),
    }
    t = SupportTicket.from_dict(raw)
    assert t.requester_user_id == requester_id
    assert t.requester_org_id == org_id
    assert t.ai_handoff_at == datetime(2026, 4, 26, 10, 0, 0)
    assert t.created_at == datetime(2026, 4, 26, 9, 0, 0)
    assert t.category == SupportTicketCategory.HOW_TO
