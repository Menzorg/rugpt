"""
Scheduler Service

Background asyncio task that:
1. Polls calendar_events for due triggers (proactive agent → notification)
2. Runs task jobs: morning polls, overdue checks, poll expiry, evening reports

Time-sensitive jobs (morning polls, evening reports) use each organization's
timezone from the organizations table (IANA, e.g. "Europe/Moscow").
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Optional, TYPE_CHECKING
from zoneinfo import ZoneInfo

from .calendar_service import CalendarService

if TYPE_CHECKING:
    from ..agents.executor import AgentExecutor
    from ..storage.role_storage import RoleStorage
    from ..storage.user_storage import UserStorage
    from ..storage.org_storage import OrgStorage
    from .task_service import TaskService
    from .task_poll_service import TaskPollService
    from .task_report_service import TaskReportService

logger = logging.getLogger("rugpt.services.scheduler")


class SchedulerService:
    """
    Background scheduler.

    Calendar events:
        Polls get_due_events() → agent_executor → notification

    Task jobs (idempotent, safe to call every cycle):
        Morning (org-local hours 8-10): create daily polls for users with active tasks
        Always: check overdue tasks, expire stale polls
        Evening (org-local hours 18-20): generate reports for admins
    """

    def __init__(
        self,
        calendar_service: CalendarService,
        notification_service=None,
        agent_executor: Optional[AgentExecutor] = None,
        role_storage: Optional[RoleStorage] = None,
        user_storage: Optional[UserStorage] = None,
        org_storage: Optional[OrgStorage] = None,
        task_service: Optional[TaskService] = None,
        task_poll_service: Optional[TaskPollService] = None,
        task_report_service: Optional[TaskReportService] = None,
        poll_interval: int = 30,
        enabled: bool = True,
        morning_hours: tuple = (8, 9, 10),
        evening_hours: tuple = (18, 19, 20),
    ):
        self.calendar_service = calendar_service
        self.notification_service = notification_service
        self.agent_executor = agent_executor
        self.role_storage = role_storage
        self.user_storage = user_storage
        self.org_storage = org_storage
        self.task_service = task_service
        self.task_poll_service = task_poll_service
        self.task_report_service = task_report_service
        self.poll_interval = poll_interval
        self.enabled = enabled
        self.morning_hours = morning_hours
        self.evening_hours = evening_hours
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
                logger.error(f"Scheduler calendar poll error: {e}")

            try:
                await self._process_task_jobs()
            except Exception as e:
                logger.error(f"Scheduler task jobs error: {e}")

            try:
                await asyncio.sleep(self.poll_interval)
            except asyncio.CancelledError:
                break

    # ── Calendar events ───────────────────────────────────────────

    async def _process_due_events(self):
        """Check for and process due calendar events"""
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
        user_id = event.created_by_user_id — the person who created the event.
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
                user_id=event.created_by_user_id,
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

    # ── Task jobs ─────────────────────────────────────────────────

    async def _process_task_jobs(self):
        """
        Run task-related jobs.

        Timezone-independent (always run):
        - overdue check, expire stale polls

        Timezone-dependent (per-org local hour):
        - morning polls, evening reports
        """
        # Always: check overdue tasks
        if self.task_service:
            try:
                overdue = await self.task_service.check_overdue()
                if overdue:
                    logger.info(f"Marked {len(overdue)} task(s) as overdue")
            except Exception as e:
                logger.error(f"Overdue check failed: {e}")

        # Always: expire stale polls
        if self.task_poll_service:
            try:
                expired = await self.task_poll_service.expire_stale_polls()
                if expired:
                    logger.info(f"Expired {len(expired)} stale poll(s)")
            except Exception as e:
                logger.error(f"Poll expiry failed: {e}")

        # Time-sensitive jobs require org_storage for timezone lookup
        if not self.org_storage:
            return

        has_morning = self.task_poll_service and self.task_service
        has_evening = self.task_report_service and self.user_storage and self.task_poll_service

        if not has_morning and not has_evening:
            return

        # Load active organizations and check local hour for each
        try:
            orgs = await self.org_storage.list_all(active_only=True)
        except Exception as e:
            logger.error(f"Failed to load organizations: {e}")
            return

        now_utc = datetime.now(timezone.utc)

        for org in orgs:
            try:
                local_hour = self._get_org_local_hour(org.timezone, now_utc)
            except Exception as e:
                logger.warning(f"Bad timezone '{org.timezone}' for org {org.id}: {e}")
                continue

            if has_morning and local_hour in self.morning_hours:
                await self._run_morning_polls_for_org(org.id)

            if has_evening and local_hour in self.evening_hours:
                await self._run_evening_reports_for_org(org.id, now_utc)

    @staticmethod
    def _get_org_local_hour(tz_name: str, now_utc: datetime) -> int:
        """Convert UTC time to the organization's local hour."""
        tz = ZoneInfo(tz_name)
        return now_utc.astimezone(tz).hour

    async def _run_morning_polls_for_org(self, org_id):
        """
        Create daily polls for employees in this org who have active tasks.

        Idempotent: create_daily_poll() checks if poll already exists for today.
        """
        try:
            assignees = await self.task_service.storage.list_distinct_assignees()
            # Filter to this org
            org_assignees = [
                (uid, oid) for uid, oid in assignees if oid == org_id
            ]
            if not org_assignees:
                return

            created_count = 0
            for assignee_user_id, _ in org_assignees:
                try:
                    poll = await self.task_poll_service.create_daily_poll(
                        assignee_user_id=assignee_user_id,
                        org_id=org_id,
                    )
                    if poll:
                        created_count += 1
                except Exception as e:
                    logger.error(
                        f"Failed to create poll for user {assignee_user_id}: {e}"
                    )

            if created_count:
                logger.info(
                    f"Morning polls for org {org_id}: created {created_count} poll(s)"
                )

        except Exception as e:
            logger.error(f"Morning polls for org {org_id} failed: {e}")

    async def _run_evening_reports_for_org(self, org_id, now_utc: datetime):
        """
        Generate evening reports for admins in this org.

        Uses org timezone for the report date (today in org's local time).
        """
        try:
            org = await self.org_storage.get_by_id(org_id)
            if not org:
                return

            tz = ZoneInfo(org.timezone)
            today = now_utc.astimezone(tz).date()

            polls = await self.task_poll_service.list_by_org_and_date(org_id, today)
            if not polls:
                return

            admins = await self.user_storage.list_admins_by_org(org_id)
            for admin in admins:
                try:
                    await self.task_report_service.generate_report(
                        org_id=org_id,
                        manager_user_id=admin.id,
                        report_date=today,
                        user_storage=self.user_storage,
                    )
                except Exception as e:
                    logger.error(
                        f"Failed to generate report for admin {admin.id} "
                        f"in org {org_id}: {e}"
                    )

        except Exception as e:
            logger.error(f"Evening reports for org {org_id} failed: {e}")

    @property
    def is_running(self) -> bool:
        return self._running
