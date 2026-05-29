"""Action registry: registers action_type → handler/permission/params_schema.

The registry is the single dispatcher used by `POST /api/v1/actions/{action_type}`.
Action handlers mutate target resources (e.g., invoice_approve flips an invoice
status). All actions go through this single chokepoint so authorization,
parameter validation and audit can be enforced uniformly.
"""
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Dict, Optional

from pydantic import BaseModel, ValidationError


class ActionError(Exception):
    """Base error for action dispatch failures."""


class UnknownActionError(ActionError):
    """Raised when an unknown action_type is requested."""


class PermissionDeniedError(ActionError):
    """Raised when the user doesn't pass the permission check."""


@dataclass
class ActionDefinition:
    action_type: str
    handler: Callable[..., Awaitable[Dict[str, Any]]]   # async (engine, user, params) -> dict
    params_schema: type[BaseModel]
    permission: Callable[[Any, BaseModel], bool]


class ActionRegistry:
    def __init__(self):
        self._defs: Dict[str, ActionDefinition] = {}

    def register(self, definition: ActionDefinition) -> None:
        if definition.action_type in self._defs:
            raise ValueError(f"action_type {definition.action_type!r} already registered")
        self._defs[definition.action_type] = definition

    def reset(self) -> None:
        """Drop all registered actions. Mirrors the EngineService lifecycle so a
        re-initialize after close() can repopulate without duplicate-registration."""
        self._defs.clear()

    def get(self, action_type: str) -> Optional[ActionDefinition]:
        return self._defs.get(action_type)

    def list_action_types(self) -> list[str]:
        return list(self._defs.keys())

    async def dispatch(self, *, engine, action_type: str, params: dict, user) -> Dict[str, Any]:
        definition = self.get(action_type)
        if definition is None:
            raise UnknownActionError(f"unknown action_type: {action_type}")

        try:
            parsed = definition.params_schema(**(params or {}))
        except ValidationError as exc:
            raise ActionError(f"invalid params for {action_type}: {exc}") from exc

        if not definition.permission(user, parsed):
            raise PermissionDeniedError(f"user not allowed to invoke {action_type}")

        return await definition.handler(engine, user, parsed)
