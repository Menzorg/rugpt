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
from langgraph.prebuilt import ToolRuntime

from src.engine.agents.runtime import RuntimeContext
from src.engine.config import Config
from src.engine.constants import IMAGE_TYPES
from src.engine.utils.image_parser import image_bytes_to_data_url
from src.engine.utils.token_counter import count_tokens
from src.engine.unified_logger import get_logger

logger = get_logger("agents")


class GetInvoiceInput(BaseModel):
    invoice_id: str = Field(description="UUID of the invoice")


def create_get_invoice_tool(engine):
    async def _get(
        invoice_id: str,
        config: RunnableConfig,
        runtime: ToolRuntime[RuntimeContext],
    ):
        cfg = (config or {}).get("configurable", {}) or {}
        caller_raw = cfg.get("caller_user_id")
        async def _count_and_return(s: str) -> str:
            try:
                if runtime is not None:
                    async with runtime.context.lock:
                        runtime.context.total_tokens_spent += count_tokens(s)
            except Exception:
                # Token counting is best-effort; don't fail the tool if it errors
                pass
            return s

        if not caller_raw:
            return await _count_and_return("Error: caller_user_id missing")
        
        try:
            inv_uuid = UUID(invoice_id)
        except ValueError:
            return await _count_and_return(f"Error: invalid invoice_id {invoice_id!r}")
        
        caller_id = UUID(str(caller_raw))
        user = await engine.user_storage.get_by_id(caller_id)
        if not user:
            return await _count_and_return("Error: caller not found")
        
        inv = await engine.invoice_storage.get_by_id(inv_uuid)
        if (
            not inv
            or inv.org_id != user.org_id
            or (not user.is_admin and inv.uploaded_by_user_id != user.id)
        ):
            return await _count_and_return("Invoice not found or not visible")
        
        uploader = await engine.user_storage.get_by_id(inv.uploaded_by_user_id)
        f = await engine.user_file_storage.get_by_id(inv.file_id)

        lines = [
            f"id: {inv.id}",
            f"file: {f.original_filename if f else inv.file_id}",
            f"file_id: {inv.file_id}",
            f"status: {inv.status.value}",
            f"due_date: {inv.due_date or '—'}",
            f"uploader: {uploader.name if uploader else inv.uploaded_by_user_id}",
        ]
        if f and f.is_table:
            lines.append("is_table: true")
        if f and f.summary:
            lines.append(f"summary: {f.summary}")
        if inv.status.value == "rejected" and inv.rejection_reason:
            lines.append(f"rejection_reason: {inv.rejection_reason}")
        result = "\n".join(lines)

        is_image = bool(f and (f.file_type or "").lower() in IMAGE_TYPES)
        if is_image:
            try:
                data = await engine.storage_adapter.read(f.storage_key)
                data_url = image_bytes_to_data_url(data, file_type=(f.file_type or "").lower())
                content = [
                    {"type": "text", "text": result},
                    {"type": "image_url", "image_url": {"url": data_url}},
                ]
            except Exception as exc:
                # Fall back to text-only if the image can't be read/encoded.
                logger.warning("get_invoice: failed to attach image for invoice %s: %s", inv.id, exc)
                content = result
        else:
            content = result

        # Token accounting always uses the text portion only; the returned
        # content may be a plain string or a multimodal list.
        await _count_and_return(result)
        return content

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
