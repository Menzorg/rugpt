"""
Health Check Routes

Endpoints for service health monitoring.
"""
from fastapi import APIRouter
from datetime import datetime

router = APIRouter(prefix="/health", tags=["health"])


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
    Readiness check - indicates if service is ready to handle requests.
    Used by Kubernetes/orchestrators for readiness probes.
    """
    # TODO: Check database connectivity
    return {
        "ready": True,
        "timestamp": datetime.utcnow().isoformat()
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
