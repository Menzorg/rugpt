"""Unit tests for the show_modal tool: input validation + per-role whitelist."""
import pytest
from unittest.mock import MagicMock
from langchain_core.runnables import RunnableConfig

from src.engine.agents.tools.show_modal import create_show_modal_tool


@pytest.fixture
def registry_stub():
    """Stub action registry that knows about 'invoice_approve' only."""
    from pydantic import BaseModel
    class Params(BaseModel):
        invoice_id: str

    reg = MagicMock()
    def get(action_type):
        if action_type == "invoice_approve":
            d = MagicMock()
            d.params_schema = Params
            return d
        return None
    reg.get.side_effect = get
    return reg


@pytest.fixture
def role_stub(allowed):
    r = MagicMock()
    r.agent_config = {"allowed_action_types": list(allowed)}
    return r


def make_config(role) -> RunnableConfig:
    return {"configurable": {"role": role, "caller_user_id": "u-1", "org_id": "o-1"}}


@pytest.mark.asyncio
@pytest.mark.parametrize("allowed", [["invoice_approve"]])
async def test_show_modal_emits_payload_for_allowed_action(registry_stub, role_stub):
    tool = create_show_modal_tool(registry_stub)
    payload = await tool.ainvoke(
        {
            "title": "Утвердить счёт",
            "body": "Поставщик X, сумма 1000",
            "actions": [
                {"label": "Утвердить", "action_type": "invoice_approve",
                 "params": {"invoice_id": "abc-uuid"}}
            ],
        },
        config=make_config(role_stub),
    )
    assert "shown" in payload.lower() or "modal" in payload.lower()


@pytest.mark.asyncio
@pytest.mark.parametrize("allowed", [["invoice_approve"]])
async def test_show_modal_rejects_action_type_not_in_role_whitelist(registry_stub, role_stub):
    tool = create_show_modal_tool(registry_stub)
    result = await tool.ainvoke(
        {
            "title": "X",
            "body": "Y",
            "actions": [
                {"label": "L", "action_type": "delete_all_users",
                 "params": {}}
            ],
        },
        config=make_config(role_stub),
    )
    assert "not allowed" in result.lower()


@pytest.mark.asyncio
@pytest.mark.parametrize("allowed", [["invoice_approve"]])
async def test_show_modal_rejects_unknown_action_type(registry_stub, role_stub):
    role_stub.agent_config = {"allowed_action_types": ["never_registered"]}
    tool = create_show_modal_tool(registry_stub)
    result = await tool.ainvoke(
        {
            "title": "X",
            "body": "Y",
            "actions": [
                {"label": "L", "action_type": "never_registered", "params": {}}
            ],
        },
        config=make_config(role_stub),
    )
    assert "unknown" in result.lower() or "not registered" in result.lower()


@pytest.mark.asyncio
@pytest.mark.parametrize("allowed", [["invoice_approve"]])
async def test_show_modal_rejects_bad_params_schema(registry_stub, role_stub):
    tool = create_show_modal_tool(registry_stub)
    result = await tool.ainvoke(
        {
            "title": "X",
            "body": "Y",
            "actions": [
                {"label": "L", "action_type": "invoice_approve",
                 "params": {}}   # missing invoice_id
            ],
        },
        config=make_config(role_stub),
    )
    assert "invalid" in result.lower() or "missing" in result.lower()
