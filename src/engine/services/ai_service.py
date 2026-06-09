"""
AI Service

Service for generating AI responses.
Core rule: system user → their role responds (mirror → sender's role).
"""
from __future__ import annotations

from src.engine.unified_logger import get_logger
import re
from datetime import datetime
from typing import Optional, List, TYPE_CHECKING
from uuid import UUID, uuid4

from ..config import Config
from ..constants import IMAGE_TYPES
from ..agents.result import AgentResult
from ..models.agent_run import AgentRun
from ..models.chat import ChatType
from ..models.message import Message, Mention, MentionType, SenderType
from ..models.role import Role
from ..models.support_ticket_event import (
    SupportTicketActorRole,
    SupportTicketEvent,
    SupportTicketEventType,
)
from ..storage.role_storage import RoleStorage
from ..storage.user_storage import UserStorage
from ..storage.message_storage import MessageStorage
from ..storage.chat_storage import ChatStorage
from ..storage.agent_run_storage import AgentRunStorage
from ..utils.image_parser import image_bytes_to_data_url
from .prompt_cache import PromptCache

# Avoid circular import: agents.executor -> services -> ai_service -> agents.executor
if TYPE_CHECKING:
    from ..agents.executor import AgentExecutor
    from ..kafka.producer import KafkaProducerService
    from ..storage.support_ticket_storage import SupportTicketStorage
    from ..storage.support_ticket_event_storage import SupportTicketEventStorage
    from ..storage.task_poll_storage import TaskPollStorage
    from ..storage.task_storage import TaskStorage
    from ..storage.storage_adapter import StorageAdapter

logger = get_logger("services")



# Poll-dialog role codes (lives in system org per migration 029).
POLL_INTERVIEWER_ROLE_CODE = "poll_interviewer"
POLL_SUMMARIZER_ROLE_CODE = "poll_summarizer"
RUGPT_SYSTEM_ORG_ID = UUID("00000000-0000-0000-0000-000000000000")


