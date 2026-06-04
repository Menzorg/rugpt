"""InvoiceService: upload + status transitions.

Upload reuses the existing FileService for binary storage (no separate
invoice-binary path). Status transitions enforce the only valid graph:
created -> approved | rejected; approved -> processed.
"""
from datetime import date
from typing import Optional
from uuid import UUID

from src.engine.models.invoice import Invoice, InvoiceStatus
from src.engine.unified_logger import get_logger

logger = get_logger("services")


class InvoiceService:
    def __init__(self, invoice_storage, file_service):
        self._invoice_storage = invoice_storage
        self._file_service = file_service

    async def upload(
        self,
        org_id: UUID,
        uploader_user_id: UUID,
        filename: str,
        data: bytes,
        due_date: Optional[date],
    ) -> Invoice:
        """Stores binary via FileService, kicks off RAG indexing (for AI summary
        which the invoice_clerk role uses in modals), then creates the invoice
        row.

        Note: FileService.upload no longer auto-enqueues RAG ingestion (per
        file_service.py:104 — owner must opt in). We explicitly enqueue here
        because invoice summaries are core to the invoice_clerk UX in Plan 3.
        Ingestion is async via ThreadPoolExecutor; summary lands in
        user_files.summary minutes later.
        """
        uf = await self._file_service.upload(
            org_id=org_id,
            user_id=uploader_user_id,
            uploaded_by_user_id=uploader_user_id,
            filename=filename,
            data=data,
            is_public=False,
        )
        # Best-effort RAG-ingest enqueue. Skipping on error so a transient
        # ingestion failure doesn't block invoice creation.
        try:
            await self._file_service.index_for_rag(uf.id, uploader_user_id, is_invoice=True)
        except Exception as exc:
            logger.warning(
                "invoice upload: index_for_rag failed for file=%s: %s — invoice still created with empty summary",
                uf.id, exc,
            )
        invoice = await self._invoice_storage.create(
            org_id=org_id,
            file_id=uf.id,
            uploaded_by_user_id=uploader_user_id,
            due_date=due_date,
        )
        logger.info(
            "invoice uploaded id=%s file=%s uploader=%s due_date=%s",
            invoice.id, uf.id, uploader_user_id, due_date,
        )
        return invoice

    async def approve(self, invoice_id: UUID, actor_user_id: UUID) -> Invoice:
        inv = await self._invoice_storage.get_by_id(invoice_id)
        if not inv:
            raise ValueError(f"invoice {invoice_id} not found")
        if inv.status != InvoiceStatus.CREATED:
            raise ValueError(f"invoice in status {inv.status.value}, cannot approve")
        updated = await self._invoice_storage.update_status(
            invoice_id, InvoiceStatus.APPROVED, actor_user_id,
        )
        logger.info("invoice approved id=%s actor=%s", invoice_id, actor_user_id)
        return updated

    async def reject(self, invoice_id: UUID, actor_user_id: UUID, reason: Optional[str]) -> Invoice:
        inv = await self._invoice_storage.get_by_id(invoice_id)
        if not inv:
            raise ValueError(f"invoice {invoice_id} not found")
        if inv.status != InvoiceStatus.CREATED:
            raise ValueError(f"invoice in status {inv.status.value}, cannot reject")
        updated = await self._invoice_storage.update_status(
            invoice_id, InvoiceStatus.REJECTED, actor_user_id, rejection_reason=reason,
        )
        logger.info("invoice rejected id=%s actor=%s reason=%r", invoice_id, actor_user_id, reason)
        return updated

    async def mark_processed(self, invoice_id: UUID, actor_user_id: UUID) -> Invoice:
        inv = await self._invoice_storage.get_by_id(invoice_id)
        if not inv:
            raise ValueError(f"invoice {invoice_id} not found")
        if inv.status != InvoiceStatus.APPROVED:
            raise ValueError(f"invoice in status {inv.status.value}, cannot mark processed")
        updated = await self._invoice_storage.update_status(
            invoice_id, InvoiceStatus.PROCESSED, actor_user_id,
        )
        logger.info("invoice processed id=%s actor=%s", invoice_id, actor_user_id)
        return updated
