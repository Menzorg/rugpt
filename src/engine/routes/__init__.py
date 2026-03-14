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
from .in_app_notifications import router as in_app_notifications_router
from .tasks import router as tasks_router
from .task_polls import router as task_polls_router
from .task_reports import router as task_reports_router
from .files import router as files_router
from .rag import router as rag_router

__all__ = [
    'health_router',
    'auth_router',
    'organizations_router',
    'users_router',
    'roles_router',
    'chats_router',
    'calendar_router',
    'notifications_router',
    'in_app_notifications_router',
    'tasks_router',
    'task_polls_router',
    'task_reports_router',
    'files_router',
    'rag_router',
]
