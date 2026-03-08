"""
Task Reports Routes

Endpoints for evening reports:
- GET /task-reports — list reports for current manager
- GET /task-reports/{report_id} — get a specific report
"""
import logging
from typing import Optional, List
from uuid import UUID

from fastapi import APIRouter, HTTPException, Depends, Query
from pydantic import BaseModel

from ..services.engine_service import get_engine_service
from .auth import get_current_user

logger = logging.getLogger("rugpt.routes.task_reports")
router = APIRouter(prefix="/task-reports", tags=["task-reports"])


class TaskReportResponse(BaseModel):
    id: str
    org_id: str
    generated_for_user_id: str
    report_date: str
    content: str
    task_summaries: list
    created_at: str


@router.get("", response_model=List[TaskReportResponse])
async def list_reports(
    limit: int = Query(30, ge=1, le=100),
    current_user: dict = Depends(get_current_user),
):
    """List reports for the current user (manager)"""
    engine = get_engine_service()
    reports = await engine.task_report_service.list_by_user(
        current_user["user_id"], limit,
    )
    return [TaskReportResponse(**r.to_dict()) for r in reports]


@router.get("/{report_id}", response_model=TaskReportResponse)
async def get_report(report_id: str, current_user: dict = Depends(get_current_user)):
    """Get a specific report by ID"""
    engine = get_engine_service()
    try:
        report_uuid = UUID(report_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid report ID")

    report = await engine.task_report_service.get(report_uuid)
    if not report:
        raise HTTPException(status_code=404, detail="Report not found")
    if report.org_id != current_user["org_id"]:
        raise HTTPException(status_code=403, detail="Access denied")

    return TaskReportResponse(**report.to_dict())
