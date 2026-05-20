"""list_invoices tool: returns invoices visible to the caller.

Admin → all invoices in org. Non-admin → only invoices uploaded by self.
Output is a compact textual list with id, file name, status, due_date,
uploader name. The LLM uses ids from this output to drive get_invoice /
show_modal.
"""
from typing import Optional, Literal
from uuid import UUID

from pydantic import BaseModel, Field
from langchain_core.runnables import RunnableConfig
from langchain_core.tools import StructuredTool


class ListInvoicesInput(BaseModel):
    status: Optional[Literal["created", "approved", "rejected", "processed"]] = Field(
        default=None,
        description=(
            "Filter by invoice status. Omit to return all statuses. "
            "Use 'created' for invoices awaiting boss decision; "
            "'approved' for invoices awaiting accountant processing."
        ),
    )


def create_list_invoices_tool(engine):
    """Factory wires the tool to a live EngineService."""

    async def _list(
        config: RunnableConfig,
        status: Optional[str] = None,
    ) -> str:
        cfg = (config or {}).get("configurable", {}) or {}
        caller_id_raw = cfg.get("caller_user_id")
        if not caller_id_raw:
            return "Error: caller_user_id missing in tool config"

        caller_id = UUID(str(caller_id_raw))
        user = await engine.user_storage.get_by_id(caller_id)
        if not user:
            return "Error: caller not found"

        from src.engine.models.invoice import InvoiceStatus
        status_enum: Optional[InvoiceStatus] = None
        if status:
            try:
                status_enum = InvoiceStatus(status)
            except ValueError:
                return f"Error: unknown status {status!r}"

        if user.is_admin:
            invoices = await engine.invoice_storage.list_by_org(user.org_id, status_enum)
        else:
            invoices = await engine.invoice_storage.list_for_user(user.org_id, user.id, status_enum)

        if not invoices:
            return "Нет счетов под этот фильтр."

        # Hydrate uploader names + file names for readability.
        user_ids = {inv.uploaded_by_user_id for inv in invoices}
        users_by_id = {}
        for uid in user_ids:
            u = await engine.user_storage.get_by_id(uid)
            users_by_id[uid] = u.name if u else str(uid)

        file_ids = {inv.file_id for inv in invoices}
        files_by_id = {}
        for fid in file_ids:
            f = await engine.user_file_storage.get_by_id(fid)
            files_by_id[fid] = f.original_filename if f else str(fid)

        lines = []
        for inv in invoices:
            lines.append(
                f"- id={inv.id} | file={files_by_id.get(inv.file_id, '?')} | "
                f"status={inv.status.value} | due={inv.due_date or '—'} | "
                f"uploader={users_by_id.get(inv.uploaded_by_user_id, '?')}"
            )
        return "\n".join(lines)

    return StructuredTool.from_function(
        coroutine=_list,
        name="list_invoices",
        description=(
            "List invoices visible to the caller. Admin sees all invoices in the "
            "organization; non-admin sees only invoices they uploaded themselves. "
            "Returns a compact text list with invoice ids the LLM can use in "
            "follow-up tool calls (get_invoice / show_modal)."
        ),
        args_schema=ListInvoicesInput,
    )
