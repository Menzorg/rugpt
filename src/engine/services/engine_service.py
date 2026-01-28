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
from .chat_service import ChatService
from .mention_service import MentionService
from .ai_service import AIService
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

        # Initialize LLM provider
        self.llm_provider = OllamaProvider()

        # Initialize services (after storages)
        self.chat_service = ChatService(self.chat_storage, self.message_storage)
        self.mention_service = MentionService(self.user_storage)
        self.ai_service = AIService(
            role_storage=self.role_storage,
            user_storage=self.user_storage,
            chat_storage=self.chat_storage,
            message_storage=self.message_storage,
            llm_provider=self.llm_provider
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
