"""
Task Report Service

Business logic for AI-generated evening reports.
Aggregates poll data, asks LLM (role=report_generator) to write a summary,
falls back to deterministic plain text on LLM failure.
"""
import logging
from datetime import date
from typing import Optional, List
from uuid import UUID

from ..models.task_report import TaskReport
from ..storage.task_report_storage import TaskReportStorage
from .task_poll_service import TaskPollService
from .in_app_notification_service import InAppNotificationService

logger = logging.getLogger("rugpt.services.task_report")

REPORT_GENERATOR_ROLE_CODE = "report_generator"
RUGPT_SYSTEM_ORG_ID = UUID("00000000-0000-0000-0000-000000000000")


class TaskReportService:

    def __init__(
        self,
        storage: TaskReportStorage,
        task_poll_service: TaskPollService,
        in_app_notification_service: InAppNotificationService,
        role_storage=None,
        task_storage=None,
        user_storage=None,
        agent_executor=None,
    ):
        self.storage = storage
        self.poll_service = task_poll_service
        self.notification_service = in_app_notification_service
        self.role_storage = role_storage
        self.task_storage = task_storage
        self.user_storage = user_storage
        # Wired in by EngineService after AgentExecutor is constructed.
        self.agent_executor = agent_executor

    async def get(self, report_id: UUID) -> Optional[TaskReport]:
        return await self.storage.get_by_id(report_id)

    async def list_by_user(self, user_id: UUID, limit: int = 30) -> List[TaskReport]:
        return await self.storage.list_by_user(user_id, limit)

    async def generate_report(
        self,
        org_id: UUID,
        manager_user_id: UUID,
        report_date: date,
        user_storage=None,
    ) -> Optional[TaskReport]:
        """
        Generate an evening report for a manager.
        Aggregates today's poll responses, asks LLM to summarize.
        Called by scheduler (evening_report_job).
        """
        # Allow caller to override user_storage; fall back to one from constructor.
        ustore = user_storage or self.user_storage

        polls = await self.poll_service.list_by_org_and_date(org_id, report_date)
        if not polls:
            logger.info(f"No polls for org {org_id} on {report_date}, skipping report")
            return None

        # Build task summaries (raw data — saved as-is, also fed into LLM prompt).
        # Resolve assignee names + task titles in advance for richer prompt.
        task_summaries = []
        completed_polls = 0
        expired_polls = 0

        # Collect unique task ids referenced by responses to bulk-fetch titles.
        task_ids: set[UUID] = set()
        for poll in polls:
            if poll.status == "completed":
                for resp in poll.responses:
                    tid = resp.get("task_id")
                    if tid:
                        try:
                            task_ids.add(UUID(tid) if isinstance(tid, str) else tid)
                        except (ValueError, TypeError):
                            pass

        title_by_id: dict[UUID, str] = {}
        if task_ids and self.task_storage is not None:
            try:
                tasks_map = await self.task_storage.get_many_by_ids(list(task_ids))
                title_by_id = {tid: t.title for tid, t in tasks_map.items()}
            except Exception as e:
                logger.warning(f"Failed to bulk-fetch task titles: {e}")

        for poll in polls:
            assignee_name = str(poll.assignee_user_id)
            if ustore:
                user = await ustore.get_by_id(poll.assignee_user_id)
                if user:
                    assignee_name = user.name or user.username

            if poll.status == "completed":
                completed_polls += 1
                for resp in poll.responses:
                    tid_raw = resp.get("task_id")
                    tid_uuid = None
                    if tid_raw:
                        try:
                            tid_uuid = UUID(tid_raw) if isinstance(tid_raw, str) else tid_raw
                        except (ValueError, TypeError):
                            pass
                    task_summaries.append({
                        "task_id": resp.get("task_id"),
                        "task_title": title_by_id.get(tid_uuid) if tid_uuid else None,
                        "assignee_user_id": str(poll.assignee_user_id),
                        "assignee_name": assignee_name,
                        "new_status": resp.get("new_status"),
                        "employee_comment": resp.get("comment"),
                        "poll_completed": True,
                    })
            elif poll.status == "expired":
                expired_polls += 1
                task_summaries.append({
                    "assignee_user_id": str(poll.assignee_user_id),
                    "assignee_name": assignee_name,
                    "poll_completed": False,
                })

        # Try LLM-driven summary, fall back to deterministic plain text on any error.
        content = await self._generate_ai_content(
            report_date=report_date,
            task_summaries=task_summaries,
            completed_polls=completed_polls,
            total_polls=len(polls),
            expired_polls=expired_polls,
            manager_user_id=manager_user_id,
        )
        if content is None:
            content = self._fallback_plain_text(
                report_date=report_date,
                task_summaries=task_summaries,
                completed_polls=completed_polls,
                total_polls=len(polls),
                expired_polls=expired_polls,
            )

        report = TaskReport(
            org_id=org_id,
            generated_for_user_id=manager_user_id,
            report_date=report_date,
            content=content,
            task_summaries=task_summaries,
        )
        created = await self.storage.create(report)
        logger.info(f"Generated report for manager {manager_user_id} on {report_date}")

        await self.notification_service.create(
            user_id=manager_user_id,
            org_id=org_id,
            type="report",
            title=f"Вечерний отчёт за {report_date.isoformat()}",
            content=f"Опросов завершено: {completed_polls} из {len(polls)}",
            reference_type="task_report",
            reference_id=created.id,
        )

        return created

    async def _generate_ai_content(
        self,
        report_date: date,
        task_summaries: list,
        completed_polls: int,
        total_polls: int,
        expired_polls: int,
        manager_user_id: UUID,
    ) -> Optional[str]:
        """Call LLM via AgentExecutor; return None on any failure (caller falls back)."""
        if self.agent_executor is None or self.role_storage is None:
            logger.error(
                "TaskReportService: agent_executor or role_storage not wired, "
                "cannot generate AI report (manager=%s, date=%s)",
                manager_user_id, report_date,
            )
            return None

        try:
            role = await self.role_storage.get_by_code(
                REPORT_GENERATOR_ROLE_CODE, RUGPT_SYSTEM_ORG_ID
            )
            if role is None:
                logger.error(
                    "TaskReportService: role '%s' not found in system org — "
                    "did migration 028 run? (manager=%s, date=%s)",
                    REPORT_GENERATOR_ROLE_CODE, manager_user_id, report_date,
                )
                return None

            user_input = self._build_llm_input(
                report_date=report_date,
                task_summaries=task_summaries,
                completed_polls=completed_polls,
                total_polls=total_polls,
                expired_polls=expired_polls,
            )

            result = await self.agent_executor.execute(
                role=role,
                messages=[{"role": "user", "content": user_input}],
                temperature=0.3,
                max_tokens=2048,
                user_id=manager_user_id,
            )
            content = (result.content or "").strip() if result else ""
            if not content:
                logger.error(
                    "TaskReportService: AI returned empty content "
                    "(manager=%s, date=%s)",
                    manager_user_id, report_date,
                )
                return None
            return content
        except Exception as e:
            logger.error(
                "TaskReportService: LLM call failed (manager=%s, date=%s): %s",
                manager_user_id, report_date, e,
                exc_info=True,
            )
            return None

    @staticmethod
    def _build_llm_input(
        report_date: date,
        task_summaries: list,
        completed_polls: int,
        total_polls: int,
        expired_polls: int,
    ) -> str:
        lines = [
            f"Дата отчёта: {report_date.isoformat()}",
            f"Опросов завершено: {completed_polls} из {total_polls}",
        ]
        if expired_polls:
            lines.append(f"Опросов просрочено: {expired_polls}")
        lines.append("")
        lines.append("Ответы сотрудников:")

        for s in task_summaries:
            name = s.get("assignee_name") or "—"
            if s.get("poll_completed"):
                title = s.get("task_title") or s.get("task_id") or "—"
                status = s.get("new_status") or "—"
                comment = s.get("employee_comment") or ""
                line = f"- {name} | задача: {title} | статус: {status}"
                if comment:
                    line += f" | комментарий: {comment}"
                lines.append(line)
            else:
                lines.append(f"- {name} | опрос не пройден")
        return "\n".join(lines)

    @staticmethod
    def _fallback_plain_text(
        report_date: date,
        task_summaries: list,
        completed_polls: int,
        total_polls: int,
        expired_polls: int,
    ) -> str:
        """Deterministic plain text. Used when LLM is unavailable or failed."""
        lines = [f"Отчёт за {report_date.isoformat()}", ""]
        lines.append(f"Опросов завершено: {completed_polls} из {total_polls}")
        if expired_polls:
            lines.append(f"Опросов просрочено: {expired_polls}")
        lines.append("")

        for s in task_summaries:
            name = s.get("assignee_name", "?")
            if s.get("poll_completed"):
                title = s.get("task_title")
                status = s.get("new_status", "—")
                comment = s.get("employee_comment", "")
                if title:
                    line = f"  {name} — «{title}»: {status}"
                else:
                    line = f"  {name}: {status}"
                if comment:
                    line += f" — {comment}"
                lines.append(line)
            else:
                lines.append(f"  {name}: опрос не пройден")
        return "\n".join(lines)
