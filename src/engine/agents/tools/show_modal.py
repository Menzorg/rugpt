"""show_modal: generic modal-protocol tool.

The tool is read-only: it does not write to DB. It validates that the calling
role is allowed to emit the requested action_types (per Role.agent_config.
allowed_action_types) and that each action's params match the registered
params_schema. On success it returns a brief confirmation string to the LLM;
the structured payload is recorded in ToolRuntime so ai_service can write it
into messages.metadata.modal during message persistence.
"""
from typing import Optional
from pydantic import BaseModel, Field, ValidationError

from langchain_core.runnables import RunnableConfig
from langchain_core.tools import StructuredTool, ToolException
from langgraph.prebuilt import ToolRuntime

from src.engine.agents.runtime import RuntimeContext
from src.engine.unified_logger import get_logger

logger = get_logger("agents")


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
        runtime: ToolRuntime[RuntimeContext],
        target: Optional[dict] = None,
    ) -> str:
        logger.info("show_modal: called title=%r runtime_is_none=%s", title, runtime is None)
        cfg = (config or {}).get("configurable", {}) or {}
        role = cfg.get("role")
        if role is None:
            raise ToolException("role not provided in runnable config")

        err = _validate(role, action_registry, actions)
        if err:
            logger.warning("show_modal: validation failed: %s", err)
            # Returning an error string lets the LLM see it in the tool result
            # and reason about it (or retry with different params).
            return err

        payload = {
            "title": title,
            "body": body,
            "actions": [a.model_dump() for a in actions],
            "target": target,
        }
        if runtime is not None:
            async with runtime.context.lock:
                runtime.context.called_modals.append(payload)
                logger.info("show_modal: appended payload to called_modals, total=%d payload=%s", len(runtime.context.called_modals), payload)
        else:
            logger.warning("show_modal: runtime is None — payload NOT appended to called_modals")
        return "Modal shown."

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
