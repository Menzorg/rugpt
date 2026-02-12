"""
RuGPT Engine Application

FastAPI application for the RuGPT corporate AI assistant.
"""
import os
import logging
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from .config import Config
from .services.engine_service import get_engine_service, init_engine_service
from .routes import (
    health_router,
    auth_router,
    organizations_router,
    users_router,
    roles_router,
    chats_router,
    calendar_router,
    notifications_router
)

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
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

# Add CORS middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # Configure for production
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
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
