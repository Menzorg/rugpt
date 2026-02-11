"""
Calendar Storage

PostgreSQL storage for calendar events.
"""
import json
import logging
from datetime import datetime
from typing import Optional, List
from uuid import UUID

from .base import BaseStorage
from ..models.calendar_event import CalendarEvent

logger = logging.getLogger("rugpt.storage.calendar")


class CalendarStorage(BaseStorage):
    """Storage for CalendarEvent entities"""

    async def create(self, event: CalendarEvent) -> CalendarEvent:
        """Create a new calendar event"""
        query = """
            INSERT INTO calendar_events (
                id, role_id, org_id, title, description, event_type,
                scheduled_at, cron_expression, next_trigger_at,
                last_triggered_at, trigger_count,
                source_chat_id, source_message_id,
                metadata, created_by_user_id, is_active,
                created_at, updated_at
            )
            VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16,$17,$18)
            RETURNING *
        """
        row = await self.fetchrow(
            query,
            event.id, event.role_id, event.org_id, event.title,
            event.description, event.event_type,
            event.scheduled_at, event.cron_expression, event.next_trigger_at,
            event.last_triggered_at, event.trigger_count,
            event.source_chat_id, event.source_message_id,
            json.dumps(event.metadata), event.created_by_user_id, event.is_active,
            event.created_at, event.updated_at
        )
        return self._row_to_event(row)

    async def get_by_id(self, event_id: UUID) -> Optional[CalendarEvent]:
        """Get event by ID"""
        query = "SELECT * FROM calendar_events WHERE id = $1"
        row = await self.fetchrow(query, event_id)
        return self._row_to_event(row) if row else None

    async def list_by_org(self, org_id: UUID, active_only: bool = True) -> List[CalendarEvent]:
        """List events in organization"""
        if active_only:
            query = """
                SELECT * FROM calendar_events
                WHERE org_id = $1 AND is_active = true
                ORDER BY next_trigger_at NULLS LAST
            """
        else:
            query = "SELECT * FROM calendar_events WHERE org_id = $1 ORDER BY created_at DESC"
        rows = await self.fetch(query, org_id)
        return [self._row_to_event(row) for row in rows]

    async def list_by_role(self, role_id: UUID, active_only: bool = True) -> List[CalendarEvent]:
        """List events for a specific role"""
        if active_only:
            query = """
                SELECT * FROM calendar_events
                WHERE role_id = $1 AND is_active = true
                ORDER BY next_trigger_at NULLS LAST
            """
        else:
            query = "SELECT * FROM calendar_events WHERE role_id = $1 ORDER BY created_at DESC"
        rows = await self.fetch(query, role_id)
        return [self._row_to_event(row) for row in rows]

    async def get_due_events(self, now: Optional[datetime] = None) -> List[CalendarEvent]:
        """Get events that are due for triggering"""
        if now is None:
            now = datetime.utcnow()
        query = """
            SELECT * FROM calendar_events
            WHERE is_active = true AND next_trigger_at <= $1
            ORDER BY next_trigger_at
        """
        rows = await self.fetch(query, now)
        return [self._row_to_event(row) for row in rows]

    async def update(self, event: CalendarEvent) -> CalendarEvent:
        """Update calendar event"""
        event.updated_at = datetime.utcnow()
        query = """
            UPDATE calendar_events
            SET title = $2, description = $3, event_type = $4,
                scheduled_at = $5, cron_expression = $6, next_trigger_at = $7,
                last_triggered_at = $8, trigger_count = $9,
                metadata = $10, is_active = $11, updated_at = $12
            WHERE id = $1
            RETURNING *
        """
        row = await self.fetchrow(
            query,
            event.id, event.title, event.description, event.event_type,
            event.scheduled_at, event.cron_expression, event.next_trigger_at,
            event.last_triggered_at, event.trigger_count,
            json.dumps(event.metadata), event.is_active, event.updated_at
        )
        return self._row_to_event(row)

    async def deactivate(self, event_id: UUID) -> bool:
        """Deactivate event"""
        query = """
            UPDATE calendar_events
            SET is_active = false, updated_at = $2
            WHERE id = $1
        """
        result = await self.execute(query, event_id, datetime.utcnow())
        return "UPDATE 1" in result

    def _row_to_event(self, row) -> CalendarEvent:
        """Convert database row to CalendarEvent"""
        metadata = row["metadata"] if row["metadata"] else {}
        if isinstance(metadata, str):
            metadata = json.loads(metadata)

        return CalendarEvent(
            id=row["id"],
            role_id=row["role_id"],
            org_id=row["org_id"],
            title=row["title"],
            description=row["description"],
            event_type=row["event_type"],
            scheduled_at=row["scheduled_at"],
            cron_expression=row["cron_expression"],
            next_trigger_at=row["next_trigger_at"],
            last_triggered_at=row["last_triggered_at"],
            trigger_count=row["trigger_count"],
            source_chat_id=row["source_chat_id"],
            source_message_id=row["source_message_id"],
            metadata=metadata,
            created_by_user_id=row["created_by_user_id"],
            is_active=row["is_active"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )
