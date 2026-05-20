"""Generic dispatcher for action_types registered in EngineService.action_registry.

This is the single backend chokepoint for "AI prepares, human decides" flows:
the frontend modal renderer POSTs here with the action_type chosen by the user.
All authorization, parameter validation and side effects live in handler
modules under `src/engine/actions/`.
"""
from fastapi import APIRouter, Depends, HTTPException
from uuid import UUID

from src.engine.actions.registry import (
    ActionError,
    PermissionDeniedError,
    UnknownActionError,
)
from src.engine.routes.auth import get_current_user
from src.engine.services.engine_service import get_engine_service
from src.engine.unified_logger import get_logger

logger = get_logger("routes")

router = APIRouter(prefix="/actions", tags=["actions"])


@router.post("/{action_type}")
async def dispatch_action(
    action_type: str,
    params: dict,
    current_user: dict = Depends(get_current_user),
):
    engine = get_engine_service()
    user = await engine.user_storage.get_by_id(UUID(str(current_user["user_id"])))
    if not user:
        raise HTTPException(status_code=401, detail="User not found")

    try:
        result = await engine.action_registry.dispatch(
            engine=engine,
            action_type=action_type,
            params=params or {},
            user=user,
        )
        return result
    except UnknownActionError as exc:
        logger.warning("action dispatch unknown_type=%s user=%s", action_type, user.id)
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except PermissionDeniedError as exc:
        logger.warning("action dispatch denied type=%s user=%s", action_type, user.id)
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except ActionError as exc:
        logger.warning("action dispatch bad_params type=%s user=%s err=%s", action_type, user.id, exc)
        raise HTTPException(status_code=400, detail=str(exc)) from exc
