"""
Corrections Routes

CRUD endpoints for correction rules — admin-created records that link an AI
role's incorrect response to a user correction and an extracted lesson.
"""
import logging
from typing import List, Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException

from pydantic import BaseModel

from .auth import get_current_user
from ..services.engine_service import get_engine_service, EngineService

logger = logging.getLogger("rugpt.routes.corrections")
router = APIRouter(prefix="/corrections", tags=["corrections"])


# ============================================
# Request / Response Models
# ============================================

class CorrectionRuleResponse(BaseModel):
    id: str
    role_id: str
    src_ai_response_id: str
    user_correction_text: str
    extracted_lesson: str


class CreateCorrectionRequest(BaseModel):
    """Admin provides the corrected AI message and their correction text.
    extracted_lesson is derived automatically — not accepted from the client."""
    corrected_message_id: str   # src_ai_response_id
    user_correction_text: str


class UpdateCorrectionRequest(BaseModel):
    """Only the human-supplied fields may be changed after creation."""
    corrected_message_id: str
    user_correction_text: str


# ============================================
# Helpers
# ============================================

def _require_admin(current_user: dict) -> None:
    if not current_user["is_admin"]:
        raise HTTPException(status_code=403, detail="Admin access required")


def _get_engine() -> EngineService:
    return get_engine_service()


def _to_response(rule) -> CorrectionRuleResponse:
    return CorrectionRuleResponse(
        id=str(rule.id),
        role_id=str(rule.role_id),
        src_ai_response_id=str(rule.src_ai_response_id) if rule.src_ai_response_id else "",
        user_correction_text=rule.user_correction_text or "",
        extracted_lesson=rule.extracted_lesson or "",
    )


# ============================================
# Routes
# ============================================

@router.get("", response_model=List[CorrectionRuleResponse])
@router.get("/", response_model=List[CorrectionRuleResponse])
async def list_corrections(
    role_id: Optional[str] = None,
    current_user: dict = Depends(get_current_user),
    engine: EngineService = Depends(_get_engine),
):
    """List correction rules, optionally filtered by role_id. Admin only."""
    _require_admin(current_user)
    if role_id is not None:
        try:
            rid = UUID(role_id)
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid role_id")
        rules = await engine.correction_rule_service.get_rules_for_role(rid)
    else:
        rules = await engine.correction_rule_storage.list_active()
    return [_to_response(r) for r in rules]


@router.get("/{correction_id}", response_model=CorrectionRuleResponse)
async def get_correction(
    correction_id: str,
    current_user: dict = Depends(get_current_user),
    engine: EngineService = Depends(_get_engine),
):
    """Get a correction rule by ID. Admin only."""
    _require_admin(current_user)
    try:
        cid = UUID(correction_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid correction_id")
    rule = await engine.correction_rule_service.get_rule(cid)
    if not rule:
        raise HTTPException(status_code=404, detail="Correction rule not found")
    return _to_response(rule)


@router.post("", response_model=CorrectionRuleResponse, status_code=201)
@router.post("/", response_model=CorrectionRuleResponse, status_code=201)
async def create_correction(
    body: CreateCorrectionRequest,
    current_user: dict = Depends(get_current_user),
    engine: EngineService = Depends(_get_engine),
):
    """Create a correction rule from an AI message ID and admin correction text. Admin only."""
    _require_admin(current_user)
    try:
        ai_message_id = UUID(body.corrected_message_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid corrected_message_id")
    user_id = UUID(current_user["user_id"])
    try:
        rule = await engine.correction_rule_service.reject_and_create_rule(
            ai_message_id=ai_message_id,
            user_id=user_id,
            correction_text=body.user_correction_text,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return _to_response(rule)


@router.patch("/{correction_id}", response_model=CorrectionRuleResponse)
async def update_correction(
    correction_id: str,
    body: UpdateCorrectionRequest,
    current_user: dict = Depends(get_current_user),
    engine: EngineService = Depends(_get_engine),
):
    """Update the corrected message or correction text on an existing rule. Admin only."""
    _require_admin(current_user)
    try:
        cid = UUID(correction_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid correction_id")
    try:
        ai_message_id = UUID(body.corrected_message_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid corrected_message_id")
    rule = await engine.correction_rule_service.update_rule(
        rule_id=cid,
        src_ai_response_id=ai_message_id,
        user_correction_text=body.user_correction_text,
    )
    if not rule:
        raise HTTPException(status_code=404, detail="Correction rule not found")
    return _to_response(rule)


@router.delete("/{correction_id}", status_code=204)
async def delete_correction(
    correction_id: str,
    current_user: dict = Depends(get_current_user),
    engine: EngineService = Depends(_get_engine),
):
    """Deactivate a correction rule. Admin only."""
    _require_admin(current_user)
    try:
        cid = UUID(correction_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid correction_id")
    success = await engine.correction_rule_service.deactivate_rule(cid)
    if not success:
        raise HTTPException(status_code=404, detail="Correction rule not found")
