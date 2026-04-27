"""
SupportTicketEventStorage — append-only audit trail for support tickets.

Insert is a single INSERT with RETURNING (atomic). list_by_ticket reads in
DESC created_at order, matching idx_support_ticket_events_ticket(ticket_id,
created_at DESC) from migration 023.
"""
import json
from typing import List
from uuid import UUID

from src.engine.models.support_ticket_event import (
    SupportTicketActorRole,
    SupportTicketEvent,
    SupportTicketEventType,
)
from src.engine.storage.base import BaseStorage


class SupportTicketEventStorage(BaseStorage):
    """PostgreSQL append-only storage for support_ticket_events (migration 023)."""

    async def insert(self, event: SupportTicketEvent) -> SupportTicketEvent:
        row = await self.fetchrow(
            """
            INSERT INTO support_ticket_events (
                id, ticket_id, actor_user_id, actor_role,
                event_type, payload, created_at
            )
            VALUES ($1, $2, $3, $4, $5, $6::jsonb, $7)
            RETURNING *
            """,
            event.id, event.ticket_id, event.actor_user_id,
            event.actor_role.value, event.event_type.value,
            json.dumps(event.payload or {}), event.created_at,
        )
        return self._row_to_event(row)

    async def list_by_ticket(
        self, ticket_id: UUID, limit: int = 200
    ) -> List[SupportTicketEvent]:
        rows = await self.fetch(
            """
            SELECT * FROM support_ticket_events
            WHERE ticket_id = $1
            ORDER BY created_at DESC
            LIMIT $2
            """,
            ticket_id, limit,
        )
        return [self._row_to_event(r) for r in rows]

    @staticmethod
    def _row_to_event(row) -> SupportTicketEvent:
        # JSONB column: asyncpg may return dict or JSON string depending on
        # codec setup — handle both (matches task_event_storage pattern).
        payload = row["payload"]
        if isinstance(payload, str):
            payload = json.loads(payload)
        return SupportTicketEvent(
            id=row["id"],
            ticket_id=row["ticket_id"],
            actor_user_id=row["actor_user_id"],
            actor_role=SupportTicketActorRole(row["actor_role"]),
            event_type=SupportTicketEventType(row["event_type"]),
            payload=payload or {},
            created_at=row["created_at"],
        )
