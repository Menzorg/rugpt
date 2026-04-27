"""Tests for ChatService.can_user_access_chat — strict orgship enforcement
with cross-org exemption only for ChatType.SUPPORT.

Critical security regression suite: any future code change that broadens
the exemption beyond SUPPORT chats must fail these tests.
"""
import asyncio
from uuid import uuid4

import pytest

from src.engine.config import Config
from src.engine.models.chat import Chat, ChatType
from src.engine.models.user import User
from src.engine.services.chat_service import ChatService


RUGPT_SUPPORT_ORG_ID = Config.RUGPT_SUPPORT_ORG_ID


def _user(id_=None, org_id=None) -> User:
    return User(id=id_ or uuid4(), org_id=org_id or uuid4(), name="Test", username="test", email="t@t")


def _chat(type_: ChatType, org_id, participants, support_ticket_id=None) -> Chat:
    return Chat(
        org_id=org_id,
        type=type_,
        participants=participants,
        support_ticket_id=support_ticket_id,
    )


def _make_service():
    """ChatService for access checks does not need DB.

    CONTRACT: can_user_access_chat MUST NOT touch storage. If it ever does,
    this fixture (which uses __new__ to skip storage init) becomes wrong
    and tests will fail with AttributeError on a None storage attribute —
    that's the signal to either update the fixture or push the storage
    access elsewhere.
    """
    return ChatService.__new__(ChatService)


# ---------- SUPPORT chat access ----------

def test_support_chat_accessible_to_participant_from_requester_org():
    svc = _make_service()
    org_a = uuid4()
    user = _user(org_id=org_a)
    chat = _chat(ChatType.SUPPORT, org_id=org_a, participants=[user.id], support_ticket_id=uuid4())
    assert _maybe_run(svc.can_user_access_chat(user, chat)) is True


def test_support_chat_accessible_to_operator_in_queue_even_without_participation():
    """Operator from RuGPT Support sees unassigned ticket in queue → can read chat
    BEFORE taking it. Exemption requires support_ticket_id IS NOT NULL.
    """
    svc = _make_service()
    requester_org = uuid4()
    operator = _user(org_id=RUGPT_SUPPORT_ORG_ID)
    chat = _chat(
        ChatType.SUPPORT,
        org_id=requester_org,
        participants=[uuid4()],  # operator NOT yet in participants (queue state)
        support_ticket_id=uuid4(),
    )
    assert _maybe_run(svc.can_user_access_chat(operator, chat)) is True


def test_support_chat_denied_to_random_foreign_user():
    """A user from a third org who is neither participant nor RuGPT Support
    operator must be denied.
    """
    svc = _make_service()
    requester_org = uuid4()
    foreign_org = uuid4()
    intruder = _user(org_id=foreign_org)
    chat = _chat(
        ChatType.SUPPORT,
        org_id=requester_org,
        participants=[uuid4()],
        support_ticket_id=uuid4(),
    )
    assert _maybe_run(svc.can_user_access_chat(intruder, chat)) is False


def test_support_chat_denied_to_operator_when_support_ticket_id_is_None():
    """Defensive: even an RuGPT Support operator must NOT bypass orgship
    via the queue exemption if chat.support_ticket_id is missing.
    Pathological state but the exemption clause checks both fields.
    """
    svc = _make_service()
    requester_org = uuid4()
    operator = _user(org_id=RUGPT_SUPPORT_ORG_ID)
    chat = _chat(
        ChatType.SUPPORT,
        org_id=requester_org,
        participants=[uuid4()],
        support_ticket_id=None,  # missing — pathological
    )
    assert _maybe_run(svc.can_user_access_chat(operator, chat)) is False


# ---------- Regression: non-SUPPORT chats MUST enforce orgship ----------

@pytest.mark.parametrize("chat_type", [ChatType.DIRECT, ChatType.TASK, ChatType.PROJECT])
def test_non_support_chat_denied_cross_org_even_if_in_participants(chat_type):
    """SECURITY REGRESSION: a user must NOT access a chat from a different org
    even if they appear in chat.participants — the support exemption must
    NOT broaden to DIRECT/TASK/PROJECT.
    """
    svc = _make_service()
    org_a = uuid4()
    org_b = uuid4()
    user = _user(org_id=org_a)
    chat = _chat(chat_type, org_id=org_b, participants=[user.id])
    result = _maybe_run(svc.can_user_access_chat(user, chat))
    assert result is False, (
        f"Cross-org leak: {chat_type} chat exposed to user from different org"
    )


@pytest.mark.parametrize("chat_type", [ChatType.DIRECT, ChatType.TASK, ChatType.PROJECT])
def test_non_support_chat_denied_to_rugpt_support_operator(chat_type):
    """SECURITY REGRESSION: RuGPT Support operators must NOT use the support
    exemption to access non-support chats.
    """
    svc = _make_service()
    customer_org = uuid4()
    operator = _user(org_id=RUGPT_SUPPORT_ORG_ID)
    chat = _chat(chat_type, org_id=customer_org, participants=[operator.id])
    result = _maybe_run(svc.can_user_access_chat(operator, chat))
    assert result is False, (
        f"Operator privilege escalation: {chat_type} chat from customer org "
        f"exposed to RuGPT Support operator"
    )


@pytest.mark.parametrize("chat_type", [ChatType.DIRECT, ChatType.TASK, ChatType.PROJECT, ChatType.SUPPORT])
def test_chat_with_empty_participants_denied(chat_type):
    """Edge case: a chat with no participants must not be accessible
    to anyone (except potentially via SUPPORT operator queue exemption,
    which requires a non-None support_ticket_id; that case is covered
    separately).
    """
    svc = _make_service()
    org_a = uuid4()
    user = _user(org_id=org_a)
    chat = _chat(chat_type, org_id=org_a, participants=[])
    # No participants, not in operator org — must deny regardless of type
    assert _maybe_run(svc.can_user_access_chat(user, chat)) is False


def test_support_chat_accessible_to_operator_who_is_already_participant_even_if_ticket_id_None():
    """Pathological edge: a SUPPORT chat that has lost its support_ticket_id
    (legacy / migration corruption) is still accessible to anyone who is
    explicitly in the participants list. The participant check trumps the
    queue exemption — this is the correct behavior because participant
    membership is the strongest claim.
    """
    svc = _make_service()
    requester_org = uuid4()
    operator = _user(org_id=RUGPT_SUPPORT_ORG_ID)
    chat = _chat(
        ChatType.SUPPORT,
        org_id=requester_org,
        participants=[operator.id],
        support_ticket_id=None,
    )
    assert _maybe_run(svc.can_user_access_chat(operator, chat)) is True


def test_direct_chat_accessible_within_same_org_and_participant():
    """Sanity: same-org DIRECT chat is accessible to participants (no regression
    on the happy path).
    """
    svc = _make_service()
    org_a = uuid4()
    user = _user(org_id=org_a)
    chat = _chat(ChatType.DIRECT, org_id=org_a, participants=[user.id])
    assert _maybe_run(svc.can_user_access_chat(user, chat)) is True


def test_direct_chat_denied_to_same_org_non_participant():
    """Sanity: same-org but not a participant → denied.
    """
    svc = _make_service()
    org_a = uuid4()
    user = _user(org_id=org_a)
    chat = _chat(ChatType.DIRECT, org_id=org_a, participants=[uuid4()])
    assert _maybe_run(svc.can_user_access_chat(user, chat)) is False


# ---------- helpers ----------

def _maybe_run(value):
    """Helper: if can_user_access_chat is async, run it; else return value as-is."""
    if asyncio.iscoroutine(value):
        return asyncio.run(value)
    return value
