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

from src.engine.unified_logger import get_logger
from datetime import datetime, timedelta, timezone
from typing import Optional, TYPE_CHECKING
from zoneinfo import ZoneInfo

from .calendar_service import CalendarService
from ..config import Config
from ..logging_context import bind_correlation_id, correlation_id_var

if TYPE_CHECKING:
    from ..agents.executor import AgentExecutor
    from ..storage.role_storage import RoleStorage
    from ..storage.user_storage import UserStorage
    from ..storage.org_storage import OrgStorage
    from ..storage.chat_storage import ChatStorage
    from ..storage.message_storage import MessageStorage
    from ..storage.agent_run_storage import AgentRunStorage
    from .task_service import TaskService
    from .task_poll_service import TaskPollService
    from .task_report_service import TaskReportService
    from .ai_service import AIService
    from .in_app_notification_service import InAppNotificationService

logger = get_logger("services")

# Per-poll cooldown between consecutive poll_initial retry attempts. Spaces
# out the 3-attempt budget so a transient outage doesn't burn it in 90s.
POLL_RETRY_COOLDOWN_MINUTES = 5

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

        # Poll-retry deps — wired post-construction by EngineService (avoids
        # circular import of AIService / ChatStorage at scheduler init time).
        # All optional: if any is missing, _retry_stuck_poll_initials is a no-op.
        self.chat_storage: Optional["ChatStorage"] = None
        self.message_storage: Optional["MessageStorage"] = None
        self.agent_run_storage: Optional["AgentRunStorage"] = None
        self.ai_service: Optional["AIService"] = None
        self.in_app_notification_service: Optional["InAppNotificationService"] = None

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
            # Per-event correlation_id so one scheduler tick fan-out stays
            # greppable in logs even though there's no upstream HTTP trace.
            token = bind_correlation_id(f"sched-evt-{event.id}")
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
            finally:
                correlation_id_var.reset(token)

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
        # Bind a fresh correlation_id for this scheduler tick so every log line
        # from jobs below inherits it — otherwise they'd land under "-".
        tick_token = bind_correlation_id(f"sched-tick-{int(datetime.now(timezone.utc).timestamp())}")
        try:
            await self._process_task_jobs_inner()
        finally:
            correlation_id_var.reset(tick_token)

    async def _process_task_jobs_inner(self):
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

        # Always: retry stuck poll_initial (chat exists but AI never greeted).
        # Bounded at 3 failures, then writes a fallback message + bell notification.
        try:
            await self._retry_stuck_poll_initials()
        except Exception as e:
            logger.error(f"Poll-initial retry job failed: {e}")

        # Time-sensitive jobs require org_storage for timezone lookup
        if not self.org_storage:
            return

        has_morning = self.task_poll_service and self.task_service
        has_evening = self.task_report_service and self.user_storage and self.task_poll_service
        has_admin_briefing = (
            self.user_storage and self.task_service and self.in_app_notification_service
        )

        if not has_morning and not has_evening and not has_admin_briefing:
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

            if has_admin_briefing and local_hour in self.morning_hours:
                await self._run_admin_briefing_for_org(org.id, org.timezone)

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

    async def _run_admin_briefing_for_org(self, org_id, tz_name: str):
        """Дневная сводка по открытым задачам, поставленным админом.

        Для каждого активного админа орги:
        - Собрать задачи, где он creator и статус != done.
        - Если есть и сводка ещё не выслана сегодня в org-local дне — создать
          in_app_notification типа 'daily_admin_briefing' со списком.

        Идемпотентность через exists_for_user_on_date_in_tz: дневной таймстамп
        в часовом поясе орги, поэтому окно morning_hours (8-10, три тика) не
        дублирует уведомление.
        """
        try:
            admins = await self.user_storage.list_admins_by_org(org_id)
        except Exception as e:
            logger.error(f"Admin briefing: failed to load admins for org {org_id}: {e}")
            return

        for admin in admins:
            try:
                already = await self.in_app_notification_service.storage.exists_for_user_on_date_in_tz(
                    user_id=admin.id,
                    type="daily_admin_briefing",
                    tz_name=tz_name,
                )
                if already:
                    continue

                tasks_with_assignee = await self.task_service.list_tasks_created_by(
                    admin.id, include_done=False,
                )
                if not tasks_with_assignee:
                    continue

                title, body = self._format_admin_briefing(tasks_with_assignee)
                await self.in_app_notification_service.create(
                    user_id=admin.id,
                    org_id=org_id,
                    type="daily_admin_briefing",
                    title=title,
                    content=body,
                )
                logger.info(
                    f"Admin briefing sent: org={org_id} admin={admin.id} "
                    f"open_tasks={len(tasks_with_assignee)}"
                )
            except Exception as e:
                logger.error(
                    f"Admin briefing failed for admin {admin.id} in org {org_id}: {e}"
                )

    @staticmethod
    def _format_admin_briefing(entries: list) -> tuple[str, str]:
        """Сформировать title и body нотификации из списка {task, assignee, ...}.

        Показываем до 10 первых задач (отсортированы по дедлайну ASC NULLS LAST
        в list_by_creator_with_assignee). Если задач больше — добавляем
        "и ещё N".
        """
        total = len(entries)
        title = f"Открытые задачи: {total}"

        max_lines = 10
        lines: list[str] = []
        for entry in entries[:max_lines]:
            task = entry.get("task")
            assignee = entry.get("assignee")
            assignee_name = assignee["name"] if assignee else "—"

            task_title = getattr(task, "title", "") or "(без названия)"
            deadline = getattr(task, "deadline", None)
            if deadline is not None:
                deadline_str = deadline.strftime("%d.%m")
            else:
                deadline_str = "без срока"

            lines.append(f"• «{task_title}» — {assignee_name}, до {deadline_str}")

        if total > max_lines:
            lines.append(f"… и ещё {total - max_lines}")

        body = "\n".join(lines)
        return title, body

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
                    report_exists = await self.task_report_service.storage.exists_for_user_on_date(
                        org_id=org_id,
                        generated_for_user_id=admin.id,
                        report_date=today,
                    )
                    if report_exists:
                        logger.debug(
                            f"Evening report already exists for admin {admin.id} "
                            f"in org {org_id} on {today}"
                        )
                        continue

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

    # ── Poll-initial retry job ────────────────────────────────────

    async def _retry_stuck_poll_initials(self) -> None:
        """For each pending poll where chat exists and has 0 messages:
        if failed agent_runs < 3 AND last failure is older than the cooldown,
        republish poll_initial via Kafka. If >= 3, persist a template fallback
        message + in-app notification and stop retrying (the fallback message
        itself bumps msg_count > 0, so subsequent ticks skip — sentinel-effect).

        Cooldown: a transient LLM/Kafka outage can otherwise burn all 3 retries
        in 90 seconds (3 ticks × 30s). The per-poll cooldown ensures retries
        are spaced out so the budget covers ~POLL_RETRY_COOLDOWN × 3 of outage.
        """
        # Lazy/local imports to avoid circular dependency at module load.
        from ..services.ai_service import POLL_INTERVIEWER_ROLE_CODE
        from ..models.message import Message, SenderType

        # Graceful degradation: if any dep is missing (tests, partial wiring,
        # KAFKA_ENABLED=false-style bare engines), just bail without raising.
        if not (
            self.task_poll_service
            and getattr(self.task_poll_service, "storage", None)
            and self.chat_storage
            and self.message_storage
            and self.agent_run_storage
            and self.ai_service
            and self.user_storage
            and self.in_app_notification_service
        ):
            return

        try:
            pending_polls = await self.task_poll_service.storage.list_pending_today()
        except Exception as e:
            logger.error(
                f"_retry_stuck_poll_initials: list_pending_today failed: {e}",
                exc_info=True,
            )
            return

        if not pending_polls:
            return

        # poll_interviewer_ai is invariant across the org's lifetime — resolve
        # once per tick, not per poll.
        try:
            interviewer = await self.user_storage.get_by_username(
                "poll_interviewer_ai",
                Config.SYSTEM_ORG_ID,
            )
        except Exception as e:
            logger.error(
                f"_retry_stuck_poll_initials: get_by_username failed: {e}",
                exc_info=True,
            )
            return

        if interviewer is None:
            logger.error(
                "_retry_stuck_poll_initials: poll_interviewer_ai not found "
                "in system org — cannot retry or write fallback for any poll"
            )
            return

        now = datetime.utcnow()
        cooldown = timedelta(minutes=POLL_RETRY_COOLDOWN_MINUTES)

        for poll in pending_polls:
            try:
                chat = await self.chat_storage.get_by_poll_id(poll.id)
                if chat is None:
                    # Poll has no chat — Task 10 race-mitigation owns this case;
                    # the retry job intentionally does not create chats.
                    continue

                msg_count = await self.message_storage.count_by_chat(chat.id)
                if msg_count > 0:
                    # AI already greeted (or fallback already written) — done.
                    continue

                # role_code is the discriminator for poll_initial agent runs
                # (no separate `kind` column in agent_runs).
                failed_count = await self.agent_run_storage.count_failed_by_chat_and_kind(
                    chat.id, kind=POLL_INTERVIEWER_ROLE_CODE,
                )

                if failed_count >= 3:
                    fallback_text = (
                        "Здравствуйте! Произошла ошибка инициализации опроса. "
                        "Расскажите про свои активные задачи самостоятельно."
                    )
                    await self.message_storage.create(Message(
                        chat_id=chat.id,
                        sender_id=interviewer.id,
                        sender_type=SenderType.AI_ROLE,
                        content=fallback_text,
                        # ai_is_valid=True: fallback is operator-blessed (not pending review).
                        ai_is_valid=True,
                    ))
                    await self.in_app_notification_service.create(
                        user_id=poll.assignee_user_id,
                        org_id=poll.org_id,
                        type="system",
                        title="AI временно недоступен",
                        content="Сводка опроса будет собрана из вашего диалога.",
                        reference_type="task_poll",
                        reference_id=poll.id,
                    )
                    logger.warning(
                        f"_retry_stuck_poll_initials: poll {poll.id} reached max "
                        f"retries (3); wrote fallback message + bell notification"
                    )
                    continue

                # Cooldown gate: if we have a recent failed agent_run,
                # don't burn the next attempt yet — wait out the outage window.
                if failed_count > 0:
                    last_failed = await self.agent_run_storage.last_failed_at(
                        chat.id, kind=POLL_INTERVIEWER_ROLE_CODE,
                    )
                    if last_failed is not None and (now - last_failed) < cooldown:
                        # Logged at DEBUG to avoid log noise — this is normal
                        # cooldown behavior.
                        logger.debug(
                            f"_retry_stuck_poll_initials: poll={poll.id} chat={chat.id} "
                            f"in cooldown (last fail {now - last_failed} ago, "
                            f"cooldown {cooldown}) — skipping"
                        )
                        continue

                # Re-enqueue with new request_id (best-effort; Kafka may be down)
                await self.ai_service.enqueue_poll_initial(
                    poll.id, chat.id, interviewer.id,
                )
                logger.info(
                    f"_retry_stuck_poll_initials: republished poll_initial "
                    f"poll={poll.id} chat={chat.id} (attempt {failed_count + 1}/3)"
                )
            except Exception as e:
                logger.error(
                    f"_retry_stuck_poll_initials: poll {poll.id}: {e}",
                    exc_info=True,
                )

    @property
    def is_running(self) -> bool:
        return self._running
