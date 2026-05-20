"""show_modal: generic modal-protocol tool.

The tool is read-only: it does not write to DB. It validates that the calling
role is allowed to emit the requested action_types (per Role.agent_config.
allowed_action_types) and that each action's params match the registered
params_schema. On success it returns a brief confirmation string to the LLM;
the structured payload is stashed in the runnable's tool metadata for the
ai_service to pick up and write into messages.metadata.modal during message
persistence.
"""
from typing import Optional
from pydantic import BaseModel, Field, ValidationError

from langchain_core.runnables import RunnableConfig
from langchain_core.tools import StructuredTool, ToolException


class ShowModalAction(BaseModel):
    label: str = Field(description="Button label visible to the user")
    action_type: str = Field(description="Registered action_type the click will dispatch")
    params: dict = Field(default_factory=dict, description="Params for the action handler")


class ShowModalInput(BaseModel):
    title: str = Field(description="Modal title")
    body: str = Field(description="Modal body (markdown supported)")
    actions: list[ShowModalAction] = Field(description="One or more action buttons")
    target: Optional[dict] = Field(
        default=None,
        description=(
            "Optional hint to the frontend about which resource this modal "
            "targets, e.g. {\"type\": \"invoice\", \"id\": \"<uuid>\"}. "
            "Frontend uses it to derive resolved-state on refresh."
        ),
    )


def _validate(role, registry, actions: list[ShowModalAction]) -> Optional[str]:
    """Return error string if any action is invalid, else None."""
    allowed = (role.agent_config or {}).get("allowed_action_types", []) if role else []
    for a in actions:
        if a.action_type not in allowed:
            return (
                f"action_type {a.action_type!r} is not allowed for this role "
                f"(allowed: {allowed})"
            )
        definition = registry.get(a.action_type)
        if definition is None:
            return f"action_type {a.action_type!r} is unknown / not registered on the engine"
        try:
            definition.params_schema(**(a.params or {}))
        except ValidationError as exc:
            return f"invalid params for {a.action_type}: {exc}"
    return None


def create_show_modal_tool(action_registry):
    """Factory: returns a LangChain StructuredTool bound to the given registry."""

    async def _show_modal(
        title: str,
        body: str,
        actions: list[ShowModalAction],
        config: RunnableConfig,
        target: Optional[dict] = None,
    ) -> str:
        cfg = (config or {}).get("configurable", {}) or {}
        role = cfg.get("role")
        if role is None:
            raise ToolException("role not provided in runnable config")

        err = _validate(role, action_registry, actions)
        if err:
            # Returning an error string lets the LLM see it in the tool result
            # and reason about it (or retry with different params).
            return err

        # Stash payload for ai_service to attach to the persisted message.
        # We use the standard LangChain "tool output metadata" channel: when
        # the tool returns a string, the ai_service inspects `intermediate_steps`
        # of the agent run and pulls the modal payload from there. See
        # ai_service modal payload handling.
        payload = {
            "title": title,
            "body": body,
            "actions": [a.model_dump() for a in actions],
            "target": target,
        }
        # We tag the success string deterministically so ai_service can find it
        # in the tool log when extracting payloads.
        import json
        return f"<<MODAL_EMITTED>>{json.dumps(payload, ensure_ascii=False)}<</MODAL_EMITTED>>"

    return StructuredTool.from_function(
        coroutine=_show_modal,
        name="show_modal",
        description=(
            "Display a confirmation modal to the user with one or more action "
            "buttons. The tool itself does not mutate any data — it only "
            "renders a card. When the user clicks a button, the chosen "
            "action_type's handler runs on the backend. Use this whenever you "
            "need a human-in-the-loop decision."
        ),
        args_schema=ShowModalInput,
    )
