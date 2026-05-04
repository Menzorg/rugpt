"""
RuGPT Engine Application

FastAPI application for the RuGPT corporate AI assistant.
"""
import os
import logging
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from .config import Config
from .logging_context import (
    CorrelationIDFilter,
    CorrelationIDMiddleware,
    RequestLoggingMiddleware,
)
from .services.engine_service import get_engine_service, init_engine_service
from .tasks.ingest_queue import ingest_queue
from .routes import (
    health_router,
    auth_router,
    organizations_router,
    users_router,
    roles_router,
    chats_router,
    calendar_router,
    notifications_router,
    in_app_notifications_router,
    tasks_router,
    task_polls_router,
    task_reports_router,
    files_router,
    rag_router,
    departments_router,
    projects_router,
    support_router,
    corrections_router,
)

# Configure logging with correlation_id in every record
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(correlation_id)s] %(name)s %(levelname)s: %(message)s",
)
_correlation_filter = CorrelationIDFilter()
for _handler in logging.root.handlers:
    _handler.addFilter(_correlation_filter)
logger = logging.getLogger("rugpt.app")

# Suppress noisy loggers
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("asyncpg").setLevel(logging.WARNING)

# Create FastAPI application
app = FastAPI(
    title="RuGPT Engine API",
    description="Corporate AI Assistant with Role System and Multi-tenancy",
    version="0.1.0"
)

# Request logging — added first so it runs INSIDE the correlation_id scope
# (FastAPI executes the outermost middleware first on the way in, which means
# middlewares added later wrap the ones added earlier).
app.add_middleware(RequestLoggingMiddleware)

# Correlation ID middleware — binds X-Correlation-ID for the whole request
# lifecycle so every subsequent log line carries it.
app.add_middleware(CorrelationIDMiddleware)

# Add CORS middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # Configure for production
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# Глобальный exception handler — uvicorn по дефолту пишет traceback только в stderr,
# наш DailyDirJsonlHandler этого не видит. Здесь явно прогоняем через `rugpt`-логгер
# с exc_info=True → traceback попадает в logs/<date>/engine.jsonl.
@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception):
    logger.exception(
        "unhandled %s %s -> %s: %s",
        request.method, request.url.path, type(exc).__name__, exc,
    )
    return JSONResponse(
        status_code=500,
        content={"detail": f"Internal error: {type(exc).__name__}: {exc}"},
    )


@app.on_event("startup")
async def startup_event():
    """Initialize services on startup"""
    logger.info("Starting RuGPT Engine...")

    try:
        await init_engine_service()
        logger.info("RuGPT Engine started successfully")
    except Exception as e:
        logger.error(f"Failed to start RuGPT Engine: {e}")
        raise


@app.on_event("shutdown")
async def shutdown_event():
    """Cleanup on shutdown"""
    logger.info("Shutting down RuGPT Engine...")

    # Drop queued ingest jobs; running threads finish naturally
    ingest_queue.shutdown(wait=False)

    try:
        engine = get_engine_service()
        await engine.close()
        logger.info("RuGPT Engine shutdown complete")
    except Exception as e:
        logger.error(f"Error during shutdown: {e}")


# Include routers
app.include_router(health_router, prefix="/api/v1", tags=["health"])
app.include_router(auth_router, prefix="/api/v1", tags=["auth"])
app.include_router(organizations_router, prefix="/api/v1", tags=["organizations"])
app.include_router(users_router, prefix="/api/v1", tags=["users"])
app.include_router(roles_router, prefix="/api/v1", tags=["roles"])
app.include_router(chats_router, tags=["chats"])
app.include_router(calendar_router, prefix="/api/v1", tags=["calendar"])
app.include_router(notifications_router, prefix="/api/v1", tags=["notifications"])
app.include_router(in_app_notifications_router, prefix="/api/v1", tags=["in-app-notifications"])
app.include_router(tasks_router, prefix="/api/v1", tags=["tasks"])
app.include_router(task_polls_router, prefix="/api/v1", tags=["task-polls"])
app.include_router(task_reports_router, prefix="/api/v1", tags=["task-reports"])
app.include_router(files_router, prefix="/api/v1", tags=["files"])
app.include_router(rag_router, prefix="/api/v1", tags=["rag"])
app.include_router(departments_router, prefix="/api/v1", tags=["departments"])
app.include_router(projects_router, prefix="/api/v1", tags=["projects"])
app.include_router(support_router, prefix="/api/v1", tags=["support"])
app.include_router(corrections_router, prefix="/api/v1", tags=["corrections"])


@app.get("/")
async def root():
    """Root endpoint"""
    return {
        "service": "RuGPT Engine",
        "version": "0.1.0",
        "status": "running"
    }


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        app,
        host=Config.API_HOST,
        port=Config.API_PORT
    )
