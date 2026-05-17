"""Helpers for LiteLLM request metadata carried in ChatOpenAI extra_body."""
from __future__ import annotations

from typing import Any, Mapping, Optional
from uuid import UUID, uuid4

from ..logging_context import get_correlation_id


def append_extra_body_key(
    body: Optional[Mapping[str, Any]],
    key: str,
    value: Any,
) -> dict[str, Any]:
    """Return a copy of extra_body with one top-level key added or replaced."""
    updated = dict(body or {})
    updated[key] = value
    return updated


def append_metadata_key(
    body: Optional[Mapping[str, Any]],
    key: str,
    value: Any,
) -> dict[str, Any]:
    """Return a copy of extra_body with one metadata key added or replaced."""
    updated = dict(body or {})
    metadata = dict(updated.get("metadata") or {})
    metadata[key] = value
    updated["metadata"] = metadata
    return updated


def resolve_litellm_session_id() -> str:
    correlation_id = get_correlation_id()
    if correlation_id and correlation_id != "-":
        return correlation_id
    return str(uuid4())


def build_initial_extra_body(
    *,
    litellm_session_id: str,
    agent_name: str,
    chat_id: Optional[UUID],
    supervisor_name: Optional[str] = None,
) -> dict[str, Any]:
    """Build initial LiteLLM tracking fields inside ChatOpenAI extra_body."""
    metadata = {
        "agent_name": agent_name,
        "chatid": str(chat_id) if chat_id is not None else "",
    }
    if supervisor_name:
        metadata["supervisor_name"] = supervisor_name

    return {
        "litellm_session_id": litellm_session_id,
        "metadata": metadata,
    }
