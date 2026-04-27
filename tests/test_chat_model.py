"""
Tests for Chat model — ChatType.SUPPORT and support_ticket_id field.
"""


def test_chat_support_type_and_ticket_id():
    """ChatType.SUPPORT serializes correctly + support_ticket_id round-trips."""
    from uuid import uuid4
    from src.engine.models.chat import Chat, ChatType

    ticket_id = uuid4()
    c = Chat(type=ChatType.SUPPORT, support_ticket_id=ticket_id)
    d = c.to_dict()
    assert d["type"] == "support"
    assert d["support_ticket_id"] == str(ticket_id)

    c2 = Chat.from_dict(d)
    assert c2.type == ChatType.SUPPORT
    assert c2.support_ticket_id == ticket_id


def test_chat_support_ticket_id_none_default():
    """support_ticket_id defaults to None for non-support chat types."""
    from src.engine.models.chat import Chat, ChatType

    c = Chat(type=ChatType.DIRECT)
    assert c.support_ticket_id is None
    d = c.to_dict()
    assert d["support_ticket_id"] is None

    c2 = Chat.from_dict(d)
    assert c2.support_ticket_id is None


def test_chat_legacy_dict_without_support_ticket_id():
    """Backward compatibility: a dict from before this field was added must still parse."""
    from uuid import uuid4
    from src.engine.models.chat import Chat, ChatType

    legacy_dict = {
        "id": str(uuid4()),
        "org_id": str(uuid4()),
        "type": "direct",
        "name": None,
        "participants": [str(uuid4())],
        "created_by": None,
        "task_id": None,
        "project_id": None,
        "is_active": True,
        "created_at": "2026-04-26T10:00:00",
        "updated_at": "2026-04-26T10:00:00",
        "last_message_at": None,
        # NO support_ticket_id key
    }
    c = Chat.from_dict(legacy_dict)
    assert c.support_ticket_id is None