class AIService:
    """Service for AI response generation"""

    def __init__(
        self,
        role_storage: RoleStorage,
        user_storage: UserStorage,
        chat_storage: ChatStorage,
        message_storage: MessageStorage,
        prompt_cache: Optional[PromptCache] = None,
        agent_executor: Optional[AgentExecutor] = None,
        agent_run_storage: Optional[AgentRunStorage] = None,
        kafka_producer: Optional["KafkaProducerService"] = None,
        support_ticket_storage: Optional["SupportTicketStorage"] = None,
        support_ticket_event_storage: Optional["SupportTicketEventStorage"] = None,
        task_poll_storage: Optional["TaskPollStorage"] = None,
        task_storage: Optional["TaskStorage"] = None,
        storage_adapter: Optional["StorageAdapter"] = None,
    ):
        self.role_storage = role_storage
        self.user_storage = user_storage
        self.chat_storage = chat_storage
        self.message_storage = message_storage
        self.prompt_cache = prompt_cache
        self.agent_executor = agent_executor
        self.agent_run_storage = agent_run_storage
        self.kafka_producer = kafka_producer
        # Optional support-ticket deps — wired by EngineService when support
        # subsystem is active. Setter-style assignment also supported (legacy
        # callers / tests construct AIService without these and inject later).
        self.support_ticket_storage = support_ticket_storage
        self.support_ticket_event_storage = support_ticket_event_storage
        # Poll-dialog deps — wired by EngineService for poll_interviewer /
        # poll_summarizer LLM scenarios. Default None for backward compat with
        # callers/tests that construct AIService without these (they degrade
        # gracefully — the poll methods raise a clear RuntimeError if invoked
        # without these wired).
        self.task_poll_storage = task_poll_storage
        self.task_storage = task_storage
        self.storage_adapter = storage_adapter

    def _is_support_aware(self) -> bool:
        """True iff both support storages are wired. Gates the support hook so
        existing callers / tests that don't construct support deps degrade
        silently to legacy behavior."""
        return (
            getattr(self, "support_ticket_storage", None) is not None
            and getattr(self, "support_ticket_event_storage", None) is not None
        )

    async def _enqueue_agent_run(
        self,
        message: Message,
        responder_id: UUID,
        strip_username: Optional[str] = None,
        role_code: str = "",
        invocation_kind_override: Optional[str] = None,
    ) -> Optional[UUID]:
        """Create AgentRun row + publish to agent.requests. Returns request_id or None on failure."""
        request_id = uuid4()
        logger.info(
            f"Enqueue agent run: request_id={request_id} chat={message.chat_id} "
            f"responder={responder_id} role={role_code or '-'}"
        )
        run = AgentRun(
            request_id=request_id,
            chat_id=message.chat_id,
            user_message_id=message.id,
            triggering_user_id=message.sender_id,
            role_code=role_code or "",
            status="pending",
        )
        try:
            payload = {
                "request_id": str(request_id),
                "chat_id": str(message.chat_id),
                "user_message_id": str(message.id),
                "triggering_user_id": str(message.sender_id),
                "responder_id": str(responder_id),
                "strip_username": strip_username,
                "role_code": role_code or "",
            }
            if invocation_kind_override is not None:
                payload["invocation_kind_override"] = invocation_kind_override
            await self.agent_run_storage.create(run)
            await self.kafka_producer.send(
                Config.KAFKA_TOPIC_AGENT_REQUESTS,
                payload,
                key=str(message.chat_id),
            )
            return request_id
        except Exception as e:
            logger.error(f"Failed to enqueue agent run: {e}")
            return None

    async def try_auto_respond(
        self,
        message: Message,
        chat_id: UUID,
        sender_id: UUID,
    ) -> Optional[Message]:
        """
        Auto-respond if chat has a system user participant.
        Called when message has no @@ mentions.

        Publishes the agent request to agent.requests and returns None — the AI
        message arrives later via chat.events WS broadcast. Returning None does
        NOT mean "no handler"; an agent run may have been enqueued.
        """
        chat = await self.chat_storage.get_by_id(chat_id)
        if not chat:
            return None

        # Pre-check: SUPPORT chat with handoff already done — silent skip.
        # After AI hands off to a human operator, the AI must not auto-respond
        # to subsequent requester messages. The operator owns the conversation.
        if (
            chat.type == ChatType.SUPPORT
            and self._is_support_aware()
            and chat.support_ticket_id is not None
        ):
            ticket = await self.support_ticket_storage.get_by_id(
                chat.support_ticket_id
            )
            if ticket is not None and ticket.ai_handoff_at is not None:
                logger.info(
                    f"try_auto_respond: chat={chat_id} skipped "
                    f"(ticket {ticket.id} already handed off to operator)"
                )
                return None

        # Collect system participants (excluding sender)
        system_users = []
        for pid in chat.participants:
            if pid == sender_id:
                continue
            u = await self.user_storage.get_by_id(pid)
            if u and u.is_system:
                system_users.append(u)

        if not system_users:
            return None

        responder = system_users[0]
        
        invocation_kind_override = (
            "mention" if responder.id not in chat.participants else None
        )

        logger.info(
            f"try_auto_respond: chat={chat_id} responder={responder.id} "
        )
        await self._enqueue_agent_run(
            message=message,
            responder_id=responder.id,
            invocation_kind_override=invocation_kind_override,
            # after it generates the response. See Task 18 follow-up.
        )
        return None

    async def _record_support_ai_response(
        self,
        ticket_id: UUID,
        ai_user_id: UUID,
        response_message_id: UUID,
    ) -> None:
        """Stamp ai_first_response_at (idempotent) + insert AI_RESPONDED audit
        event after a successful AI reply on a SUPPORT chat. Best-effort —
        failures are logged but do not break the AI response flow."""
        try:
            await self.support_ticket_storage.set_ai_first_response(ticket_id)
            await self.support_ticket_event_storage.insert(
                SupportTicketEvent(
                    ticket_id=ticket_id,
                    actor_user_id=ai_user_id,
                    actor_role=SupportTicketActorRole.AI,
                    event_type=SupportTicketEventType.AI_RESPONDED,
                    payload={"message_id": str(response_message_id)},
                )
            )
        except Exception:
            # Best-effort: AI message is already persisted and returned to user.
            # Audit-write failure leaves a traceback for triage but doesn't break
            # the reply path. Watch for these in logs — silent drift means the
            # ticket gets an AI reply without ai_first_response_at stamp + AI_RESPONDED
            # event, which corrupts SLA reporting and the soft-handoff UI hint.
            logger.exception(
                "Failed to record support AI response for ticket=%s", ticket_id,
            )

    async def record_support_first_response(
        self,
        chat_id: UUID,
        ai_user_id: UUID,
        response_message_id: UUID,
    ) -> None:
        """After a successful AI reply, stamp support-ticket SLA (idempotent).
        No-op for non-support chats / when support storages aren't wired."""
        if not self._is_support_aware():
            return
        chat = await self.chat_storage.get_by_id(chat_id)
        if not chat or chat.type != ChatType.SUPPORT or chat.support_ticket_id is None:
            return
        await self._record_support_ai_response(
            ticket_id=chat.support_ticket_id,
            ai_user_id=ai_user_id,
            response_message_id=response_message_id,
        )

    async def process_ai_mentions(
        self,
        message: Message,
        org_id: UUID
    ) -> List[Message]:
        """
        Process @@ mentions in a message and enqueue AI responses.

        Each mention -> AgentRun + Kafka publish to agent.requests; returns an
        empty list. The caller should consult `has_pending_agent_runs` to set
        `agent_pending=true` in the HTTP response.
        """
        ai_mentions = [m for m in message.mentions if m.type == MentionType.AI_ROLE]
        if not ai_mentions:
            return []

        logger.info(
            f"process_ai_mentions: message={message.id} chat={message.chat_id} "
        )

        for mention in ai_mentions:
            await self.chat_storage.set_active_agent(message.chat_id, mention.user_id)
            await self._enqueue_agent_run(
                message=message,
                responder_id=mention.user_id,
                strip_username=mention.username,
            )
        return []

    def has_pending_agent_runs(self, message: Message) -> bool:
        """Does this message trigger any agent work? Used for agent_pending HTTP flag.
        Cheap check — no storage hit, uses in-memory message attrs only."""
        return any(m.type == MentionType.AI_ROLE for m in (message.mentions or []))

    async def generate_response(
        self,
        message: Message,
        responder_id: UUID,
        strip_username: Optional[str] = None,
        invocation_kind_override: Optional[str] = None,
    ) -> Optional[Message]:
        """
        Generate AI response from a responder user.

        Core rule:
        - System user with role → respond using that role
        - System user without role (mirror) → respond using SENDER's role
        - Regular user with role → respond using that role

        Args:
            message: The user message to respond to
            responder_id: User ID of who should respond (system user or regular user)
            strip_username: If set, strip @@username from message content before sending to LLM
            invocation_kind_override: If set, overrides mention/direct detection
        """
        logger.info(
            f"generate_response: message={message.id} responder={responder_id}"
        )

        # Find responder
        responder = await self.user_storage.get_by_id(responder_id)
        if not responder:
            logger.warning(
                f"Responder {responder_id} not found in DB "
                f"(message={message.id} chat={message.chat_id})"
            )
            return None

        # Determine role
        role = await self._resolve_role(responder, message.sender_id)
        if not role:
            # Конкретная причина уже залогирована _resolve_role'ом строкой выше.
            # Здесь — просто добавляем message/chat-контекст для grep'а по chat_id.
            logger.warning(
                f"generate_response aborted: role unresolved for @{getattr(responder, 'username', responder.id)} "
                f"(message={message.id} chat={message.chat_id})"
            )
            return None # TODO: return message that user has no role

        if not role.is_active:
            logger.warning(
                f"Role {role.code or role.id} is inactive "
                f"(responder=@{getattr(responder, 'username', responder.id)} chat={message.chat_id})"
            )
            return None # TODO: return message that user has no active role

        # Build conversation context
        conv_messages = await self._build_conversation(
            message,
            strip_username,
        )
        is_mention_call = self._is_mention_call(message, responder_id)
        invocation_kind = invocation_kind_override or (
            "mention" if is_mention_call else "direct"
        )

        # Generate
        try:
            agent_call = await self._call_llm(
                role,
                conv_messages,
                caller_user_id=message.sender_id,
                callee_user_id=responder_id if invocation_kind == "mention" else None,
                invocation_kind=invocation_kind,
                chat_id=message.chat_id,
                agent_name=responder.username,
            )
            if agent_call is None:
                return None
            agent_result, metadata = agent_call
            if agent_result is None or agent_result.content is None:
                return None

            # Persist via the modal-aware helper so any show_modal tool call
            # surfaces as messages.metadata.modal. Content postprocessing
            # (LaTeX→ASCII) lives in _create_ai_message; persist helper delegates
            # there.
            ai_message = await self.persist_ai_message_with_modal(
                chat_id=message.chat_id,
                sender_id=responder_id,
                agent_result=agent_result,
                metadata=metadata,
                role_id=role.id,
                reply_to_id=message.id,
            )

            logger.info(
                f"AI response from {role.name} ({role.model_name}): "
                f"{len(agent_result.content)} chars, metadata={ai_message.metadata or {}}"
            )
            return ai_message

        except Exception as e:
            logger.error(f"Failed to generate AI response: {e}")
            return None

    async def _resolve_role(self, responder, sender_id: UUID) -> Optional[Role]:
        """
        Resolve which role to use for response.
        Mirror (is_system + no role) → sender's role.
        """
        # username нужен в warning'ах чтобы grep по логу сразу указывал на юзера
        # без необходимости отдельного psql-запроса по UUID.
        responder_label = f"@{responder.username} ({responder.id})" if getattr(responder, 'username', None) else str(responder.id)

        if responder.role_id:
            role = await self.role_storage.get_by_id(responder.role_id)
            if not role:
                logger.warning(f"Role {responder.role_id} not found (responder={responder_label})")
            return role

        if responder.is_system:
            # Mirror: use sender's role
            sender = await self.user_storage.get_by_id(sender_id)
            if not sender or not sender.role_id:
                sender_label = f"@{sender.username}" if sender and getattr(sender, 'username', None) else str(sender_id)
                logger.warning(f"Mirror: sender {sender_label} has no role (responder={responder_label})")
                return None
            role = await self.role_storage.get_by_id(sender.role_id)
            if not role:
                logger.warning(f"Mirror: role {sender.role_id} of sender @{sender.username} not found")
            return role

        logger.warning(f"User {responder_label} has no role assigned — @@-mention silently dropped")
        return None

    def _is_mention_call(self, message: Message, responder_id: UUID) -> bool:
        """True when this response is caused by an AI-role mention of responder_id."""
        return any(
            mention.type == MentionType.AI_ROLE and mention.user_id == responder_id
            for mention in (message.mentions or [])
        )

    async def _call_llm(
        self,
        role: Role,
        conv_messages: List[dict],
        caller_user_id: UUID,
        callee_user_id: Optional[UUID] = None,
        invocation_kind: str = "direct",
        chat_id: Optional[UUID] = None,
        agent_name: Optional[str] = None,
    ) -> Optional[tuple[AgentResult, dict]]:
        """Call LLM via AgentExecutor.

        Returns (AgentResult, metadata). Returns None only on executor-level
        error or missing executor — content-empty results are returned through
        so the caller can decide.
        """
        if not self.agent_executor:
            logger.error("AIService has no agent_executor — cannot generate response")
            return None
        result, metadata  = await self.agent_executor.execute(
            role=role,
            messages=conv_messages,
            temperature=0.3,
            caller_user_id=caller_user_id,
            callee_user_id=callee_user_id,
            invocation_kind=invocation_kind,
            chat_id=chat_id,
            agent_name=agent_name,
        )
        
        if result.finish_reason == "error":
            logger.error(f"Agent error: {result.error}")
            # TODO: include into error message "Разработчикам уже отправлен автоматический технический отчет об ошибке" once automatic report storage is implemented
            if result.error and "ContextWindowExceededError" in result.error:
                result.content = (
                    "Ой, эта задача оказалась слишком большой для меня — "
                    "я просто не могу удержать всё это в голове за один раз. "
                    "Давайте попробуем разбить запрос на несколько этапов и выполнить по очереди."
                )
            else:
                result.content = (
                    "Что-то пошло не так на моей стороне — я столкнулся с технической ошибкой "
                    "и не смог ответить. Попробуйте ещё раз чуть позже, а если не поможет — "
                    "напишите в поддержку, они разберутся."
                )
            return result, {}
        return result, metadata

    async def _resolve_user_name(self, sender_id: UUID) -> str:
        """Resolve username marker for an AI message sender."""
        user = await self.user_storage.get_by_id(sender_id)
        if user and user.username:
            return user.username
        return str(sender_id)

    @staticmethod
    def _wrap_agent_content(agent_name: str, content: str, created_at: Optional[datetime] = None) -> str:
        time_tag = f"<time>{created_at.isoformat()}</time>" if created_at is not None else ""
        return f"{time_tag}<name>{agent_name}</name><content>{content}</content>"

    async def _build_conversation(
        self,
        message: Message,
        strip_username: Optional[str] = None,
        limit: int = 10,
    ) -> List[dict]:
        """Build conversation as list of {"role": str, "content": str} dicts."""
        messages = []

        history = await self.message_storage.list_by_chat(message.chat_id, limit=limit)

        # Cache agent names to avoid redundant DB hits per unique sender.
        agent_name_cache: dict[UUID, str] = {}

        for msg in history:
            if msg.id == message.id:
                continue
            role_name = "assistant" if msg.sender_type == SenderType.AI_ROLE else "user"
            content = self._with_attachment_ids(msg)
        
            if msg.sender_id not in agent_name_cache:
                agent_name_cache[msg.sender_id] = await self._resolve_user_name(msg.sender_id)
            content = self._wrap_agent_content(agent_name_cache[msg.sender_id], content, msg.created_at)
            messages.append({"role": role_name, "content": content})

        # Current message
        content = message.content
        if strip_username:
            content = self._strip_mention(content, strip_username)
        content = self._with_attachment_ids(message, content)
        content = await self._with_image_attachments(message, content)
        messages.append({"role": "user", "content": self._wrap_agent_content("", content, message.created_at)})

        return messages

    def _with_attachment_ids(self, message: Message, content: Optional[str] = None) -> str:
        """Append non-image attached file IDs to message content for agent context."""
        result = message.content if content is None else content
        if not message.attachments:
            return result

        file_ids = []
        for attachment in message.attachments:
            file = attachment.file
            if file is not None and (file.file_type or "").lower() in IMAGE_TYPES:
                continue
            file_ids.append(str(attachment.file_id))
        if not file_ids:
            return result
        return f"{result}\n\nAttached file ids: {', '.join(file_ids)}"

    async def _with_image_attachments(self, message: Message, content: str):
        """Format current message as multimodal content when image attachments exist."""
        if not message.attachments or self.storage_adapter is None:
            return content

        parts = [{"type": "text", "text": content}]
        for attachment in message.attachments:
            file = attachment.file
            if file is None:
                continue
            file_type = (file.file_type or "").lower()
            if file_type not in IMAGE_TYPES:
                continue
            try:
                data = await self.storage_adapter.read(file.storage_key)
                payload_type = "video_url" if file_type == "gif" else "image_url"
                parts.append({
                    "type": payload_type,
                    payload_type: {"url": image_bytes_to_data_url(data, file_type=file_type)},
                })
            except Exception as e:
                fallback_text = (
                    f"(tried to attach image {attachment.file_id}. Appears to be too big and will not be attached)"
                    if isinstance(e, ValueError) and "too big" in str(e).lower()
                    else f"(tried to attach image {attachment.file_id}. Faced errors in process)"
                )
                parts.append({"type": "text", "text": fallback_text})
                logger.warning(
                    "Failed to attach image %s to AI conversation: %s",
                    attachment.file_id,
                    e,
                    exc_info=True,
                )

        return parts if len(parts) > 1 else content

    def _strip_mention(self, content: str, username: str) -> str:
        """Remove @@username from message content."""
        pattern = re.compile(rf'@@{re.escape(username)}\s*', re.IGNORECASE)
        cleaned = pattern.sub('', content).strip()
        return cleaned if cleaned else content

    # Math markdown stuff
    _LATEX_REPLACEMENTS = {
        r"$\rightarrow$": "→",
        r"$\leftarrow$": "←",
        r"$\times$": "×",
        r"$\sqrt": "√",
        # Greek lowercase
        r"$\alpha$": "α",
        r"$\beta$": "β",
        r"$\gamma$": "γ",
        r"$\delta$": "δ",
        r"$\epsilon$": "ε",
        r"$\zeta$": "ζ",
        r"$\eta$": "η",
        r"$\theta$": "θ",
        r"$\iota$": "ι",
        r"$\kappa$": "κ",
        r"$\lambda$": "λ",
        r"$\mu$": "μ",
        r"$\nu$": "ν",
        r"$\xi$": "ξ",
        r"$\pi$": "π",
        r"$\rho$": "ρ",
        r"$\sigma$": "σ",
        r"$\tau$": "τ",
        r"$\upsilon$": "υ",
        r"$\phi$": "φ",
        r"$\chi$": "χ",
        r"$\psi$": "ψ",
        r"$\omega$": "ω",
        # Greek uppercase
        r"$\Gamma$": "Γ",
        r"$\Delta$": "Δ",
        r"$\Theta$": "Θ",
        r"$\Lambda$": "Λ",
        r"$\Xi$": "Ξ",
        r"$\Pi$": "Π",
        r"$\Sigma$": "Σ",
        r"$\Upsilon$": "Υ",
        r"$\Phi$": "Φ",
        r"$\Psi$": "Ψ",
        r"$\Omega$": "Ω",
    }
    _INLINE_AGENT_RESPONSE_RE = re.compile(
        r"^\s*(?:<time>.*?</time>\s*)?<name>.*?</name>\s*<content>(?P<content>.*)</content>\s*$",
        re.DOTALL,
    )
    _INLINE_AGENT_NAME_PREFIX_RE = re.compile(r"^\s*(?:<time>.*?</time>\s*)?<name>.*?</name>\s*", re.DOTALL)
    _INLINE_CONTENT_PREFIX_RE = re.compile(r"^\s*<content>", re.DOTALL)
    _INLINE_CONTENT_SUFFIX_RE = re.compile(r"</content>\s*$", re.DOTALL)

    @classmethod
    def _postprocess_content(cls, content: str) -> str:
        match = cls._INLINE_AGENT_RESPONSE_RE.match(content)
        if match:
            content = match.group("content")
        else:
            content = cls._INLINE_AGENT_NAME_PREFIX_RE.sub("", content, count=1)
            content = cls._INLINE_CONTENT_PREFIX_RE.sub("", content, count=1)
            content = cls._INLINE_CONTENT_SUFFIX_RE.sub("", content, count=1)

        for latex, ascii_char in cls._LATEX_REPLACEMENTS.items():
            content = content.replace(latex, ascii_char)
        return content

    async def _create_ai_message(
        self,
        chat_id: UUID,
        sender_id: UUID,
        content: str,
        reply_to_id: Optional[UUID] = None,
        metadata: Optional[dict] = None,
    ) -> Message:
        """Create and save AI message.

        `metadata` carries arbitrary JSONB payload — e.g. {"modal": {...}}
        collected from a show_modal tool call.
        """
        from datetime import datetime
        from uuid import uuid4

        content = self._postprocess_content(content)

        message = Message(
            id=uuid4(),
            chat_id=chat_id,
            sender_type=SenderType.AI_ROLE,
            sender_id=sender_id,
            content=content,
            mentions=[],
            reply_to_id=reply_to_id,
            ai_is_valid=None,
            ai_edited=False,
            is_deleted=False,
            metadata=metadata or {},
            created_at=datetime.utcnow(),
            updated_at=datetime.utcnow(),
        )

        created = await self.message_storage.create(message)
        await self.chat_storage.update_last_message(chat_id)
        return created

    async def persist_ai_message_with_modal(
        self,
        chat_id: UUID,
        sender_id: UUID,
        agent_result: AgentResult,
        metadata: Optional[dict] = None,
        role_id: Optional[UUID] = None,
        reply_to_id: Optional[UUID] = None,
    ) -> Message:
        """Persist an AI message with metadata collected during the agent run.

        `show_modal` payloads are collected through ToolRuntime by
        AgentExecutor.execute and passed here as `metadata={"modal": ...}`.
        If metadata is empty, behavior is identical to a plain AI-message
        persist.

        `role_id` is currently accepted for forward compat (Task 8 plan API);
        we don't persist it directly here because sender_id already drives the
        role-resolution path via UserStorage. Kept in the signature so future
        callers (e.g. multi-role agent paths) can wire it in without breaking
        this method.
        """
        return await self._create_ai_message(
            chat_id=chat_id,
            sender_id=sender_id,
            content=agent_result.content,
            reply_to_id=reply_to_id,
            metadata=metadata or {},
        )

    async def generate_poll_initial(
        self,
        poll_id: UUID,
        chat_id: UUID,
        responder_id: UUID,
    ) -> Message:
        """Generate first AI greeting in a poll chat.

        Reads poll.task_ids snapshot, bulk-fetches task titles via task_storage,
        calls AgentExecutor with role=poll_interviewer, persists AI message
        in chat.messages and returns it.

        Raises RuntimeError if poll/role/storages not found or LLM returns empty.
        """
        if self.task_poll_storage is None or self.task_storage is None:
            raise RuntimeError("AIService: task_poll_storage / task_storage not wired")

        poll = await self.task_poll_storage.get_by_id(poll_id)
        if poll is None:
            raise RuntimeError(f"poll {poll_id} not found")

        role = await self.role_storage.get_by_code(
            POLL_INTERVIEWER_ROLE_CODE, RUGPT_SYSTEM_ORG_ID,
        )
        if role is None:
            raise RuntimeError(
                f"role '{POLL_INTERVIEWER_ROLE_CODE}' not found in system org "
                f"(did migration 029 run?)"
            )

        # Resolve assignee name
        assignee = await self.user_storage.get_by_id(poll.assignee_user_id)
        assignee_name = (assignee.name or assignee.username) if assignee else "сотрудник"

        # Bulk-fetch task titles
        task_ids_uuid = list(poll.task_ids or [])
        tasks_map = (
            await self.task_storage.get_many_by_ids(task_ids_uuid)
            if task_ids_uuid else {}
        )

        # Build user input listing tasks
        lines = [f"Сотрудник: {assignee_name}.", "Активные задачи на сегодня:"]
        if not tasks_map:
            lines.append("- (список задач пуст)")
        else:
            for tid in task_ids_uuid:
                t = tasks_map.get(tid)
                if t:
                    deadline_str = f" (срок: {t.deadline.strftime('%d.%m.%Y')})" if t.deadline else ""
                    lines.append(f"- {t.title}{deadline_str}")
        lines.append("")
        lines.append(
            "Поприветствуй сотрудника, перечисли задачи и попроси рассказать "
            "про каждую: статус, что изменилось, есть ли проблемы."
        )
        user_input = "\n".join(lines)

        responder = await self.user_storage.get_by_id(responder_id)
        result, metadata  = await self.agent_executor.execute(
            role=role,
            messages=[{"role": "user", "content": user_input}],
            temperature=0.5,
            max_tokens=1024,
            caller_user_id=poll.assignee_user_id,
            invocation_kind="system",
            agent_name=responder.username if responder else role.code,
        )

        if not (result and result.content and result.content.strip()):
            raise RuntimeError("agent_executor returned empty content")

        ai_message = Message(
            chat_id=chat_id,
            sender_id=responder_id,
            sender_type=SenderType.AI_ROLE,
            content=self._postprocess_content(result.content).strip(),
            ai_is_valid=True,
        )
        return await self.message_storage.create(ai_message)

    async def enqueue_poll_initial(
        self,
        poll_id: UUID,
        chat_id: UUID,
        responder_id: UUID,
    ) -> UUID:
        """Create AgentRun(pending) and publish kind='poll_initial' to agent.requests.

        Returns request_id, or raises if Kafka publish failed (after best-effort
        mark_failed on the AgentRun row).

        Note: differs from `_enqueue_agent_run` which returns None on failure —
        poll-flow must fail loudly because there is no user-message fallback path.
        """
        if self.agent_run_storage is None or self.kafka_producer is None:
            raise RuntimeError("AIService: agent_run_storage / kafka_producer not wired")

        request_id = uuid4()
        # AgentRun has request_id (PK), chat_id, status — and optional fields for
        # the legacy mention path (user_message_id / triggering_user_id / role_code).
        # For poll_initial there is no triggering user-message; we record responder
        # as triggering_user_id for traceability and stamp role_code so logs/metrics
        # can split by kind without inspecting the Kafka payload.
        agent_run = AgentRun(
            request_id=request_id,
            chat_id=chat_id,
            triggering_user_id=responder_id,
            role_code=POLL_INTERVIEWER_ROLE_CODE,
            status="pending",
        )
        try:
            await self.agent_run_storage.create(agent_run)
            await self.kafka_producer.send(
                Config.KAFKA_TOPIC_AGENT_REQUESTS,
                {
                    "request_id": str(request_id),
                    "chat_id": str(chat_id),
                    "responder_id": str(responder_id),
                    "kind": "poll_initial",
                    "poll_id": str(poll_id),
                },
                key=str(chat_id),
            )
            return request_id
        except Exception as e:
            logger.error(
                f"enqueue_poll_initial: failed for poll={poll_id}: {e}",
                exc_info=True,
            )
            try:
                await self.agent_run_storage.mark_failed(request_id, str(e))
            except Exception:
                pass
            raise

    async def generate_poll_summary(
        self,
        poll_id: UUID,
        chat_id: UUID,
        responder_id: UUID,
    ) -> Message:
        """Build transcript from chat.messages, call poll_summarizer LLM,
        write result to poll.summary, mark poll completed, persist
        'Отчёт сдан, спасибо.' system message in chat.

        Raises RuntimeError on missing dependencies/poll/role/empty LLM output.
        """
        if self.task_poll_storage is None:
            raise RuntimeError("AIService: task_poll_storage not wired")

        poll = await self.task_poll_storage.get_by_id(poll_id)
        if poll is None:
            raise RuntimeError(f"poll {poll_id} not found")

        role = await self.role_storage.get_by_code(
            POLL_SUMMARIZER_ROLE_CODE, RUGPT_SYSTEM_ORG_ID,
        )
        if role is None:
            raise RuntimeError(
                f"role '{POLL_SUMMARIZER_ROLE_CODE}' not found "
                f"(did migration 029 run?)"
            )

        # Explicit large limit: default is 50 with ORDER BY created_at DESC,
        # which would silently drop the *oldest* messages — losing the AI greeting +
        # task list at the top of the dialog and skewing the summary. 500 is far
        # above realistic poll dialog length (~5-30 turns).
        messages = await self.message_storage.list_by_chat(chat_id, limit=500)
        transcript_lines = []
        for m in messages:
            sender_type_value = (
                m.sender_type.value if hasattr(m.sender_type, "value") else m.sender_type
            )
            role_label = "USER" if sender_type_value == "user" else "ASSISTANT"
            transcript_lines.append(f"{role_label}: {m.content}")
        transcript = "\n".join(transcript_lines) if transcript_lines else "(пусто)"

        user_input = (
            f"Транскрипт диалога:\n\n{transcript}\n\n"
            f"Извлеки сводку по структуре, описанной в системном промпте."
        )

        responder = await self.user_storage.get_by_id(responder_id)
        result, metadata  = await self.agent_executor.execute(
            role=role,
            messages=[{"role": "user", "content": user_input}],
            temperature=0.3,
            max_tokens=2048,
            caller_user_id=poll.assignee_user_id,
            invocation_kind="system",
            agent_name=responder.username if responder else role.code,
        )

        if not (result and result.content and result.content.strip()):
            raise RuntimeError("agent_executor returned empty content")

        summary_text = self._postprocess_content(result.content).strip()

        # Sequential UPDATE pair. If failure between — handler marks agent_run failed,
        # Kafka redelivery will retry idempotently (CAS pending->running guards re-runs).
        await self.task_poll_storage.update_summary(poll_id, summary_text)
        # update_status sets status='completed' and stamps completed_at internally.
        await self.task_poll_storage.update_status(poll_id, "completed")

        sys_message = Message(
            chat_id=chat_id,
            sender_id=responder_id,
            sender_type=SenderType.AI_ROLE,
            content="Отчёт сдан, спасибо.",
            ai_is_valid=True,
        )
        return await self.message_storage.create(sys_message)

    async def enqueue_poll_summary(
        self,
        poll_id: UUID,
        chat_id: UUID,
        responder_id: UUID,
    ) -> UUID:
        """Create AgentRun(pending) and publish kind='poll_summary' to agent.requests.

        Returns request_id, or raises if Kafka publish failed (after best-effort
        mark_failed on the AgentRun row).

        Note: differs from `_enqueue_agent_run` which returns None on failure —
        poll-flow must fail loudly because there is no user-message fallback path.
        """
        if self.agent_run_storage is None or self.kafka_producer is None:
            raise RuntimeError("AIService: agent_run_storage / kafka_producer not wired")

        request_id = uuid4()
        # AgentRun has request_id (PK), chat_id, status — and optional fields for
        # the legacy mention path (user_message_id / triggering_user_id / role_code).
        # For poll_summary there is no triggering user-message; we record responder
        # as triggering_user_id for traceability and stamp role_code so logs/metrics
        # can split by kind without inspecting the Kafka payload.
        agent_run = AgentRun(
            request_id=request_id,
            chat_id=chat_id,
            triggering_user_id=responder_id,
            role_code=POLL_SUMMARIZER_ROLE_CODE,
            status="pending",
        )
        try:
            await self.agent_run_storage.create(agent_run)
            await self.kafka_producer.send(
                Config.KAFKA_TOPIC_AGENT_REQUESTS,
                {
                    "request_id": str(request_id),
                    "chat_id": str(chat_id),
                    "responder_id": str(responder_id),
                    "kind": "poll_summary",
                    "poll_id": str(poll_id),
                },
                key=str(chat_id),
            )
            return request_id
        except Exception as e:
            logger.error(
                f"enqueue_poll_summary: failed for poll={poll_id}: {e}",
                exc_info=True,
            )
            try:
                await self.agent_run_storage.mark_failed(request_id, str(e))
            except Exception:
                pass
            raise

    async def close(self):
        """No LLM client to close — inference goes through AgentExecutor which is owned by EngineService."""
        return None
