"""
Health Check Routes

Endpoints for service health monitoring.
"""
import logging

import httpx
from fastapi import APIRouter
from datetime import datetime

from ..config import Config

logger = logging.getLogger("rugpt.routes.health")

router = APIRouter(prefix="/health", tags=["health"])


async def _litellm_alive() -> bool:
    """Ping LiteLLM /health/liveness. Returns True on HTTP 200."""
    root_url = Config.LLM_BASE_URL.removesuffix("/v1").removesuffix("/")
    try:
        async with httpx.AsyncClient(timeout=3.0) as client:
            resp = await client.get(f"{root_url}/health/liveness")
            return resp.status_code == 200
    except Exception as e:
        logger.warning(f"LiteLLM health check failed: {e}")
        return False


@router.get("")
@router.get("/")
async def health_check():
    """Basic health check endpoint"""
    return {
        "status": "healthy",
        "service": "rugpt-engine",
        "timestamp": datetime.utcnow().isoformat()
    }


@router.get("/ready")
async def readiness_check():
    """
    Readiness check — verifies downstream dependencies (LiteLLM).
    Used by Kubernetes/orchestrators for readiness probes.
    """
    litellm_ok = await _litellm_alive()
    return {
        "ready": bool(litellm_ok),
        "litellm": "ok" if litellm_ok else "fail",
        "timestamp": datetime.utcnow().isoformat(),
    }


@router.get("/live")
async def liveness_check():
    """
    Liveness check - indicates if service is running.
    Used by Kubernetes/orchestrators for liveness probes.
    """
    return {
        "alive": True,
        "timestamp": datetime.utcnow().isoformat()
    }
