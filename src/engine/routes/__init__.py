"""
RuGPT API Routes

FastAPI route handlers for RuGPT engine.
"""
from .health import router as health_router
from .auth import router as auth_router
from .organizations import router as organizations_router
from .users import router as users_router
from .roles import router as roles_router
from .chats import router as chats_router
from .calendar import router as calendar_router
from .notifications import router as notifications_router

__all__ = [
    'health_router',
    'auth_router',
    'organizations_router',
    'users_router',
    'roles_router',
    'chats_router',
    'calendar_router',
    'notifications_router',
]
