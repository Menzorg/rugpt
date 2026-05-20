"""Action handlers for invoice approve/reject/mark_processed.

All three are dispatched through ActionRegistry. Registry-level permission
checks: admin-only for approve/reject; "active user" for mark_processed.
The accountant identity (organizations.accountant_user_id) is an async DB
lookup, so the finer-grained check (admin OR designated accountant) lives
in the handler body where awaits are allowed.
"""
from typing import Optional
from uuid import UUID

from pydantic import BaseModel

from src.engine.actions.registry import (
    ActionDefinition,
    ActionRegistry,
    PermissionDeniedError,
)


class InvoiceApproveParams(BaseModel):
    invoice_id: str


class InvoiceRejectParams(BaseModel):
    invoice_id: str
    reason: Optional[str] = None


class InvoiceMarkProcessedParams(BaseModel):
    invoice_id: str


async def invoice_approve_handler(engine, user, params: InvoiceApproveParams) -> dict:
    inv = await engine.invoice_service.approve(UUID(params.invoice_id), UUID(str(user.id)))
    return {"ok": True, "invoice": inv.to_dict()}


async def invoice_reject_handler(engine, user, params: InvoiceRejectParams) -> dict:
    inv = await engine.invoice_service.reject(
        UUID(params.invoice_id), UUID(str(user.id)), params.reason,
    )
    return {"ok": True, "invoice": inv.to_dict()}


async def invoice_mark_processed_handler(engine, user, params: InvoiceMarkProcessedParams) -> dict:
    # Accountant-identity check requires an async DB lookup, so it lives here
    # (registry-level permission is sync and can't await org_storage).
    org = await engine.org_storage.get_by_id(UUID(str(user.org_id)))
    accountant_user_id = org.accountant_user_id if org else None
    is_admin = bool(getattr(user, "is_admin", False))
    is_accountant = accountant_user_id is not None and accountant_user_id == user.id
    if not (is_admin or is_accountant):
        raise PermissionDeniedError("only admin or designated accountant can mark processed")
    inv = await engine.invoice_service.mark_processed(
        UUID(params.invoice_id), UUID(str(user.id)),
    )
    return {"ok": True, "invoice": inv.to_dict()}


def _admin_only(user, params) -> bool:
    return bool(getattr(user, "is_admin", False))


def register(registry: ActionRegistry) -> None:
    registry.register(ActionDefinition(
        action_type="invoice_approve",
        handler=invoice_approve_handler,
        params_schema=InvoiceApproveParams,
        permission=_admin_only,
    ))
    registry.register(ActionDefinition(
        action_type="invoice_reject",
        handler=invoice_reject_handler,
        params_schema=InvoiceRejectParams,
        permission=_admin_only,
    ))
    # mark_processed uses lax registry permission + tighter check in the handler
    # because the accountant identity is an org-level lookup that needs async DB.
    registry.register(ActionDefinition(
        action_type="invoice_mark_processed",
        handler=invoice_mark_processed_handler,
        params_schema=InvoiceMarkProcessedParams,
        permission=lambda u, p: bool(getattr(u, "is_active", False)),
    ))
