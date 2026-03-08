"""
Task Report Storage

PostgreSQL CRUD for task_reports table.
"""
import json
import logging
from datetime import date
from typing import Optional, List
from uuid import UUID

from .base import BaseStorage
from ..models.task_report import TaskReport

logger = logging.getLogger("rugpt.storage.task_report")


class TaskReportStorage(BaseStorage):

    async def create(self, report: TaskReport) -> TaskReport:
        """Create a new task report"""
        query = """
            INSERT INTO task_reports
                (id, org_id, generated_for_user_id,
                 report_date, content, task_summaries, created_at)
            VALUES ($1, $2, $3, $4, $5, $6, $7)
            RETURNING *
        """
        row = await self.fetchrow(
            query,
            report.id, report.org_id,
            report.generated_for_user_id,
            report.report_date, report.content,
            json.dumps(report.task_summaries),
            report.created_at,
        )
        return self._row_to_report(row)

    async def get_by_id(self, report_id: UUID) -> Optional[TaskReport]:
        """Get report by ID"""
        row = await self.fetchrow(
            "SELECT * FROM task_reports WHERE id = $1",
            report_id,
        )
        return self._row_to_report(row) if row else None

    async def list_by_user(
        self,
        generated_for_user_id: UUID,
        limit: int = 30,
    ) -> List[TaskReport]:
        """List reports for a manager, newest first"""
        query = """
            SELECT * FROM task_reports
            WHERE generated_for_user_id = $1
            ORDER BY report_date DESC
            LIMIT $2
        """
        rows = await self.fetch(query, generated_for_user_id, limit)
        return [self._row_to_report(r) for r in rows]

    async def list_by_org_and_date(
        self,
        org_id: UUID,
        report_date: date,
    ) -> List[TaskReport]:
        """List all reports for an org on a specific date"""
        query = """
            SELECT * FROM task_reports
            WHERE org_id = $1 AND report_date = $2
            ORDER BY created_at DESC
        """
        rows = await self.fetch(query, org_id, report_date)
        return [self._row_to_report(r) for r in rows]

    def _row_to_report(self, row) -> TaskReport:
        """Map asyncpg Record to TaskReport"""
        summaries = row["task_summaries"]
        if isinstance(summaries, str):
            summaries = json.loads(summaries)

        return TaskReport(
            id=row["id"],
            org_id=row["org_id"],
            generated_for_user_id=row["generated_for_user_id"],
            report_date=row["report_date"],
            content=row["content"],
            task_summaries=summaries if summaries else [],
            created_at=row["created_at"],
        )
