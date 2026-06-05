"""list_invoices tool: returns invoices visible to the caller.

Admin → all invoices in org. Non-admin → only invoices uploaded by self.
Output is a compact paginated textual list with id, file name, status, due_date,
uploader name, and a budgeted file summary. The LLM uses ids from this output to
drive get_invoice / show_modal.
"""
from datetime import date
from typing import Optional, Literal
from uuid import UUID

from pydantic import BaseModel, Field
from langchain_core.runnables import RunnableConfig
from langchain_core.tools import StructuredTool
from langgraph.prebuilt import ToolRuntime

from src.engine.agents.runtime import RuntimeContext
from src.engine.constants import IMAGE_TYPES
from src.engine.models.invoice import Invoice
from src.engine.models.user_file import UserFile
from src.engine.utils.token_counter import count_tokens

from .util.summary_budget import (
    format_summary_part_with_budget,
    per_summary_token_limit,
)


_PAGE_SIZE = 30
_SUMMARY_TOKENS_BUDGET = 2000
_MAX_SUMMARY_TOKENS_PER_INVOICE = 80


class ListInvoicesInput(BaseModel):
    uploaded_by_user_id: str = Field(
        default="",
        description="Filter by UUID of the user who uploaded the invoice (empty = any).",
    )
    status: Optional[Literal["created", "approved", "rejected", "processed"]] = Field(
        default=None,
        description=(
            "Filter by invoice status. Omit to return all statuses. "
            "Use 'created' for invoices awaiting boss decision; "
            "'approved' for invoices awaiting accountant processing."
        ),
    )
    due_from: Optional[date] = Field(
        default=None,
        description="Filter invoices with due_date on or after this date, YYYY-MM-DD (empty = no lower bound).",
    )
    due_to: Optional[date] = Field(
        default=None,
        description="Filter invoices with due_date on or before this date, YYYY-MM-DD (empty = no upper bound).",
    )
    created_from: Optional[date] = Field(
        default=None,
        description="Filter invoices created on or after this date, YYYY-MM-DD (empty = no lower bound).",
    )
    created_to: Optional[date] = Field(
        default=None,
        description="Filter invoices created on or before this date, YYYY-MM-DD (empty = no upper bound).",
    )
    page: int = Field(
        default=1,
        ge=1,
        description="Page number for paginated listing (default 1). Page size is 30.",
    )


