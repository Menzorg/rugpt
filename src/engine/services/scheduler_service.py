"""
Scheduler Service

Background asyncio task that polls calendar_events for due triggers.
Triggers proactive agent execution and sends notifications.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Optional, TYPE_CHECKING

from .calendar_service import CalendarService

if TYPE_CHECKING:
    from ..agents.executor import AgentExecutor
    from ..storage.role_storage import RoleStorage

logger = logging.getLogger("rugpt.services.scheduler")


class SchedulerService:
    """
    Background scheduler for calendar events.

    Polls the database every poll_interval seconds for events
    where next_trigger_at <= NOW() and is_active = true.

    On trigger:
    1. Marks event as triggered (recompute next_trigger or deactivate)
    2. Loads the event's role, calls agent_executor with event context
    3. Sends the agent response (or fallback text) as notification
    """

    def __init__(
        self,
        calendar_service: CalendarService,
        notification_service=None,
        agent_executor: Optional[AgentExecutor] = None,
        role_storage: Optional[RoleStorage] = None,
        poll_interval: int = 30,
        enabled: bool = True,
    ):
        self.calendar_service = calendar_service
        self.notification_service = notification_service
        self.agent_executor = agent_executor
        self.role_storage = role_storage
        self.poll_interval = poll_interval
        self.enabled = enabled
        self._task: Optional[asyncio.Task] = None
        self._running = False

    async def start(self):
        """Start the scheduler background task"""
        if not self.enabled:
            logger.info("Scheduler is disabled (SCHEDULER_ENABLED=false)")
            return

        if self._running:
            logger.warning("Scheduler is already running")
            return

        self._running = True
        self._task = asyncio.create_task(self._poll_loop())
        logger.info(f"Scheduler started (poll_interval={self.poll_interval}s)")

    async def stop(self):
        """Stop the scheduler background task"""
        self._running = False
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        self._task = None
        logger.info("Scheduler stopped")

    async def _poll_loop(self):
        """Main polling loop"""
        while self._running:
            try:
                await self._process_due_events()
            except Exception as e:
                logger.error(f"Scheduler poll error: {e}")

            try:
                await asyncio.sleep(self.poll_interval)
            except asyncio.CancelledError:
                break

    async def _process_due_events(self):
        """Check for and process due events"""
        due_events = await self.calendar_service.get_due_events()

        if not due_events:
            return

        logger.info(f"Scheduler found {len(due_events)} due event(s)")

        for event in due_events:
            try:
                await self.calendar_service.mark_triggered(event)
                logger.info(
                    f"Triggered event: '{event.title}' "
                    f"(type={event.event_type}, role_id={event.role_id})"
                )

                # Build notification content (proactive agent or fallback)
                content = await self._build_notification_content(event)

                # Send notification to event creator
                await self._notify_event(event, content)

            except Exception as e:
                logger.error(f"Failed to process event {event.id}: {e}")

    async def _build_notification_content(self, event) -> str:
        """
        Build notification content for a triggered event.

        If agent_executor and role_storage are available, runs the role's agent
        with the event as context (proactive execution).
        Otherwise, returns a simple reminder text.
        """
        fallback = f"Reminder: {event.title}"
        if event.description:
            fallback += f"\n{event.description}"

        if not self.agent_executor or not self.role_storage:
            return fallback

        try:
            role = await self.role_storage.get_by_id(event.role_id)
            if not role:
                logger.warning(f"Role {event.role_id} not found for event {event.id}")
                return fallback

            # Build a proactive context message for the agent
            proactive_message = (
                f"Сработало календарное событие: \"{event.title}\"."
            )
            if event.description:
                proactive_message += f"\nОписание: {event.description}"
            proactive_message += (
                "\nПодготовь краткое уведомление для пользователя по этому событию."
            )

            messages = [{"role": "user", "content": proactive_message}]

            result = await self.agent_executor.execute(
                role=role,
                messages=messages,
                temperature=0.5,
                max_tokens=512,
            )

            if result.error:
                logger.warning(
                    f"Agent error for proactive event {event.id}: {result.error}"
                )
                return fallback

            if result.content:
                logger.info(
                    f"Proactive agent response for event '{event.title}': "
                    f"{len(result.content)} chars"
                )
                return result.content

        except Exception as e:
            logger.error(f"Proactive agent execution failed for event {event.id}: {e}")

        return fallback

    async def _notify_event(self, event, content: str):
        """Send notification for a triggered event"""
        if not self.notification_service:
            return

        if not event.created_by_user_id:
            logger.debug(f"Event {event.id} has no creator, skipping notification")
            return

        try:
            sent = await self.notification_service.send_notification(
                user_id=event.created_by_user_id,
                content=content,
                event_id=event.id,
                role_id=event.role_id,
            )
            if sent:
                logger.info(f"Notification sent for event '{event.title}'")
            else:
                logger.warning(
                    f"No channels delivered for event '{event.title}' "
                    f"(user={event.created_by_user_id})"
                )
        except Exception as e:
            logger.error(f"Failed to notify for event {event.id}: {e}")

    @property
    def is_running(self) -> bool:
        return self._running
