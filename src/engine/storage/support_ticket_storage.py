"""
SupportTicketStorage — CRUD + atomic CAS take for tech support tickets.

Lifecycle: open -> in_progress (after take) -> closed.
The `take` method uses a single atomic UPDATE with WHERE-clause CAS so that
exactly one operator wins under concurrent contention.
"""
from typing import List, Optional
from uuid import UUID

from src.engine.models.support_ticket import (
    ClosedByRole,
    SupportTicket,
    SupportTicketCategory,
    SupportTicketStatus,
)
from src.engine.storage.base import BaseStorage


class SupportTicketStorage(BaseStorage):
    """PostgreSQL CRUD for support_tickets table (migration 023)."""

    async def create(self, t: SupportTicket) -> SupportTicket:
        row = await self.fetchrow(
            """
            INSERT INTO support_tickets (
                id, requester_user_id, requester_org_id, category, status,
                assignee_user_id, ai_handoff_at, ai_first_response_at,
                closed_at, closed_by_user_id, closed_by_role,
                title, created_at, updated_at
            ) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14)
            RETURNING *
            """,
            t.id, t.requester_user_id, t.requester_org_id,
            t.category.value, t.status.value,
            t.assignee_user_id, t.ai_handoff_at, t.ai_first_response_at,
            t.closed_at, t.closed_by_user_id,
            t.closed_by_role.value if t.closed_by_role else None,
            t.title, t.created_at, t.updated_at,
        )
        return self._row_to_ticket(row)

    async def get_by_id(self, ticket_id: UUID) -> Optional[SupportTicket]:
        row = await self.fetchrow(
            "SELECT * FROM support_tickets WHERE id = $1", ticket_id
        )
        return self._row_to_ticket(row) if row else None

    async def list_by_requester(
        self,
        user_id: UUID,
        status: Optional[SupportTicketStatus] = None,
        limit: int = 50,
    ) -> List[SupportTicket]:
        if status is not None:
            rows = await self.fetch(
                """
                SELECT * FROM support_tickets
                WHERE requester_user_id = $1 AND status = $2
                ORDER BY created_at DESC
                LIMIT $3
                """,
                user_id, status.value, limit,
            )
        else:
            rows = await self.fetch(
                """
                SELECT * FROM support_tickets
                WHERE requester_user_id = $1
                ORDER BY created_at DESC
                LIMIT $2
                """,
                user_id, limit,
            )
        return [self._row_to_ticket(r) for r in rows]

    async def list_queue(self, limit: int = 100) -> List[SupportTicket]:
        rows = await self.fetch(
            """
            SELECT * FROM support_tickets
            WHERE status = 'open' AND assignee_user_id IS NULL
            ORDER BY created_at ASC
            LIMIT $1
            """,
            limit,
        )
        return [self._row_to_ticket(r) for r in rows]

    async def list_by_assignee(
        self, operator_id: UUID, limit: int = 100
    ) -> List[SupportTicket]:
        rows = await self.fetch(
            """
            SELECT * FROM support_tickets
            WHERE assignee_user_id = $1 AND status = 'in_progress'
            ORDER BY created_at DESC
            LIMIT $2
            """,
            operator_id, limit,
        )
        return [self._row_to_ticket(r) for r in rows]

    async def take(
        self, ticket_id: UUID, operator_id: UUID
    ) -> Optional[SupportTicket]:
        """
        Atomic CAS take.

        Assigns operator iff ticket is still open + unassigned. Returns the
        updated ticket if this caller won the race, None if it lost (already
        taken / closed). Single UPDATE with WHERE-clause CAS guarantees
        exactly-one-winner under concurrency.
        """
        row = await self.fetchrow(
            """
            UPDATE support_tickets
               SET assignee_user_id = $1,
                   status = 'in_progress',
                   updated_at = NOW()
             WHERE id = $2
               AND assignee_user_id IS NULL
               AND status = 'open'
            RETURNING *
            """,
            operator_id, ticket_id,
        )
        return self._row_to_ticket(row) if row else None

    async def set_ai_first_response(self, ticket_id: UUID) -> None:
        """Stamp ai_first_response_at on first AI reply (idempotent)."""
        await self.execute(
            """
            UPDATE support_tickets
               SET ai_first_response_at = NOW(),
                   updated_at = NOW()
             WHERE id = $1 AND ai_first_response_at IS NULL
            """,
            ticket_id,
        )

    # TODO(consistency): take/close/reopen all RETURNING * the updated row;
    # set_ai_handoff and set_ai_first_response do not. Align signatures so
    # callers don't need a follow-up SELECT.
    async def set_ai_handoff(self, ticket_id: UUID) -> None:
        """Stamp ai_handoff_at when AI hands off to human operator (idempotent)."""
        await self.execute(
            """
            UPDATE support_tickets
               SET ai_handoff_at = NOW(),
                   updated_at = NOW()
             WHERE id = $1 AND ai_handoff_at IS NULL
            """,
            ticket_id,
        )

    async def close_ticket(
        self, ticket_id: UUID, by_user_id: UUID, by_role: ClosedByRole
    ) -> Optional[SupportTicket]:
        """Set ticket status to 'closed' iff currently open or in_progress.

        Returns the closed ticket on success.
        Returns None if the ticket was already closed (caller must call get_by_id
        separately if it needs the current row state). NOT idempotent — repeat
        calls return None, not the closed row.
        """
        row = await self.fetchrow(
            """
            UPDATE support_tickets
               SET status = 'closed',
                   closed_at = NOW(),
                   closed_by_user_id = $1,
                   closed_by_role = $2,
                   updated_at = NOW()
             WHERE id = $3 AND status IN ('open', 'in_progress')
            RETURNING *
            """,
            by_user_id, by_role.value, ticket_id,
        )
        return self._row_to_ticket(row) if row else None

    async def reopen(self, ticket_id: UUID) -> Optional[SupportTicket]:
        """
        Reopen a closed ticket back to in_progress (clears closed_* fields).
        Returns None if ticket was not in 'closed' state.
        """
        row = await self.fetchrow(
            """
            UPDATE support_tickets
               SET status = 'in_progress',
                   closed_at = NULL,
                   closed_by_user_id = NULL,
                   closed_by_role = NULL,
                   updated_at = NOW()
             WHERE id = $1 AND status = 'closed'
            RETURNING *
            """,
            ticket_id,
        )
        return self._row_to_ticket(row) if row else None

    @staticmethod
    def _row_to_ticket(row) -> Optional[SupportTicket]:
        if row is None:
            return None
        return SupportTicket(
            id=row["id"],
            requester_user_id=row["requester_user_id"],
            requester_org_id=row["requester_org_id"],
            category=SupportTicketCategory(row["category"]),
            status=SupportTicketStatus(row["status"]),
            assignee_user_id=row["assignee_user_id"],
            ai_handoff_at=row["ai_handoff_at"],
            ai_first_response_at=row["ai_first_response_at"],
            closed_at=row["closed_at"],
            closed_by_user_id=row["closed_by_user_id"],
            closed_by_role=(
                ClosedByRole(row["closed_by_role"])
                if row["closed_by_role"] else None
            ),
            title=row["title"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )
