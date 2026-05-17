from uuid import uuid4

from src.engine.agents.metadata import (
    append_extra_body_key,
    append_metadata_key,
    build_initial_extra_body,
    resolve_litellm_session_id,
)
from src.engine.logging_context import bind_correlation_id, correlation_id_var


def test_build_litellm_extra_body_adds_session_and_metadata():
    chat_id = uuid4()

    extra_body = build_initial_extra_body(
        litellm_session_id="session-1",
        agent_name="reasoner",
        chat_id=chat_id,
    )

    assert extra_body == {
        "litellm_session_id": "session-1",
        "metadata": {
            "agent_name": "reasoner",
            "chatid": str(chat_id),
        },
    }


def test_append_extra_body_key_preserves_metadata_when_adding_thinking_flags():
    base = {
        "litellm_session_id": "session-1",
        "metadata": {
            "agent_name": "reasoner",
            "chatid": "chat-1",
        },
    }

    assert append_extra_body_key(
        base,
        "chat_template_kwargs",
        {"enable_thinking": True},
    ) == {
        "litellm_session_id": "session-1",
        "metadata": {
            "agent_name": "reasoner",
            "chatid": "chat-1",
        },
        "chat_template_kwargs": {"enable_thinking": True},
    }


def test_append_metadata_key_creates_metadata_when_missing():
    assert append_metadata_key(
        {"litellm_session_id": "session-1"},
        "supervisor_name",
        "supervisor",
    ) == {
        "litellm_session_id": "session-1",
        "metadata": {
            "supervisor_name": "supervisor",
        },
    }


def test_resolve_litellm_session_id_uses_correlation_id():
    token = bind_correlation_id("corr-123")
    try:
        assert resolve_litellm_session_id() == "corr-123"
    finally:
        correlation_id_var.reset(token)


def test_resolve_litellm_session_id_falls_back_when_correlation_id_missing():
    token = correlation_id_var.set("-")
    try:
        session_id = resolve_litellm_session_id()
    finally:
        correlation_id_var.reset(token)

    assert session_id
    assert session_id != "-"
