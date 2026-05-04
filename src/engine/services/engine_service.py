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
from ..storage.message_attachment_storage import MessageAttachmentStorage
from ..storage.calendar_storage import CalendarStorage
from ..storage.notification_channel_storage import NotificationChannelStorage
from ..storage.notification_log_storage import NotificationLogStorage
from ..storage.in_app_notification_storage import InAppNotificationStorage
from ..storage.task_storage import TaskStorage
from ..storage.task_participant_storage import TaskParticipantStorage
from ..storage.task_poll_storage import TaskPollStorage
from ..storage.task_report_storage import TaskReportStorage
from ..storage.user_file_storage import UserFileStorage
from ..storage.correction_rule_storage import CorrectionRuleStorage
from ..storage.device_storage import DeviceStorage
from ..storage.department_storage import DepartmentStorage
from ..storage.project_storage import ProjectStorage
from ..storage.task_event_storage import TaskEventStorage
from ..storage.agent_run_storage import AgentRunStorage
from ..storage.support_ticket_storage import SupportTicketStorage
from ..storage.support_ticket_event_storage import SupportTicketEventStorage
from ..storage.memory_snapshot_storage import MemorySnapshotStorage
from ..storage.storage_adapter import LocalStorageAdapter
from .chat_service import ChatService
from .project_service import ProjectService
from .task_event_service import TaskEventService
from .reference_service import ReferenceService
from ..kafka.producer import KafkaProducerService
from ..kafka.consumer import KafkaConsumerLoop
from ..kafka.agent_handler import AgentRequestHandler
from .mention_service import MentionService
from .ai_service import AIService
from .prompt_cache import PromptCache
from .calendar_service import CalendarService
from .scheduler_service import SchedulerService
from .notification_service import NotificationService
from .in_app_notification_service import InAppNotificationService
from .task_service import TaskService
from .task_poll_service import TaskPollService
from .task_report_service import TaskReportService
from .file_service import FileService
from .correction_rule_service import CorrectionRuleService
from .memory_service import MemoryService
from .department_service import DepartmentService
from .rag_service import RAGService
from .support_notification_service import SupportNotificationService
from .support_ticket_service import SupportTicketService
from ..storage.rag_store import RAG_store
from ..notifications.telegram_sender import TelegramSender
from ..notifications.email_sender import EmailSender

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
        self.message_attachment_storage = MessageAttachmentStorage(self.postgres_dsn)
        # Wire attachment storage into message storage so list_by_chat / get_by_id
        # auto-hydrate `Message.attachments` for callers (chat_service, routes).
        self.message_storage.attachment_storage = self.message_attachment_storage
        self.calendar_storage = CalendarStorage(self.postgres_dsn)
        self.notification_channel_storage = NotificationChannelStorage(self.postgres_dsn)
        self.notification_log_storage = NotificationLogStorage(self.postgres_dsn)
        self.in_app_notification_storage = InAppNotificationStorage(self.postgres_dsn)
        self.task_storage = TaskStorage(self.postgres_dsn)
        self.task_participant_storage = TaskParticipantStorage(self.postgres_dsn)
        self.task_poll_storage = TaskPollStorage(self.postgres_dsn)
        self.task_report_storage = TaskReportStorage(self.postgres_dsn)
        self.user_file_storage = UserFileStorage(self.postgres_dsn)
        self.correction_rule_storage = CorrectionRuleStorage(self.postgres_dsn)
        self.device_storage = DeviceStorage(self.postgres_dsn)
        self.department_storage = DepartmentStorage(self.postgres_dsn)
        self.project_storage = ProjectStorage(self.postgres_dsn)
        self.task_event_storage = TaskEventStorage(self.postgres_dsn)
        self.agent_run_storage = AgentRunStorage(self.postgres_dsn)
        self.support_ticket_storage = SupportTicketStorage(self.postgres_dsn)
        self.support_ticket_event_storage = SupportTicketEventStorage(self.postgres_dsn)
        self.memory_snapshot_storage = MemorySnapshotStorage(self.postgres_dsn)

        # Initialize prompt cache (prompts dir relative to project root)
        prompts_dir = str(Config.BASE_DIR / "src" / "engine" / "prompts")
        self.prompt_cache = PromptCache(prompts_dir)

        # Initialize calendar service
        self.calendar_service = CalendarService(self.calendar_storage)

        # Initialize department service
        self.department_service = DepartmentService(self.department_storage, self.user_storage)

        # Initialize in-app notification service
        self.in_app_notification_service = InAppNotificationService(self.in_app_notification_storage)

        # Support ticket notification service — fan-out to RuGPT Support operators
        # via in-app notifications (type='system', reference_type='support_ticket').
        self.support_notification_service = SupportNotificationService(
            in_app_notification_service=self.in_app_notification_service,
            user_storage=self.user_storage,
        )

        # Initialize chat/task_event/project services (order matters):
        # ChatService -> TaskEventService -> ProjectService -> TaskService
        self.chat_service = ChatService(
            self.chat_storage,
            self.message_storage,
            user_file_storage=self.user_file_storage,
            message_attachment_storage=self.message_attachment_storage,
        )

        # Support ticket service — business logic for tech-support tickets.
        # Depends on chat_storage/message_storage/user_storage directly (not chat_service).
        self.support_ticket_service = SupportTicketService(
            ticket_storage=self.support_ticket_storage,
            event_storage=self.support_ticket_event_storage,
            chat_storage=self.chat_storage,
            message_storage=self.message_storage,
            user_storage=self.user_storage,
            notification_service=self.support_notification_service,
        )

        self.task_event_service = TaskEventService(self.task_event_storage)
        self.project_service = ProjectService(self.project_storage, self.chat_service)

        # Kafka producer — event bus to NestJS (chat.events) + internal queue (agent.requests).
        # No-op when Config.KAFKA_ENABLED=false, so tests without Kafka keep working.
        self.kafka_producer = KafkaProducerService()

        # Initialize task service with chat/event/project/notification integration
        self.task_service = TaskService(
            self.task_storage,
            self.in_app_notification_service,
            chat_service=self.chat_service,
            task_event_service=self.task_event_service,
            project_service=self.project_service,
            user_storage=self.user_storage,
            task_participant_storage=self.task_participant_storage,
        )

        # Reference service (parallel to mentions, resolves !<uuid>/!!<uuid> in messages)
        self.reference_service = ReferenceService(
            task_storage=self.task_storage,
            project_storage=self.project_storage,
        )

        # Initialize task poll service
        self.task_poll_service = TaskPollService(
            storage=self.task_poll_storage,
            task_service=self.task_service,
            in_app_notification_service=self.in_app_notification_service,
        )

        # Initialize task report service.
        # AgentExecutor is wired in below after it's constructed (chicken-and-egg).
        self.task_report_service = TaskReportService(
            storage=self.task_report_storage,
            task_poll_service=self.task_poll_service,
            in_app_notification_service=self.in_app_notification_service,
            role_storage=self.role_storage,
            task_storage=self.task_storage,
            user_storage=self.user_storage,
        )

        # Initialize file service with StorageAdapter
        self.storage_adapter = LocalStorageAdapter(base_dir=Config.STORAGE_LOCAL_DIR)
        self.file_service = FileService(
            file_storage=self.user_file_storage,
            storage_adapter=self.storage_adapter,
            max_file_size=Config.FILE_MAX_SIZE_MB * 1024 * 1024,
            allowed_types=set(Config.FILE_ALLOWED_TYPES.split(",")),
        )

        # Initialize RAG store and service
        self.rag_store = RAG_store(
            dsn=Config.RAG_STORE_DSN,
            vector_dim=Config.RAG_VECTOR_DIM,
        )
        self.rag_service = RAGService(
            store=self.rag_store,
            embedding_model=Config.EMBEDDING_MODEL,
            llm_base_url=Config.LLM_BASE_URL,
            llm_api_key=Config.LLM_API_KEY,
            chunk_size=Config.RAG_CHUNK_SIZE,
            chunk_overlap=Config.RAG_CHUNK_OVERLAP,
            summary_input_max_chars=Config.RAG_SUMMARY_INPUT_MAX_CHARS,
            file_storage=self.user_file_storage,  # для обновления rag_status при индексации
        )

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
        from ..agents.tools.task_tool import create_task_tools
        from ..agents.tools.rag_tool import rag_search
        from ..agents.tools.expand_chunk_tool import create_expand_chunk_tool
        from ..agents.tools.table_rows_tool import table_rows_search
        from ..agents.tools.web_tool import web_search
        from ..agents.tools.role_call_tool import role_call
        from ..agents.tools.list_documents import list_documents
        from ..agents.tools.user_tool import create_user_tools
        from ..agents.tools.analyze_image import create_analyze_image_tool

        # Create calendar tools wired to CalendarService
        cal_create_tool, cal_query_tool = create_calendar_tools(self.calendar_service)

        # Create task tools wired to TaskService
        task_create_tool, task_query_tool, task_update_tool = create_task_tools(self.task_service)
        expand_chunk_tool = create_expand_chunk_tool(self.rag_service, self.user_file_storage)
        analyze_image_tool = create_analyze_image_tool(
            self.user_file_storage,
            self.storage_adapter,
        )

        # Initialize tool registry
        self.tool_registry = ToolRegistry()
        self.tool_registry.register("calendar_create", cal_create_tool)
        self.tool_registry.register("calendar_query", cal_query_tool)
        self.tool_registry.register("task_create", task_create_tool)
        self.tool_registry.register("task_query", task_query_tool)
        self.tool_registry.register("task_update", task_update_tool)
        self.tool_registry.register("rag_search", rag_search)
        self.tool_registry.register("expand_chunk", expand_chunk_tool)
        self.tool_registry.register("table_rows_search", table_rows_search)
        self.tool_registry.register("web_search", web_search)
        self.tool_registry.register("role_call", role_call)
        self.tool_registry.register("list_documents", list_documents)
        self.tool_registry.register("analyze_image", analyze_image_tool)

        (user_search_tool,) = create_user_tools(
            user_storage=self.user_storage,
            role_storage=self.role_storage,
            department_service=self.department_service,
        )
        self.tool_registry.register("user_search", user_search_tool)

        # MemoryService needs AgentExecutor, so it is created after it.
        # AgentExecutor receives memory_service via setter below to break the chicken-egg.
        self.agent_executor = AgentExecutor(
            base_url=Config.LLM_BASE_URL,
            api_key=Config.LLM_API_KEY,
            default_model=Config.DEFAULT_MODEL,
            prompt_cache=self.prompt_cache,
            tool_registry=self.tool_registry,
        )

        self.memory_service = MemoryService(
            agent_executor=self.agent_executor,
            chat_storage=self.chat_storage,
            message_storage=self.message_storage,
            memory_snapshot_storage=self.memory_snapshot_storage,
        )
        self.agent_executor.memory_service = self.memory_service
        self.task_report_service.agent_executor = self.agent_executor
        # Wire chat/message storage so TaskReportService can fall back to raw
        # transcript when an expired poll has no AI-generated summary.
        self.task_report_service.chat_storage = self.chat_storage
        self.task_report_service.message_storage = self.message_storage

        # Initialize scheduler (started in initialize(), stopped in close())
        self.scheduler_service = SchedulerService(
            calendar_service=self.calendar_service,
            notification_service=self.notification_service,
            agent_executor=self.agent_executor,
            role_storage=self.role_storage,
            user_storage=self.user_storage,
            org_storage=self.org_storage,
            task_service=self.task_service,
            task_poll_service=self.task_poll_service,
            task_report_service=self.task_report_service,
            poll_interval=int(Config.SCHEDULER_POLL_INTERVAL),
            enabled=Config.SCHEDULER_ENABLED,
        )

        # Initialize remaining services (chat_service already created above)
        self.mention_service = MentionService(self.user_storage)
        self.ai_service = AIService(
            role_storage=self.role_storage,
            user_storage=self.user_storage,
            chat_storage=self.chat_storage,
            message_storage=self.message_storage,
            prompt_cache=self.prompt_cache,
            agent_executor=self.agent_executor,
            agent_run_storage=self.agent_run_storage,
            kafka_producer=self.kafka_producer,
            support_ticket_storage=self.support_ticket_storage,
            support_ticket_event_storage=self.support_ticket_event_storage,
            task_poll_storage=self.task_poll_storage,
            task_storage=self.task_storage,
            storage_adapter=self.storage_adapter,
        )

        # Wire poll-chat + AI deps into TaskPollService (post-construction —
        # AIService and ChatService are constructed after TaskPollService to
        # avoid circular dependency at __init__ time).
        self.task_poll_service.chat_service = self.chat_service
        self.task_poll_service.ai_service = self.ai_service
        self.task_poll_service.user_storage = self.user_storage

        # Wire poll-retry deps into SchedulerService (post-construction —
        # AIService is constructed after SchedulerService for the same reason).
        # Used by SchedulerService._retry_stuck_poll_initials.
        self.scheduler_service.chat_storage = self.chat_storage
        self.scheduler_service.message_storage = self.message_storage
        self.scheduler_service.agent_run_storage = self.agent_run_storage
        self.scheduler_service.ai_service = self.ai_service
        self.scheduler_service.in_app_notification_service = self.in_app_notification_service

        # Kafka consumer for agent.requests topic (async inference).
        # Created here; started in initialize() after storages are connected.
        self._agent_request_handler = AgentRequestHandler(
            ai_service=self.ai_service,
            message_storage=self.message_storage,
            agent_run_storage=self.agent_run_storage,
            kafka_producer=self.kafka_producer,
        )
        self.agent_request_consumer = KafkaConsumerLoop(
            topic=Config.KAFKA_TOPIC_AGENT_REQUESTS,
            group_id=Config.KAFKA_CONSUMER_GROUP_AGENT_RUNNERS,
            handler=self._agent_request_handler,
        )

        # Initialize correction rule service
        self.correction_rule_service = CorrectionRuleService(
            correction_rule_storage=self.correction_rule_storage,
            message_storage=self.message_storage,
            role_storage=self.role_storage,
            user_storage=self.user_storage,
            chat_service=self.chat_service,
            agent_executor=self.agent_executor,
            embedding_model=Config.EMBEDDING_MODEL,
            llm_base_url=Config.LLM_BASE_URL,
            llm_api_key=Config.LLM_API_KEY,
            kafka_producer=self.kafka_producer,
        )
        self.agent_executor.correction_rule_service = self.correction_rule_service

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
        await self.message_attachment_storage.init()
        await self.calendar_storage.init()
        await self.notification_channel_storage.init()
        await self.notification_log_storage.init()
        await self.in_app_notification_storage.init()
        await self.task_storage.init()
        await self.task_participant_storage.init()
        await self.task_poll_storage.init()
        await self.task_report_storage.init()
        await self.user_file_storage.init()
        await self.correction_rule_storage.init()
        await self.device_storage.init()
        await self.department_storage.init()
        await self.project_storage.init()
        await self.task_event_storage.init()
        await self.agent_run_storage.init()
        await self.support_ticket_storage.init()
        await self.support_ticket_event_storage.init()
        await self.memory_snapshot_storage.init()

        await self.rag_store.init()

        # Wire the shared RAGService into the RAG tool
        from ..agents.tools.rag_tool import init_rag_service
        init_rag_service(self.rag_service, self.user_file_storage)

        # Wire the shared RAGService into the table rows tool
        from ..agents.tools.table_rows_tool import init_table_rows_service
        init_table_rows_service(self.rag_service, self.user_file_storage)

        # Wire the shared UserFileStorage into the document tool
        from ..agents.tools.list_documents import init_document_service
        init_document_service(self.user_file_storage, self.rag_service)

        # Start Kafka producer (no-op when KAFKA_ENABLED=false)
        try:
            await self.kafka_producer.start()
        except Exception as e:
            logger.error(f"Kafka producer failed to start: {e}")

        # Start Kafka consumer loop for agent.requests (no-op when disabled)
        try:
            await self.agent_request_consumer.start()
        except Exception as e:
            logger.error(f"Kafka agent.requests consumer failed to start: {e}")

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
        await self.message_attachment_storage.close()
        await self.calendar_storage.close()
        await self.notification_channel_storage.close()
        await self.notification_log_storage.close()
        await self.in_app_notification_storage.close()
        await self.task_storage.close()
        await self.task_participant_storage.close()
        await self.task_poll_storage.close()
        await self.task_report_storage.close()
        await self.user_file_storage.close()
        await self.correction_rule_storage.close()
        await self.device_storage.close()
        await self.department_storage.close()
        await self.project_storage.close()
        await self.task_event_storage.close()
        await self.agent_run_storage.close()
        await self.support_ticket_storage.close()
        await self.support_ticket_event_storage.close()
        await self.memory_snapshot_storage.close()
        await self.rag_store.close()
        await self.scheduler_service.stop()
        await self.notification_service.close()
        await self.ai_service.close()
        try:
            await self.agent_request_consumer.stop()
        except Exception as e:
            logger.error(f"Kafka consumer failed to stop: {e}")
        try:
            await self.kafka_producer.stop()
        except Exception as e:
            logger.error(f"Kafka producer failed to stop: {e}")

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