def _page_items(items: list[Invoice], page: int) -> tuple[list[Invoice], int, int, int, int]:
    total = len(items)
    total_pages = max(1, (total + _PAGE_SIZE - 1) // _PAGE_SIZE)
    page = max(1, min(page, total_pages))
    start = (page - 1) * _PAGE_SIZE
    end = min(start + _PAGE_SIZE, total)
    return items[start:end], page, total_pages, start, end


def _filter_invoices(
    invoices: list[Invoice],
    uploaded_by_user_id: Optional[UUID],
    due_from: Optional[date],
    due_to: Optional[date],
    created_from: Optional[date],
    created_to: Optional[date],
) -> list[Invoice]:
    """Apply optional list filters to an already permission-scoped invoice set."""
    filtered = invoices
    if uploaded_by_user_id is not None:
        filtered = [inv for inv in filtered if inv.uploaded_by_user_id == uploaded_by_user_id]
    if due_from is not None:
        filtered = [inv for inv in filtered if inv.due_date is not None and inv.due_date >= due_from]
    if due_to is not None:
        filtered = [inv for inv in filtered if inv.due_date is not None and inv.due_date <= due_to]
    if created_from is not None:
        filtered = [inv for inv in filtered if inv.created_at.date() >= created_from]
    if created_to is not None:
        filtered = [inv for inv in filtered if inv.created_at.date() <= created_to]
    return filtered


def _format_invoice_page(
    invoices: list[Invoice],
    page: int,
    total_pages: int,
    start: int,
    end: int,
    total: int,
    users_by_id: dict[UUID, str],
    files_by_id: dict[UUID, UserFile],
    summary_tokens_spent_before: int,
) -> tuple[str, int]:
    remaining = max(0, _SUMMARY_TOKENS_BUDGET - summary_tokens_spent_before)
    summary_items_count = sum(
        1 for inv in invoices
        if (file := files_by_id.get(inv.file_id)) is not None and file.summary
    )
    per_item_limit = per_summary_token_limit(
        remaining,
        summary_items_count,
        max_tokens_per_item=_MAX_SUMMARY_TOKENS_PER_INVOICE,
    )

    lines = []
    tokens_spent = 0
    for inv in invoices:
        file = files_by_id.get(inv.file_id)
        budget_result = format_summary_part_with_budget(
            file.summary if file else None,
            remaining,
            per_item_limit,
        )
        remaining = budget_result.remaining_tokens
        tokens_spent += budget_result.tokens_spent
        filename = file.original_filename if file else str(inv.file_id)
        is_image = bool(file and (file.file_type or "").lower() in IMAGE_TYPES)
        lines.append(
            f"- id={inv.id} | file={filename} | "
            f"status={inv.status.value} | due={inv.due_date or '-'} | "
            f"uploader={users_by_id.get(inv.uploaded_by_user_id, str(inv.uploaded_by_user_id))} | "
            f"is_image={'true' if is_image else 'false'} | "
            f"{budget_result.summary_part}"
        )

    span = f"{start + 1}-{end} of {total}"
    header = f"Invoices {span} (page {page}/{total_pages}):"
    footer = ""
    if summary_tokens_spent_before >= _SUMMARY_TOKENS_BUDGET:
        footer = "\n[SUMMARY BUDGET EXHAUSTED FROM PREVIOUS CALLS. USE STATUS/PAGE FILTERS TO NARROW RESULTS.]"
    return f"{header}\n" + "\n".join(lines) + footer, tokens_spent


def create_list_invoices_tool(engine):
    """Factory wires the tool to a live EngineService."""

    async def _list(
        config: RunnableConfig,
        runtime: ToolRuntime[RuntimeContext],
        uploaded_by_user_id: str = "",
        status: Optional[str] = None,
        due_from: Optional[date] = None,
        due_to: Optional[date] = None,
        created_from: Optional[date] = None,
        created_to: Optional[date] = None,
        page: int = 1,
    ) -> str:
        cfg = (config or {}).get("configurable", {}) or {}
        caller_id_raw = cfg.get("caller_user_id")
        if not caller_id_raw:
            return "Error: caller_user_id missing in tool config"

        caller_id = UUID(str(caller_id_raw))
        user = await engine.user_storage.get_by_id(caller_id)
        if not user:
            return "Error: caller not found"

        uploader_uuid: Optional[UUID] = None
        if uploaded_by_user_id:
            try:
                uploader_uuid = UUID(uploaded_by_user_id)
            except ValueError:
                return f"Error: invalid uploaded_by_user_id {uploaded_by_user_id!r}"

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

        invoices = _filter_invoices(
            invoices,
            uploaded_by_user_id=uploader_uuid,
            due_from=due_from,
            due_to=due_to,
            created_from=created_from,
            created_to=created_to,
        )

        if not invoices:
            return "Нет счетов под этот фильтр."

        page_invoices, page, total_pages, start, end = _page_items(invoices, page)

        # Hydrate only the visible page for readability and compact summaries.
        user_ids = {inv.uploaded_by_user_id for inv in page_invoices}
        users_by_id = {}
        for uid in user_ids:
            u = await engine.user_storage.get_by_id(uid)
            users_by_id[uid] = u.name if u else str(uid)

        file_ids = {inv.file_id for inv in page_invoices}
        files_by_id = {}
        for fid in file_ids:
            f = await engine.user_file_storage.get_by_id(fid)
            if f:
                files_by_id[fid] = f

        async with runtime.context.lock:
            summary_before = runtime.context.list_invoices_summary_tokens_spent
            result, tokens_spent = _format_invoice_page(
                page_invoices,
                page,
                total_pages,
                start,
                end,
                len(invoices),
                users_by_id,
                files_by_id,
                summary_tokens_spent_before=summary_before,
            )
            runtime.context.list_invoices_summary_tokens_spent += tokens_spent
            runtime.context.total_tokens_spent += count_tokens(result)
            return result

    return StructuredTool.from_function(
        coroutine=_list,
        name="list_invoices",
        description=(
            "List invoices visible to the caller. Admin sees all invoices in the "
            "organization; non-admin sees only invoices they uploaded themselves. "
            "Supports uploader, status, due date, created date, and page filters. "
            "Returns a paginated compact text list with invoice ids and short "
            "summaries the LLM can use in follow-up tool calls (get_invoice / show_modal)."
        ),
        args_schema=ListInvoicesInput,
    )
