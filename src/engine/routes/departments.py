"""
Department Routes

Admin-only endpoints for managing departments and visibility rules.
"""

from src.engine.unified_logger import get_logger
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from ..services.engine_service import get_engine_service
from .auth import get_current_user

logger = get_logger("routes")
router = APIRouter(prefix="/departments", tags=["departments"])

class CreateDepartmentRequest(BaseModel):
    name: str

class UpdateDepartmentRequest(BaseModel):
    name: str

class CreateVisibilityRuleRequest(BaseModel):
    department_a_id: str
    department_b_id: str

def _require_admin(current_user: dict):
    if not current_user.get("is_admin"):
        raise HTTPException(status_code=403, detail="Admin access required")

@router.post("/")
async def create_department(
    request: CreateDepartmentRequest,
    current_user: dict = Depends(get_current_user),
):
    _require_admin(current_user)
    engine = get_engine_service()
    dept = await engine.department_service.create_department(
        org_id=current_user["org_id"],
        name=request.name,
    )
    return dept.to_dict()

@router.get("/")
async def list_departments(current_user: dict = Depends(get_current_user)):
    _require_admin(current_user)
    engine = get_engine_service()
    depts = await engine.department_service.list_departments(current_user["org_id"])
    return [d.to_dict() for d in depts]

@router.get("/{department_id}")
async def get_department(
    department_id: str,
    current_user: dict = Depends(get_current_user),
):
    _require_admin(current_user)
    engine = get_engine_service()
    dept = await engine.department_service.get_department(UUID(department_id))
    if not dept or dept.org_id != current_user["org_id"]:
        raise HTTPException(status_code=404, detail="Department not found")
    return dept.to_dict()

@router.patch("/{department_id}")
async def update_department(
    department_id: str,
    request: UpdateDepartmentRequest,
    current_user: dict = Depends(get_current_user),
):
    _require_admin(current_user)
    engine = get_engine_service()
    dept = await engine.department_service.get_department(UUID(department_id))
    if not dept or dept.org_id != current_user["org_id"]:
        raise HTTPException(status_code=404, detail="Department not found")
    updated = await engine.department_service.update_department(UUID(department_id), request.name)
    return updated.to_dict()

@router.delete("/{department_id}")
async def delete_department(
    department_id: str,
    current_user: dict = Depends(get_current_user),
):
    _require_admin(current_user)
    engine = get_engine_service()
    dept = await engine.department_service.get_department(UUID(department_id))
    if not dept or dept.org_id != current_user["org_id"]:
        raise HTTPException(status_code=404, detail="Department not found")
    deleted = await engine.department_service.delete_department(UUID(department_id))
    if not deleted:
        raise HTTPException(status_code=404, detail="Department not found")
    return {"status": "deleted"}

@router.post("/{department_id}/head/{user_id}")
async def set_head(
    department_id: str,
    user_id: str,
    current_user: dict = Depends(get_current_user),
):
    _require_admin(current_user)
    engine = get_engine_service()
    dept = await engine.department_service.get_department(UUID(department_id))
    if not dept or dept.org_id != current_user["org_id"]:
        raise HTTPException(status_code=404, detail="Department not found")
    success = await engine.department_service.set_head(UUID(department_id), UUID(user_id))
    if not success:
        raise HTTPException(status_code=400, detail="User not found or not in this department")
    return {"status": "ok"}

@router.delete("/{department_id}/head")
async def clear_head(
    department_id: str,
    current_user: dict = Depends(get_current_user),
):
    _require_admin(current_user)
    engine = get_engine_service()
    dept = await engine.department_service.get_department(UUID(department_id))
    if not dept or dept.org_id != current_user["org_id"]:
        raise HTTPException(status_code=404, detail="Department not found")
    await engine.department_service.clear_head(UUID(department_id))
    return {"status": "ok"}

@router.post("/visibility")
async def create_visibility_rule(
    request: CreateVisibilityRuleRequest,
    current_user: dict = Depends(get_current_user),
):
    _require_admin(current_user)
    engine = get_engine_service()
    rule = await engine.department_service.create_visibility_rule(
        org_id=current_user["org_id"],
        dept_a_id=UUID(request.department_a_id),
        dept_b_id=UUID(request.department_b_id),
    )
    return rule.to_dict()

@router.delete("/visibility/{rule_id}")
async def delete_visibility_rule(
    rule_id: str,
    current_user: dict = Depends(get_current_user),
):
    _require_admin(current_user)
    engine = get_engine_service()
    deleted = await engine.department_service.delete_visibility_rule(UUID(rule_id))
    if not deleted:
        raise HTTPException(status_code=404, detail="Rule not found")
    return {"status": "deleted"}

@router.get("/visibility")
async def list_visibility_rules(current_user: dict = Depends(get_current_user)):
    _require_admin(current_user)
    engine = get_engine_service()
    rules = await engine.department_service.list_visibility_rules(current_user["org_id"])
    return [r.to_dict() for r in rules]
