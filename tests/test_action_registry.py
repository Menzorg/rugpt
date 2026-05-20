"""Unit tests for the action registry."""
import pytest
from pydantic import BaseModel
from src.engine.actions.registry import (
    ActionRegistry,
    ActionDefinition,
    ActionError,
    UnknownActionError,
    PermissionDeniedError,
)


class DummyParams(BaseModel):
    target_id: str


async def dummy_handler(engine, user, params: DummyParams) -> dict:
    return {"ok": True, "target_id": params.target_id}


def always_true(user, params):
    return True


def admin_only(user, params):
    return bool(getattr(user, "is_admin", False))


@pytest.fixture
def registry():
    r = ActionRegistry()
    r.register(ActionDefinition(
        action_type="dummy_open",
        handler=dummy_handler,
        params_schema=DummyParams,
        permission=always_true,
    ))
    r.register(ActionDefinition(
        action_type="dummy_admin",
        handler=dummy_handler,
        params_schema=DummyParams,
        permission=admin_only,
    ))
    return r


def test_register_and_get(registry):
    d = registry.get("dummy_open")
    assert d is not None
    assert d.action_type == "dummy_open"


def test_get_unknown_returns_none(registry):
    assert registry.get("nope") is None


@pytest.mark.asyncio
async def test_dispatch_unknown_raises(registry):
    with pytest.raises(UnknownActionError):
        await registry.dispatch(engine=None, action_type="nope", params={}, user=None)


@pytest.mark.asyncio
async def test_dispatch_permission_denied_raises(registry):
    class U:
        is_admin = False
    with pytest.raises(PermissionDeniedError):
        await registry.dispatch(engine=None, action_type="dummy_admin",
                                 params={"target_id": "x"}, user=U())


@pytest.mark.asyncio
async def test_dispatch_invalid_params_raises(registry):
    class U:
        is_admin = True
    with pytest.raises(ActionError):
        await registry.dispatch(engine=None, action_type="dummy_admin",
                                 params={}, user=U())  # missing target_id


@pytest.mark.asyncio
async def test_dispatch_happy_path(registry):
    class U:
        is_admin = True
    result = await registry.dispatch(engine=None, action_type="dummy_admin",
                                      params={"target_id": "abc"}, user=U())
    assert result == {"ok": True, "target_id": "abc"}


def test_register_duplicate_raises(registry):
    with pytest.raises(ValueError):
        registry.register(ActionDefinition(
            action_type="dummy_open",
            handler=dummy_handler,
            params_schema=DummyParams,
            permission=always_true,
        ))
