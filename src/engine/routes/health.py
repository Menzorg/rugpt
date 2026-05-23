"""
Health Check Routes

Endpoints for service health monitoring.
"""
import httpx
import asyncio
import time
from fastapi import APIRouter, Response
from datetime import datetime

from ..services.engine_service import get_engine_service
from ..config import Config
from src.engine.unified_logger import get_logger    

logger = get_logger("routes")  

router = APIRouter(prefix="/health", tags=["health"])


async def _litellm_alive() -> bool:
    """Ping LiteLLM /health/liveness. Returns True on HTTP 200."""
    root_url = Config.LLM_ROOT_URL
    try:
        async with httpx.AsyncClient(timeout=3.0) as client:
            resp = await client.get(f"{root_url}/health/liveness")
            return resp.status_code == 200
    except Exception as e:
        logger.warning(f"LiteLLM health check failed: {e}")
        return False

async def _check_postgres() -> dict:
    """Ping Postgres via shared pool. Returns dict with status/duration_ms/error."""
    start = time.perf_counter()
    engine = get_engine_service()
    # NB: пулы живут в каждом storage (BaseStorage.pg_pool). Все используют
    # один DSN — берём любой репрезентативный, чтобы проверить, что PG жив.
    pool = engine.user_storage.pg_pool if engine else None
    if pool is None:
        return {"status": "fail", "duration_ms": 0, "error": "pg_pool not initialized"}
    try:
        async with pool.acquire() as conn:
            await asyncio.wait_for(conn.fetchval("SELECT 1"), timeout=2.0)
        duration_ms = int((time.perf_counter() - start) * 1000)
        return {"status": "ok", "duration_ms": duration_ms}
    except Exception as e:
        duration_ms = int((time.perf_counter() - start) * 1000)
        logger.warning(f"postgres health check failed: {e}")
        return {"status": "fail", "duration_ms": duration_ms, "error": str(e)}

async def _check_kafka() -> dict:
    if not Config.KAFKA_ENABLED:
        return {"status": "skipped", "duration_ms": 0}
    start = time.perf_counter()
    engine = get_engine_service()
    producer = engine.kafka_producer if engine else None
    if producer is None:
        return {"status": "fail", "duration_ms": 0, "error": "producer not initialized"}
    try:
        await asyncio.wait_for(producer.ping(), timeout=2.0)
        duration_ms = int((time.perf_counter() - start) * 1000)
        return {"status": "ok", "duration_ms": duration_ms}
    except Exception as e:
        duration_ms = int((time.perf_counter() - start) * 1000)
        logger.warning(f"kafka health check failed: {e}")
        return {"status": "fail", "duration_ms": duration_ms, "error": str(e)}

async def _check_vllm() -> dict:
    start = time.perf_counter()
    try:
        ok = await asyncio.wait_for(_litellm_alive(), timeout=2.0)
        duration_ms = int((time.perf_counter() - start) * 1000)
        if ok:
            return {"status": "ok", "duration_ms": duration_ms} 
        return {"status": "fail", "duration_ms": duration_ms, "error": "litellm not alive"}
    except Exception as e:
        duration_ms = int((time.perf_counter() - start) * 1000)
        logger.warning(f"vllm health check failed: {e}")
        return {"status": "fail", "duration_ms": duration_ms, "error": str(e)}

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
async def readiness_check(response: Response):
    """
    Readiness check — verifies critical downstream dependencies.
    Used by Kubernetes/orchestrators for readiness probes.

    Critical (block readiness): Postgres, vLLM (via LiteLLM), Kafka (if enabled).
    Returns HTTP 503 when any critical dependency is down.
    """
    postgres, vllm, kafka = await asyncio.gather(
        _check_postgres(),
        _check_vllm(),
        _check_kafka(),
        return_exceptions=False,
    )
    ready = (
        postgres["status"] == "ok"
        and vllm["status"] == "ok"
        and kafka["status"] in ("ok", "skipped")
    )
    if not ready:
        response.status_code = 503
        logger.warning("readiness_check failed", postgres=postgres["status"], vllm=vllm["status"], kafka=kafka["status"])
    return {"ready": ready, "checks": {"postgres": postgres, "vllm": vllm, "kafka": kafka}, "timestamp": datetime.utcnow().isoformat()}


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
