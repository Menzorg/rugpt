"""get_invoice tool: return detailed text for one invoice, scoped to caller.

Admin can read any invoice in their org. Non-admin only invoices they uploaded.
Output: a textual description including file name, status, due_date, uploader
and the file summary from RAG (so the LLM can reason about contents without
fetching the binary).
"""
from uuid import UUID

from pydantic import BaseModel, Field
from langchain_core.runnables import RunnableConfig
from langchain_core.tools import StructuredTool


class GetInvoiceInput(BaseModel):
    invoice_id: str = Field(description="UUID of the invoice")


def create_get_invoice_tool(engine):
    async def _get(
        invoice_id: str,
        config: RunnableConfig,
    ) -> str:
        cfg = (config or {}).get("configurable", {}) or {}
        caller_raw = cfg.get("caller_user_id")
        if not caller_raw:
            return "Error: caller_user_id missing"

        try:
            inv_uuid = UUID(invoice_id)
        except ValueError:
            return f"Error: invalid invoice_id {invoice_id!r}"

        caller_id = UUID(str(caller_raw))
        user = await engine.user_storage.get_by_id(caller_id)
        if not user:
            return "Error: caller not found"

        inv = await engine.invoice_storage.get_by_id(inv_uuid)
        if not inv or inv.org_id != user.org_id:
            return "Invoice not found or not visible"
        if not user.is_admin and inv.uploaded_by_user_id != user.id:
            return "Invoice not found or not visible"

        uploader = await engine.user_storage.get_by_id(inv.uploaded_by_user_id)
        f = await engine.user_file_storage.get_by_id(inv.file_id)

        lines = [
            f"id: {inv.id}",
            f"file: {f.original_filename if f else inv.file_id}",
            f"status: {inv.status.value}",
            f"due_date: {inv.due_date or '—'}",
            f"uploader: {uploader.name if uploader else inv.uploaded_by_user_id}",
        ]
        if f and f.summary:
            lines.append(f"summary: {f.summary}")
        if inv.status.value == "rejected" and inv.rejection_reason:
            lines.append(f"rejection_reason: {inv.rejection_reason}")
        return "\n".join(lines)

    return StructuredTool.from_function(
        coroutine=_get,
        name="get_invoice",
        description=(
            "Get detailed information about a single invoice by id. "
            "Returns 'Invoice not found or not visible' if the caller can't "
            "see this invoice (cross-tenant or non-admin viewing someone "
            "else's upload)."
        ),
        args_schema=GetInvoiceInput,
    )
