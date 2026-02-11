"""
Calendar Service

Business logic for calendar event management.
Handles creation, triggering, and cron-based recurrence.
"""
import logging
from datetime import datetime, timezone
from typing import Optional, List
from uuid import UUID

from croniter import croniter

from ..models.calendar_event import CalendarEvent
from ..storage.calendar_storage import CalendarStorage

logger = logging.getLogger("rugpt.services.calendar")


class CalendarService:
    """Service for calendar event operations"""

    def __init__(self, calendar_storage: CalendarStorage):
        self.storage = calendar_storage

    async def create_event(
        self,
        role_id: UUID,
        org_id: UUID,
        title: str,
        description: Optional[str] = None,
        event_type: str = "one_time",
        scheduled_at: Optional[datetime] = None,
        cron_expression: Optional[str] = None,
        source_chat_id: Optional[UUID] = None,
        source_message_id: Optional[UUID] = None,
        metadata: Optional[dict] = None,
        created_by_user_id: Optional[UUID] = None,
    ) -> CalendarEvent:
        """
        Create a calendar event.

        For one_time: scheduled_at is required, next_trigger_at = scheduled_at.
        For recurring: cron_expression is required, next_trigger_at computed via croniter.
        """
        if event_type == "one_time" and not scheduled_at:
            raise ValueError("scheduled_at is required for one_time events")
        if event_type == "recurring" and not cron_expression:
            raise ValueError("cron_expression is required for recurring events")
        if event_type == "recurring" and not croniter.is_valid(cron_expression):
            raise ValueError(f"Invalid cron expression: {cron_expression}")

        # Compute next_trigger_at
        if event_type == "one_time":
            next_trigger_at = scheduled_at
        else:
            next_trigger_at = self._compute_next_trigger(cron_expression)

        event = CalendarEvent(
            role_id=role_id,
            org_id=org_id,
            title=title,
            description=description,
            event_type=event_type,
            scheduled_at=scheduled_at,
            cron_expression=cron_expression,
            next_trigger_at=next_trigger_at,
            source_chat_id=source_chat_id,
            source_message_id=source_message_id,
            metadata=metadata or {},
            created_by_user_id=created_by_user_id,
        )

        created = await self.storage.create(event)
        logger.info(
            f"Created {event_type} event '{title}' for role {role_id}, "
            f"next_trigger_at={next_trigger_at}"
        )
        return created

    async def create_from_ai_detection(
        self,
        role_id: UUID,
        org_id: UUID,
        title: str,
        date_str: str,
        description: str = "",
        chat_id: Optional[UUID] = None,
        message_id: Optional[UUID] = None,
        user_id: Optional[UUID] = None,
    ) -> CalendarEvent:
        """
        Create event from AI tool call (when LLM detects a date in conversation).

        Parses date_str as ISO format. Falls back to metadata storage if unparseable.
        """
        scheduled_at = None
        try:
            scheduled_at = datetime.fromisoformat(date_str)
        except (ValueError, TypeError):
            logger.warning(f"Could not parse date '{date_str}', storing in metadata")

        return await self.create_event(
            role_id=role_id,
            org_id=org_id,
            title=title,
            description=description,
            event_type="one_time",
            scheduled_at=scheduled_at,
            source_chat_id=chat_id,
            source_message_id=message_id,
            metadata={"original_date_str": date_str},
            created_by_user_id=user_id,
        )

    async def get_event(self, event_id: UUID) -> Optional[CalendarEvent]:
        """Get event by ID"""
        return await self.storage.get_by_id(event_id)

    async def list_events(self, org_id: UUID, active_only: bool = True) -> List[CalendarEvent]:
        """List events for organization"""
        return await self.storage.list_by_org(org_id, active_only)

    async def list_role_events(self, role_id: UUID, active_only: bool = True) -> List[CalendarEvent]:
        """List events for a specific role"""
        return await self.storage.list_by_role(role_id, active_only)

    async def get_due_events(self) -> List[CalendarEvent]:
        """Get events that are due for triggering (next_trigger_at <= now)"""
        now = datetime.now(timezone.utc)
        return await self.storage.get_due_events(now)

    async def mark_triggered(self, event: CalendarEvent) -> CalendarEvent:
        """
        Mark event as triggered.

        - Increments trigger_count
        - Sets last_triggered_at
        - For recurring: recomputes next_trigger_at via croniter
        - For one_time: deactivates the event
        """
        event.trigger_count += 1
        event.last_triggered_at = datetime.now(timezone.utc)

        if event.event_type == "recurring" and event.cron_expression:
            event.next_trigger_at = self._compute_next_trigger(event.cron_expression)
            logger.info(
                f"Recurring event '{event.title}' triggered (#{event.trigger_count}), "
                f"next at {event.next_trigger_at}"
            )
        else:
            # one_time — deactivate
            event.is_active = False
            event.next_trigger_at = None
            logger.info(
                f"One-time event '{event.title}' triggered and deactivated"
            )

        return await self.storage.update(event)

    async def update_event(
        self,
        event_id: UUID,
        title: Optional[str] = None,
        description: Optional[str] = None,
        scheduled_at: Optional[datetime] = None,
        cron_expression: Optional[str] = None,
        metadata: Optional[dict] = None,
    ) -> Optional[CalendarEvent]:
        """Update event fields"""
        event = await self.storage.get_by_id(event_id)
        if not event:
            return None

        if title is not None:
            event.title = title
        if description is not None:
            event.description = description
        if scheduled_at is not None:
            event.scheduled_at = scheduled_at
            if event.event_type == "one_time":
                event.next_trigger_at = scheduled_at
        if cron_expression is not None:
            if not croniter.is_valid(cron_expression):
                raise ValueError(f"Invalid cron expression: {cron_expression}")
            event.cron_expression = cron_expression
            event.next_trigger_at = self._compute_next_trigger(cron_expression)
        if metadata is not None:
            event.metadata = metadata

        return await self.storage.update(event)

    async def deactivate_event(self, event_id: UUID) -> bool:
        """Deactivate an event"""
        return await self.storage.deactivate(event_id)

    @staticmethod
    def _compute_next_trigger(cron_expression: str, base_time: Optional[datetime] = None) -> datetime:
        """Compute next trigger time from cron expression"""
        base = base_time or datetime.now(timezone.utc)
        cron = croniter(cron_expression, base)
        return cron.get_next(datetime)
