"""Registers all production action types at engine startup."""
from src.engine.actions.registry import ActionRegistry
from src.engine.actions import invoice_actions


def register_all(registry: ActionRegistry) -> None:
    invoice_actions.register(registry)
