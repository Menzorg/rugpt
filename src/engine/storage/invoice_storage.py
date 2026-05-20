"""asyncpg CRUD for invoices."""
from datetime import date, datetime
from typing import List, Optional
from uuid import UUID

from src.engine.storage.base import BaseStorage
from src.engine.models.invoice import Invoice, InvoiceStatus


class InvoiceStorage(BaseStorage):
    async def create(
        self,
        org_id: UUID,
        file_id: UUID,
        uploaded_by_user_id: UUID,
        due_date: Optional[date],
    ) -> Invoice:
        row = await self.fetchrow(
            """
            INSERT INTO invoices (id, org_id, file_id, uploaded_by_user_id, due_date)
            VALUES (gen_random_uuid(), $1, $2, $3, $4)
            RETURNING *
            """,
            org_id, file_id, uploaded_by_user_id, due_date,
        )
        return self._row_to_invoice(row)

    async def get_by_id(self, invoice_id: UUID) -> Optional[Invoice]:
        row = await self.fetchrow(
            "SELECT * FROM invoices WHERE id = $1 AND is_active = true",
            invoice_id,
        )
        return self._row_to_invoice(row) if row else None

    async def list_by_org(
        self,
        org_id: UUID,
        status: Optional[InvoiceStatus] = None,
    ) -> List[Invoice]:
        if status is None:
            rows = await self.fetch(
                "SELECT * FROM invoices WHERE org_id = $1 AND is_active = true "
                "ORDER BY created_at DESC",
                org_id,
            )
        else:
            rows = await self.fetch(
                "SELECT * FROM invoices WHERE org_id = $1 AND status = $2 AND is_active = true "
                "ORDER BY created_at DESC",
                org_id, status.value,
            )
        return [self._row_to_invoice(r) for r in rows]

    async def list_for_user(
        self,
        org_id: UUID,
        user_id: UUID,
        status: Optional[InvoiceStatus] = None,
    ) -> List[Invoice]:
        if status is None:
            rows = await self.fetch(
                "SELECT * FROM invoices WHERE org_id = $1 AND uploaded_by_user_id = $2 "
                "AND is_active = true ORDER BY created_at DESC",
                org_id, user_id,
            )
        else:
            rows = await self.fetch(
                "SELECT * FROM invoices WHERE org_id = $1 AND uploaded_by_user_id = $2 "
                "AND status = $3 AND is_active = true ORDER BY created_at DESC",
                org_id, user_id, status.value,
            )
        return [self._row_to_invoice(r) for r in rows]

    async def update_status(
        self,
        invoice_id: UUID,
        status: InvoiceStatus,
        actor_user_id: UUID,
        rejection_reason: Optional[str] = None,
    ) -> Optional[Invoice]:
        if status == InvoiceStatus.APPROVED:
            row = await self.fetchrow(
                "UPDATE invoices SET status = $2, approved_by_user_id = $3, "
                "approved_at = NOW(), updated_at = NOW() "
                "WHERE id = $1 AND is_active = true RETURNING *",
                invoice_id, status.value, actor_user_id,
            )
        elif status == InvoiceStatus.REJECTED:
            row = await self.fetchrow(
                "UPDATE invoices SET status = $2, rejected_at = NOW(), "
                "rejection_reason = $3, updated_at = NOW() "
                "WHERE id = $1 AND is_active = true RETURNING *",
                invoice_id, status.value, rejection_reason,
            )
        elif status == InvoiceStatus.PROCESSED:
            row = await self.fetchrow(
                "UPDATE invoices SET status = $2, processed_by_user_id = $3, "
                "processed_at = NOW(), updated_at = NOW() "
                "WHERE id = $1 AND is_active = true RETURNING *",
                invoice_id, status.value, actor_user_id,
            )
        else:
            row = await self.fetchrow(
                "UPDATE invoices SET status = $2, updated_at = NOW() "
                "WHERE id = $1 AND is_active = true RETURNING *",
                invoice_id, status.value,
            )
        return self._row_to_invoice(row) if row else None

    async def list_due_today_or_tomorrow(
        self,
        org_id: UUID,
        today: date,
    ) -> List[Invoice]:
        """Used by Plan 3 scheduler. Returns approved+unprocessed invoices
        whose due_date is today or tomorrow."""
        rows = await self.fetch(
            """
            SELECT * FROM invoices
            WHERE org_id = $1
              AND status = 'approved'
              AND processed_at IS NULL
              AND is_active = true
              AND (due_date = $2 OR due_date = $2 + 1)
            ORDER BY due_date
            """,
            org_id, today,
        )
        return [self._row_to_invoice(r) for r in rows]

    async def list_due_today_or_tomorrow_pending_notif(
        self,
        org_id: UUID,
        today: date,
    ) -> List[Invoice]:
        """Used by Plan 3 scheduler invoice-due job. Like list_due_today_or_tomorrow
        but skips invoices already notified for today (per-day idempotency via
        last_notified_for_date)."""
        rows = await self.fetch(
            """
            SELECT * FROM invoices
            WHERE org_id = $1
              AND status = 'approved'
              AND processed_at IS NULL
              AND is_active = true
              AND (due_date = $2 OR due_date = $2 + 1)
              AND (last_notified_for_date IS NULL OR last_notified_for_date < $2)
            ORDER BY due_date
            """,
            org_id, today,
        )
        return [self._row_to_invoice(r) for r in rows]

    async def update_last_notified_for_date(self, invoice_id: UUID, when: date) -> None:
        await self.execute(
            "UPDATE invoices SET last_notified_for_date = $2, updated_at = NOW() "
            "WHERE id = $1",
            invoice_id, when,
        )

    def _row_to_invoice(self, row) -> Invoice:
        return Invoice(
            id=row["id"],
            org_id=row["org_id"],
            file_id=row["file_id"],
            uploaded_by_user_id=row["uploaded_by_user_id"],
            due_date=row["due_date"],
            status=InvoiceStatus(row["status"]),
            approved_by_user_id=row["approved_by_user_id"],
            approved_at=row["approved_at"],
            rejected_at=row["rejected_at"],
            rejection_reason=row["rejection_reason"],
            processed_at=row["processed_at"],
            processed_by_user_id=row["processed_by_user_id"],
            is_active=row["is_active"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )
