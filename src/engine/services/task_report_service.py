"""
Task Report Service

Business logic for AI-generated evening reports.
Aggregates poll data, generates report text, notifies managers.
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


class TaskReportService:

    def __init__(
        self,
        storage: TaskReportStorage,
        task_poll_service: TaskPollService,
        in_app_notification_service: InAppNotificationService,
    ):
        self.storage = storage
        self.poll_service = task_poll_service
        self.notification_service = in_app_notification_service

    async def get(self, report_id: UUID) -> Optional[TaskReport]:
        """Get report by ID"""
        return await self.storage.get_by_id(report_id)

    async def list_by_user(self, user_id: UUID, limit: int = 30) -> List[TaskReport]:
        """List reports for a manager"""
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
        Aggregates today's poll responses into a summary.
        Called by scheduler (evening_report_job).

        For now generates a structured text report.
        In the future, AI will generate natural language summary via agent_executor.
        """
        # Get all polls for the org today
        polls = await self.poll_service.list_by_org_and_date(org_id, report_date)
        if not polls:
            logger.info(f"No polls for org {org_id} on {report_date}, skipping report")
            return None

        # Build task summaries
        task_summaries = []
        completed_polls = 0
        expired_polls = 0

        for poll in polls:
            assignee_name = str(poll.assignee_user_id)
            # Resolve username if user_storage provided
            if user_storage:
                user = await user_storage.get_by_id(poll.assignee_user_id)
                if user:
                    assignee_name = user.name or user.username

            if poll.status == "completed":
                completed_polls += 1
                for resp in poll.responses:
                    task_summaries.append({
                        "task_id": resp.get("task_id"),
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

        # Generate text content (plain text for now, AI-generated in future)
        lines = [f"Отчёт за {report_date.isoformat()}", ""]
        lines.append(f"Опросов завершено: {completed_polls} из {len(polls)}")
        if expired_polls:
            lines.append(f"Опросов просрочено: {expired_polls}")
        lines.append("")

        for summary in task_summaries:
            name = summary.get("assignee_name", "?")
            if summary.get("poll_completed"):
                status = summary.get("new_status", "—")
                comment = summary.get("employee_comment", "")
                lines.append(f"  {name}: {status}" + (f" — {comment}" if comment else ""))
            else:
                lines.append(f"  {name}: опрос не пройден")

        content = "\n".join(lines)

        # Save report
        report = TaskReport(
            org_id=org_id,
            generated_for_user_id=manager_user_id,
            report_date=report_date,
            content=content,
            task_summaries=task_summaries,
        )
        created = await self.storage.create(report)
        logger.info(f"Generated report for manager {manager_user_id} on {report_date}")

        # Notify manager via bell
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
