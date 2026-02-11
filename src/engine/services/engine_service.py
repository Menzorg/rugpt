"""
Engine Service

Main composite service that manages all storages and services.
Singleton pattern - one instance per process.
"""
import logging
from typing import Optional

from ..config import Config
from ..storage.org_storage import OrgStorage
from ..storage.user_storage import UserStorage
from ..storage.role_storage import RoleStorage
from ..storage.chat_storage import ChatStorage
from ..storage.message_storage import MessageStorage
from ..storage.calendar_storage import CalendarStorage
from ..storage.notification_channel_storage import NotificationChannelStorage
from ..storage.notification_log_storage import NotificationLogStorage
from .chat_service import ChatService
from .mention_service import MentionService
from .ai_service import AIService
from .prompt_cache import PromptCache
from .calendar_service import CalendarService
from .scheduler_service import SchedulerService
from .notification_service import NotificationService
from ..notifications.telegram_sender import TelegramSender
from ..notifications.email_sender import EmailSender
from ..llm.providers.ollama import OllamaProvider

logger = logging.getLogger("rugpt.services.engine")

# Singleton instance
_engine_service: Optional["EngineService"] = None


class EngineService:
    """
    Composite engine service.

    Manages:
    - All storage connections (PostgreSQL)
    - Business logic services
    - Graceful shutdown
    """

    def __init__(self):
        """Initialize engine service with all storages"""
        self.postgres_dsn = Config.get_postgres_dsn()

        # Initialize storages
        self.org_storage = OrgStorage(self.postgres_dsn)
        self.user_storage = UserStorage(self.postgres_dsn)
        self.role_storage = RoleStorage(self.postgres_dsn)
        self.chat_storage = ChatStorage(self.postgres_dsn)
        self.message_storage = MessageStorage(self.postgres_dsn)
        self.calendar_storage = CalendarStorage(self.postgres_dsn)
        self.notification_channel_storage = NotificationChannelStorage(self.postgres_dsn)
        self.notification_log_storage = NotificationLogStorage(self.postgres_dsn)

        # Initialize LLM provider (kept for health checks / model listing)
        self.llm_provider = OllamaProvider()

        # Initialize prompt cache (prompts dir relative to project root)
        prompts_dir = str(Config.BASE_DIR / "src" / "engine" / "prompts")
        self.prompt_cache = PromptCache(prompts_dir)

        # Initialize calendar service
        self.calendar_service = CalendarService(self.calendar_storage)

        # Initialize notification service
        self.notification_service = NotificationService(
            channel_storage=self.notification_channel_storage,
            log_storage=self.notification_log_storage,
        )

        # Register notification senders
        if Config.TELEGRAM_BOT_TOKEN:
            self.telegram_sender = TelegramSender(Config.TELEGRAM_BOT_TOKEN)
            self.notification_service.register_sender("telegram", self.telegram_sender)
        else:
            self.telegram_sender = None
            logger.info("Telegram sender disabled (no TELEGRAM_BOT_TOKEN)")

        if Config.SMTP_HOST:
            self.email_sender = EmailSender(
                smtp_host=Config.SMTP_HOST,
                smtp_port=Config.SMTP_PORT,
                smtp_user=Config.SMTP_USER,
                smtp_password=Config.SMTP_PASSWORD,
            )
            self.notification_service.register_sender("email", self.email_sender)
        else:
            self.email_sender = None
            logger.info("Email sender disabled (no SMTP_HOST)")

        # Lazy imports to avoid circular dependency (agents -> services -> agents)
        from ..agents.executor import AgentExecutor
        from ..agents.tools.registry import ToolRegistry
        from ..agents.tools.calendar_tool import create_calendar_tools
        from ..agents.tools.rag_tool import rag_search
        from ..agents.tools.web_tool import web_search
        from ..agents.tools.role_call_tool import role_call

        # Create calendar tools wired to CalendarService
        cal_create_tool, cal_query_tool = create_calendar_tools(self.calendar_service)

        # Initialize tool registry
        self.tool_registry = ToolRegistry()
        self.tool_registry.register("calendar_create", cal_create_tool)
        self.tool_registry.register("calendar_query", cal_query_tool)
        self.tool_registry.register("rag_search", rag_search)
        self.tool_registry.register("web_search", web_search)
        self.tool_registry.register("role_call", role_call)

        # Initialize agent executor (replaces direct OllamaProvider for generation)
        self.agent_executor = AgentExecutor(
            base_url=Config.LLM_BASE_URL,
            default_model=Config.DEFAULT_MODEL,
            prompt_cache=self.prompt_cache,
            tool_registry=self.tool_registry,
        )

        # Initialize scheduler (started in initialize(), stopped in close())
        self.scheduler_service = SchedulerService(
            calendar_service=self.calendar_service,
            notification_service=self.notification_service,
            agent_executor=self.agent_executor,
            role_storage=self.role_storage,
            poll_interval=int(Config.SCHEDULER_POLL_INTERVAL),
            enabled=Config.SCHEDULER_ENABLED,
        )

        # Initialize services (after storages)
        self.chat_service = ChatService(self.chat_storage, self.message_storage)
        self.mention_service = MentionService(self.user_storage)
        self.ai_service = AIService(
            role_storage=self.role_storage,
            user_storage=self.user_storage,
            chat_storage=self.chat_storage,
            message_storage=self.message_storage,
            llm_provider=self.llm_provider,
            prompt_cache=self.prompt_cache,
            agent_executor=self.agent_executor,
        )

        self._initialized = False
        logger.info("EngineService created")

    async def initialize(self):
        """Initialize all storages"""
        if self._initialized:
            logger.info("EngineService already initialized")
            return

        logger.info("Initializing EngineService...")

        # Initialize all storages
        await self.org_storage.init()
        await self.user_storage.init()
        await self.role_storage.init()
        await self.chat_storage.init()
        await self.message_storage.init()
        await self.calendar_storage.init()
        await self.notification_channel_storage.init()
        await self.notification_log_storage.init()

        # Start background scheduler
        await self.scheduler_service.start()

        self._initialized = True
        logger.info("EngineService initialized successfully")

    async def close(self):
        """Close all connections"""
        logger.info("Closing EngineService...")

        await self.org_storage.close()
        await self.user_storage.close()
        await self.role_storage.close()
        await self.chat_storage.close()
        await self.message_storage.close()
        await self.calendar_storage.close()
        await self.notification_channel_storage.close()
        await self.notification_log_storage.close()
        await self.scheduler_service.stop()
        await self.notification_service.close()
        await self.ai_service.close()

        self._initialized = False
        logger.info("EngineService closed")

    @property
    def is_initialized(self) -> bool:
        """Check if service is initialized"""
        return self._initialized

    @classmethod
    def get_instance(cls) -> "EngineService":
        """Get singleton instance"""
        return get_engine_service()


def get_engine_service() -> EngineService:
    """Get or create engine service singleton"""
    global _engine_service
    if _engine_service is None:
        _engine_service = EngineService()
    return _engine_service


async def init_engine_service() -> EngineService:
    """Initialize and return engine service"""
    service = get_engine_service()
    await service.initialize()
    return service
