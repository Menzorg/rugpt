"""Invoice routes: upload, list, single-get.

Action handlers (approve/reject/mark_processed) live under the generic
/actions/{action_type} dispatcher — not under /invoices/. This keeps the
"AI prepares, human decides" surface uniform.
"""
from datetime import date
from typing import Optional
from uuid import UUID

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile

from src.engine.routes.auth import get_current_user
from src.engine.services.engine_service import get_engine_service
from src.engine.unified_logger import get_logger

logger = get_logger("routes")

router = APIRouter(prefix="/invoices", tags=["invoices"])


def _can_view(user, invoice) -> bool:
    if user.is_admin and user.org_id == invoice.org_id:
        return True
    return invoice.uploaded_by_user_id == user.id


@router.post("/")
async def upload_invoice(
    file: UploadFile = File(...),
    due_date: Optional[str] = Form(None),
    current_user: dict = Depends(get_current_user),
):
    """Any active user in the org can upload an invoice. due_date is optional
    but recommended (used by scheduler in Plan 3 to remind the accountant)."""
    engine = get_engine_service()
    user = await engine.user_storage.get_by_id(UUID(str(current_user["user_id"])))
    if not user or not user.is_active:
        raise HTTPException(status_code=401, detail="Inactive user")

    data = await file.read()
    if not data:
        raise HTTPException(status_code=400, detail="Empty file")

    parsed_due: Optional[date] = None
    if due_date:
        try:
            parsed_due = date.fromisoformat(due_date)
        except ValueError:
            raise HTTPException(status_code=400, detail="due_date must be ISO format YYYY-MM-DD")

    invoice = await engine.invoice_service.upload(
        org_id=user.org_id,
        uploader_user_id=user.id,
        filename=file.filename or "invoice.pdf",
        data=data,
        due_date=parsed_due,
    )
    return invoice.to_dict()


@router.get("/")
async def list_invoices(
    status: Optional[str] = Query(None),
    current_user: dict = Depends(get_current_user),
):
    """Admin sees all in org. Non-admin sees only own."""
    engine = get_engine_service()
    user = await engine.user_storage.get_by_id(UUID(str(current_user["user_id"])))
    if not user:
        raise HTTPException(status_code=401, detail="User not found")

    from src.engine.models.invoice import InvoiceStatus
    status_enum: Optional[InvoiceStatus] = None
    if status:
        try:
            status_enum = InvoiceStatus(status)
        except ValueError:
            raise HTTPException(status_code=400, detail=f"unknown status: {status}")

    if user.is_admin:
        rows = await engine.invoice_storage.list_by_org(user.org_id, status_enum)
    else:
        rows = await engine.invoice_storage.list_for_user(user.org_id, user.id, status_enum)
    return [r.to_dict() for r in rows]


@router.get("/{invoice_id}")
async def get_invoice(
    invoice_id: str,
    current_user: dict = Depends(get_current_user),
):
    engine = get_engine_service()
    user = await engine.user_storage.get_by_id(UUID(str(current_user["user_id"])))
    if not user:
        raise HTTPException(status_code=401, detail="User not found")

    try:
        inv_uuid = UUID(invoice_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid invoice_id")

    inv = await engine.invoice_storage.get_by_id(inv_uuid)
    if not inv or inv.org_id != user.org_id or not _can_view(user, inv):
        raise HTTPException(status_code=404, detail="Invoice not found")
    return inv.to_dict()
