"""
Chat Routes

API endpoints for chats and messages.
"""
import asyncio
import logging
from typing import List, Optional
from uuid import UUID

from fastapi import APIRouter, HTTPException, Depends, Query
from pydantic import BaseModel

from .auth import get_current_user
from ..constants import RAG_COMPATIBLE_TYPES
from ..services.engine_service import get_engine_service, EngineService

logger = logging.getLogger("rugpt.routes.chats")

router = APIRouter(prefix="/api/v1/chats", tags=["chats"])


# Request/Response models

class CreateDirectChatRequest(BaseModel):
    other_user_id: UUID


class SendMessageRequest(BaseModel):
    content: str
    reply_to_id: Optional[UUID] = None
    file_ids: Optional[List[UUID]] = None


class ValidateMessageRequest(BaseModel):
    edited_content: Optional[str] = None


class RejectMessageRequest(BaseModel):
    correction_text: str


class ReplyToMentionRequest(BaseModel):
    content: str


class MarkReadRequest(BaseModel):
    message_id: UUID


class ChatResponse(BaseModel):
    id: str
    org_id: str
    type: str
    name: Optional[str]
    participants: List[str]
    created_by: Optional[str]
    task_id: Optional[str] = None
    project_id: Optional[str] = None
    is_active: bool
    created_at: str
    updated_at: str
    last_message_at: Optional[str]

    class Config:
        from_attributes = True


class MentionResponse(BaseModel):
    type: str
    user_id: str
    username: str
    position: int


class ReferenceResponse(BaseModel):
    type: str
    id: str
    title: Optional[str] = None
    accessible: bool
    position: int


class AttachmentResponse(BaseModel):
    id: str
    position: int
    original_filename: Optional[str]
    file_size: Optional[int]
    file_type: Optional[str]
    is_deleted: bool
    # ID active clone'а текущего viewer'а (или None если нет). Фронт по нему
    # дёргает индексацию RAG без отдельного запроса. None также если
    # enrichment ещё не выполнен (другие endpoint'ы кроме list_messages).
    cloned_by_me_id: Optional[str] = None
    # rag_status клона: 'indexed' / 'indexing' / 'pending' / 'failed' / ...
    # None — нет клона или endpoint не enrich'ил.
    cloned_by_me_rag_status: Optional[str] = None

    class Config:
        from_attributes = True


class MessageResponse(BaseModel):
    id: str
    chat_id: str
    sender_type: str
    sender_id: str
    content: str
    mentions: List[MentionResponse]
    references: List[ReferenceResponse] = []
    reply_to_id: Optional[str]
    ai_is_valid: Optional[bool]
    ai_edited: bool
    is_deleted: bool
    created_at: str
    updated_at: str
    attachments: List[AttachmentResponse] = []

    class Config:
        from_attributes = True


# Dependency
def get_engine() -> EngineService:
    return get_engine_service()


async def _load_actor(engine: EngineService, current_user: dict):
    """Загрузить актора (User) по проверенной личности из current_user."""
    user = await engine.user_storage.get_by_id(current_user["user_id"])
    if not user:
        raise HTTPException(status_code=401, detail="User not found")
    return user


def _require_chat_manager(chat, user) -> None:
    """Authz-гейт уровня роута: управлять чатом/его составом может только
    создатель чата либо админ той же организации. Проверка живёт в роуте, а не
    в сервисе: сервисные методы (archive/add/remove) переиспользуются фоновыми
    задачами (scheduler, Kafka) без пользовательского контекста.
    """
    if chat.created_by == user.id:
        return
    if user.is_admin and chat.org_id == user.org_id:
        return
    raise HTTPException(status_code=403, detail="Not allowed to manage this chat")


# Endpoints

@router.get("/my", response_model=List[ChatResponse])
async def list_my_chats(
    type: Optional[str] = Query(None, description="direct | task | project"),
    current_user: dict = Depends(get_current_user),
    engine: EngineService = Depends(get_engine)
):
    """List current user's chats. Optional ?type filter."""
    user_id = current_user["user_id"]
    if type is not None and type not in ("direct", "task", "project"):
        raise HTTPException(status_code=400, detail="Invalid chat type")
    chats = await engine.chat_service.list_user_chats(user_id, chat_type=type)
    return [ChatResponse(**chat.to_dict()) for chat in chats]


@router.get("/unread-counts")
async def get_unread_counts(
    current_user: dict = Depends(get_current_user),
    engine: EngineService = Depends(get_engine),
):
    """Bulk: return {chat_id: count} for all chats of the user (count > 0 only)."""
    user_id = current_user["user_id"]
    user = await engine.user_storage.get_by_id(user_id)
    if user is None:
        raise HTTPException(status_code=404, detail="User not found")
    counts = await engine.chat_service.list_unread_counts(
        user_id=user_id, org_id=user.org_id,
    )
    return {str(chat_id): count for chat_id, count in counts.items()}


@router.post("/direct", response_model=ChatResponse)
async def create_direct_chat(
    request: CreateDirectChatRequest,
    current_user: dict = Depends(get_current_user),
    engine: EngineService = Depends(get_engine)
):
    """Create direct chat with another user"""
    user_id = current_user["user_id"]
    org_id = current_user["org_id"]
    # Visibility check
    if not await engine.department_service.check_visible(
        user_id, request.other_user_id, org_id,
    ):
        raise HTTPException(status_code=403, detail="User not visible")

    chat = await engine.chat_service.create_direct_chat(
        user_id, request.other_user_id, org_id
    )
    return ChatResponse(**chat.to_dict())


@router.get("/pending-review", response_model=List[MessageResponse])
async def get_pending_review_messages(
    current_user: dict = Depends(get_current_user),
    engine: EngineService = Depends(get_engine),
):
    """Get AI messages pending review by user (ai_is_valid IS NULL).

    NB: должен быть объявлен ДО `/{chat_id}` — иначе FastAPI сматчит
    'pending-review' как chat_id и упадёт на UUID-валидации (422).
    """
    user_id = current_user["user_id"]
    messages = await engine.chat_service.get_pending_review_messages(user_id)
    return [MessageResponse(**msg.to_dict()) for msg in messages]


@router.get("/reviewed", response_model=List[MessageResponse])
async def get_reviewed_messages(
    limit: int = 50,
    current_user: dict = Depends(get_current_user),
    engine: EngineService = Depends(get_engine),
):
    """Get AI messages already validated/rejected by user (ai_is_valid IS NOT NULL).

    Парный к /pending-review для UI таба «Моя роль → Проверенные».
    Тоже должен быть ДО `/{chat_id}` (см. комментарий выше).
    """
    user_id = current_user["user_id"]
    messages = await engine.chat_service.get_reviewed_messages(user_id, limit)
    return [MessageResponse(**msg.to_dict()) for msg in messages]


# Keep old endpoint for backward compatibility (та же причина с порядком).
@router.get("/unvalidated", response_model=List[MessageResponse])
async def get_unvalidated_messages(
    current_user: dict = Depends(get_current_user),
    engine: EngineService = Depends(get_engine),
):
    """Deprecated: use /pending-review instead"""
    user_id = current_user["user_id"]
    messages = await engine.chat_service.get_pending_review_messages(user_id)
    return [MessageResponse(**msg.to_dict()) for msg in messages]


@router.get("/{chat_id}", response_model=ChatResponse)
async def get_chat(
    chat_id: UUID,
    current_user: dict = Depends(get_current_user),
    engine: EngineService = Depends(get_engine)
):
    """Get chat by ID"""
    chat = await engine.chat_service.get_chat(chat_id)
    if not chat:
        raise HTTPException(status_code=404, detail="Chat not found")
    actor = await _load_actor(engine, current_user)
    if not await engine.chat_service.can_user_access_chat(actor, chat):
        raise HTTPException(status_code=403, detail="Not allowed to access this chat")
    return ChatResponse(**chat.to_dict())


@router.post("/{chat_id}/participants/{participant_id}")
async def add_participant(
    chat_id: UUID,
    participant_id: UUID,
    current_user: dict = Depends(get_current_user),
    engine: EngineService = Depends(get_engine)
):
    """Add participant to chat"""
    user_id = current_user["user_id"]
    org_id = current_user["org_id"]
    chat = await engine.chat_service.get_chat(chat_id)
    if not chat:
        raise HTTPException(status_code=404, detail="Chat not found")
    actor = await _load_actor(engine, current_user)
    _require_chat_manager(chat, actor)
    # Visibility check
    if not await engine.department_service.check_visible(
        user_id, participant_id, org_id,
    ):
        raise HTTPException(status_code=403, detail="User not visible")

    success = await engine.chat_service.add_participant(chat_id, participant_id)
    if not success:
        raise HTTPException(status_code=400, detail="Could not add participant")
    return {"success": True}


@router.delete("/{chat_id}/participants/{participant_id}")
async def remove_participant(
    chat_id: UUID,
    participant_id: UUID,
    current_user: dict = Depends(get_current_user),
    engine: EngineService = Depends(get_engine)
):
    """Remove participant from chat"""
    chat = await engine.chat_service.get_chat(chat_id)
    if not chat:
        raise HTTPException(status_code=404, detail="Chat not found")
    actor = await _load_actor(engine, current_user)
    _require_chat_manager(chat, actor)
    success = await engine.chat_service.remove_participant(chat_id, participant_id)
    if not success:
        raise HTTPException(status_code=400, detail="Could not remove participant")
    return {"success": True}


@router.delete("/{chat_id}")
async def archive_chat(
    chat_id: UUID,
    current_user: dict = Depends(get_current_user),
    engine: EngineService = Depends(get_engine)
):
    """Archive chat"""
    chat = await engine.chat_service.get_chat(chat_id)
    if not chat:
        raise HTTPException(status_code=404, detail="Chat not found")
    actor = await _load_actor(engine, current_user)
    _require_chat_manager(chat, actor)
    success = await engine.chat_service.archive_chat(chat_id)
    if not success:
        raise HTTPException(status_code=400, detail="Could not archive chat")
    return {"success": True}


# Message endpoints

async def _enrich_attachments_cloned_by_me(messages, user_id, engine):
    """Mutates each MessageAttachment in messages, setting cloned_by_me_id to
    the user's clone id (если есть) — фронт юзает id для index-RAG action'а
    без доп. запроса. Батч: один SQL на весь список вложений всех сообщений.
    """
    source_ids = []
    for m in messages:
        for a in m.attachments or []:
            source_ids.append(a.file_id)
    if not source_ids:
        return
    clone_map = await engine.user_file_storage.find_active_clones_by_source(user_id, source_ids)
    for m in messages:
        for a in m.attachments or []:
            entry = clone_map.get(a.file_id)
            if entry:
                a.cloned_by_me_id = entry["id"]
                a.cloned_by_me_rag_status = entry["rag_status"]


@router.get("/{chat_id}/messages", response_model=List[MessageResponse])
async def list_messages(
    chat_id: UUID,
    limit: int = 50,
    before_id: Optional[UUID] = None,
    current_user: dict = Depends(get_current_user),
    engine: EngineService = Depends(get_engine)
):
    """List messages in chat. Response includes a `references` field per message
    with per-viewer resolution of !<task_uuid> and !!<project_uuid>."""
    user_id = current_user["user_id"]  # viewer — for per-viewer reference accessibility
    messages = await engine.chat_service.list_messages(chat_id, limit, before_id)

    actor = await engine.user_storage.get_by_id(user_id)
    refs_by_msg = {}
    if actor is not None and messages:
        refs_by_msg = await engine.reference_service.resolve_batch(
            [(m.id, m.content or "") for m in messages], actor,
        )

    # Per-viewer attachment enrichment: пометить файлы, которые этот юзер уже
    # клонировал «в мои файлы». Фронт скроет кнопку «В мои файлы» для них.
    await _enrich_attachments_cloned_by_me(messages, user_id, engine)

    out = []
    for msg in messages:
        d = msg.to_dict()
        d["references"] = refs_by_msg.get(msg.id, [])
        out.append(MessageResponse(**d))
    return out


class SendMessageResponse(BaseModel):
    """Response for send message.

    ai_responses is kept for backward compat with sync fallback path (Kafka disabled).
    When Kafka-backed async inference is active, ai_responses is empty and the
    client should watch for WS `message` events with sender_type=ai_role.
    agent_pending=True hints the UI to show a typing indicator.
    """
    user_message: MessageResponse
    ai_responses: List[MessageResponse] = []
    agent_pending: bool = False


@router.post("/{chat_id}/messages", response_model=SendMessageResponse)
async def send_message(
    chat_id: UUID,
    request: SendMessageRequest,
    current_user: dict = Depends(get_current_user),
    engine: EngineService = Depends(get_engine)
):
    """Send message to chat"""
    user_id = current_user["user_id"]
    org_id = current_user["org_id"]
    # Pre-send hook: support-ticket reopen-on-message + archived-ticket guard.
    # No-op for non-SUPPORT chats; raises HTTPException(403) for archived tickets.
    chat = await engine.chat_service.get_chat(chat_id)
    if chat is None:
        raise HTTPException(status_code=404, detail="Chat not found")
    if engine.support_ticket_service is not None:
        await engine.support_ticket_service.handle_incoming_message(chat, user_id)

    # Parse mentions
    mentions = await engine.mention_service.resolve_mentions(request.content, org_id, sender_id=user_id)

    if (
        request.file_ids
        and len(request.file_ids) <= 5
        and await engine.chat_storage.is_ai_direct_chat(chat_id)
    ):
        # Kick off RAG indexing for each compatible attachment and collect
        # the futures returned by index_for_rag.  Indexing is idempotent:
        # already-indexed or in-flight files return future=None and are skipped.
        index_futures = []
        for fid in request.file_ids:
            file = await engine.file_service.get(fid)
            if file is not None and file.file_type in RAG_COMPATIBLE_TYPES:
                _, future = await engine.file_service.index_for_rag(fid, user_id)
                if future is not None:
                    index_futures.append(future)

        # Wait for all pending indexing jobs to finish before the message is
        # stored, so the AI's first reply already has access to the content.
        # return_exceptions=True prevents one failure from cancelling the rest.
        if index_futures:
            try:
                await asyncio.gather(*index_futures, return_exceptions=True)
            except Exception as e:
                logger.error("RAG indexing wait failed for chat %s: %s", chat_id, e)

    # Send user message
    message = await engine.chat_service.send_message(
        chat_id=chat_id,
        sender_id=user_id,
        content=request.content,
        
        mentions=mentions,
        reply_to_id=request.reply_to_id,
        file_ids=request.file_ids,
    )

    # Publish to chat.events for real-time WS delivery via NestJS consumer.
    # Same pattern as reply-to-mention / agent_handler — engine является
    # единственным источником истины для broadcast'а пользовательского сообщения.
    if engine.kafka_producer is not None:
        try:
            from ..config import Config
            await engine.kafka_producer.send(
                Config.KAFKA_TOPIC_CHAT_EVENTS,
                {"chat_id": str(chat_id), "message": message.to_dict()},
                key=str(chat_id),
            )
        except Exception as e:
            logger.error(f"Failed to publish user message to Kafka: {e}")

    # In-app notifications для @user-упоминаний (без self-mention).
    # @@-mentions идут отдельным путём ниже через process_ai_mentions.
    sender = await engine.user_storage.get_by_id(user_id)
    sender_label = f"@{sender.username}" if sender else "пользователь"
    for m in mentions:
        if m.type.value == "user" and m.user_id != user_id:
            await engine.in_app_notification_service.create(
                user_id=m.user_id,
                org_id=org_id,
                type="mention",
                title=f"Вас упомянул {sender_label}",
                content=request.content[:200],
                reference_type="message",
                reference_id=message.id,
            )

    # Process @@ mentions -> AI responses (sync path) OR enqueue (async path)
    ai_responses = []
    ai_mentions = [m for m in mentions if m.type.value == "ai_role"]
    agent_pending = False

    if ai_mentions:
        ai_messages = await engine.ai_service.process_ai_mentions(message, org_id)
        # Async mode: process_ai_mentions returns empty list, enqueue happened internally
        if not ai_messages and engine.ai_service._is_async_mode():
            agent_pending = True
        ai_responses = [MessageResponse(**msg.to_dict()) for msg in ai_messages]
    else:
        ai_msg = await engine.ai_service.try_auto_respond(message, chat_id, user_id)
        if ai_msg:
            ai_responses = [MessageResponse(**ai_msg.to_dict())]
        elif engine.ai_service._is_async_mode():
            # Auto-respond path may have enqueued if there's a system user in the chat.
            # We can't cheaply tell if enqueue happened without extra DB lookup; err
            # on the side of showing the pending indicator when async mode is on and
            # the chat has at least one system participant.
            chat = await engine.chat_service.get_chat(chat_id)
            if chat:
                for pid in chat.participants:
                    if pid == user_id:
                        continue
                    u = await engine.user_storage.get_by_id(pid)
                    if u and u.is_system:
                        agent_pending = True
                        break

    return SendMessageResponse(
        user_message=MessageResponse(**message.to_dict()),
        ai_responses=ai_responses,
        agent_pending=agent_pending,
    )


@router.post("/{chat_id}/read", status_code=204)
async def mark_chat_read(
    chat_id: UUID,
    body: MarkReadRequest,
    current_user: dict = Depends(get_current_user),
    engine: EngineService = Depends(get_engine),
):
    """Mark messages in chat as read up to and including message_id."""
    user_id = current_user["user_id"]
    try:
        await engine.chat_service.mark_chat_read(
            chat_id=chat_id, user_id=user_id, message_id=body.message_id,
        )
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except PermissionError as e:
        raise HTTPException(status_code=403, detail=str(e))

    # Publish to chat.events for multi-device sync via NestJS consumer.
    # Старый WS-обработчик слал chat:unread-cleared в комнату user:{reader}
    # (только свои вкладки/устройства, не другим участникам). Consumer
    # ребродкастит то же по user_id читателя.
    if engine.kafka_producer is not None:
        try:
            from ..config import Config
            await engine.kafka_producer.send(
                Config.KAFKA_TOPIC_CHAT_EVENTS,
                {
                    "kind": "unread_cleared",
                    "user_id": str(user_id),
                    "chat_id": str(chat_id),
                },
                key=str(chat_id),
            )
        except Exception as e:
            logger.error(f"Failed to publish unread_cleared to Kafka: {e}")

    return None


@router.get("/messages/{message_id}", response_model=MessageResponse)
async def get_message(
    message_id: UUID,
    engine: EngineService = Depends(get_engine)
):
    """Get message by ID"""
    message = await engine.chat_service.get_message(message_id)
    if not message:
        raise HTTPException(status_code=404, detail="Message not found")
    return MessageResponse(**message.to_dict())


@router.post("/messages/{message_id}/validate", response_model=MessageResponse)
async def validate_message(
    message_id: UUID,
    request: ValidateMessageRequest,
    current_user: dict = Depends(get_current_user),
    engine: EngineService = Depends(get_engine),
):
    """Validate AI message. Identity берётся из device-подписанного current_user
    (Zero Trust: webclient SignatureGuard уже проверил подпись = ключ устройства
    этого user_id), не из голого JWT. Allowed for admin OR owner of the role
    (user_id == ai_message.sender_id).
    """
    user_id = current_user["user_id"]
    target = await engine.chat_service.get_message(message_id)
    if not target:
        raise HTTPException(status_code=404, detail="Message not found")

    actor = await engine.user_storage.get_by_id(user_id)
    if not actor:
        raise HTTPException(status_code=404, detail="User not found")

    is_admin = bool(getattr(actor, "is_admin", False))
    is_role_owner = target.sender_id == user_id
    if not (is_admin or is_role_owner):
        raise HTTPException(status_code=403, detail="Only admin or role owner may validate")

    message = await engine.chat_service.validate_ai_message(
        message_id, request.edited_content
    )
    if not message:
        raise HTTPException(status_code=404, detail="Message not found or not AI message")

    # Publish to chat.events for real-time WS delivery via NestJS consumer.
    # Same pattern as send_message — engine является единственным источником
    # истины для broadcast'а. `kind` маршрутизирует событие в consumer'е
    # (message:validated в комнату чата).
    if engine.kafka_producer is not None:
        try:
            from ..config import Config
            await engine.kafka_producer.send(
                Config.KAFKA_TOPIC_CHAT_EVENTS,
                {
                    "kind": "message_validated",
                    "chat_id": str(message.chat_id),
                    "message": message.to_dict(),
                },
                key=str(message.chat_id),
            )
        except Exception as e:
            logger.error(f"Failed to publish message_validated to Kafka: {e}")

    return MessageResponse(**message.to_dict())


@router.delete("/messages/{message_id}")
async def delete_message(
    message_id: UUID,
    engine: EngineService = Depends(get_engine)
):
    """Delete message"""
    success = await engine.chat_service.delete_message(message_id)
    if not success:
        raise HTTPException(status_code=400, detail="Could not delete message")
    return {"success": True}


class CorrectionRuleResponse(BaseModel):
    """Должна совпадать с CorrectionRule.to_dict() — иначе FastAPI 500
    на response-валидации, ack уйдёт error даже когда правило создалось."""
    id: str
    role_id: str
    mem_id: Optional[str] = None
    src_user_message_id: Optional[str] = None
    src_ai_response_id: Optional[str] = None
    user_correction_text: Optional[str] = None
    extracted_lesson: Optional[str] = None
    is_active: bool = True

    class Config:
        from_attributes = True


@router.post("/messages/{message_id}/reject", response_model=CorrectionRuleResponse)
async def reject_message(
    message_id: UUID,
    request: RejectMessageRequest,
    current_user: dict = Depends(get_current_user),
    engine: EngineService = Depends(get_engine),
):
    """Reject AI message and create correction rule.

    Identity берётся из device-подписанного current_user (прошедшего проверку
    SignatureGuard на webclient), не из голого JWT. Allowed for admin OR owner
    of the role (user_id == ai_message.sender_id) — владелец роли учит свою же
    роль через correction rules.
    """
    user_id = current_user["user_id"]
    target = await engine.chat_service.get_message(message_id)
    if not target:
        raise HTTPException(status_code=404, detail="Message not found")

    actor = await engine.user_storage.get_by_id(user_id)
    if not actor:
        raise HTTPException(status_code=404, detail="User not found")

    is_admin = bool(getattr(actor, "is_admin", False))
    is_role_owner = target.sender_id == user_id
    if not (is_admin or is_role_owner):
        raise HTTPException(status_code=403, detail="Only admin or role owner may reject")

    rule = await engine.correction_rule_service.reject_and_create_rule(
        ai_message_id=message_id,
        user_id=user_id,
        correction_text=request.correction_text,
    )

    # Publish to chat.events for real-time WS delivery via NestJS consumer.
    # chat_id берём из отклонённого сообщения; consumer ребродкастит
    # message:rejected {messageId, rule} в комнату чата.
    if engine.kafka_producer is not None:
        try:
            from ..config import Config
            await engine.kafka_producer.send(
                Config.KAFKA_TOPIC_CHAT_EVENTS,
                {
                    "kind": "message_rejected",
                    "chat_id": str(target.chat_id),
                    "message_id": str(message_id),
                    "rule": rule.to_dict(),
                },
                key=str(target.chat_id),
            )
        except Exception as e:
            logger.error(f"Failed to publish message_rejected to Kafka: {e}")

    return CorrectionRuleResponse(**rule.to_dict())


def _is_mentioned(original, sender) -> bool:
    """True if sender appears in original.mentions, regardless of mention type.

    `mention_service.resolve_mentions` пишет реальный user_id владельца
    и для @user, и для @@user (когда @@ резолвится к человеку, а не к
    системнику). Поэтому хватает простого сравнения user_id, без role_id-логики
    и без обращений к user_storage за дополнительными данными.

    Системные AI-роли типа @@pm резолвятся к system user'ам — их user_id
    не совпадёт с sender.id (который всегда обычный человек), так что
    случай отсекается естественным образом.
    """
    return any(m.user_id == sender.id for m in (original.mentions or []))


@router.post("/messages/{message_id}/reply", response_model=MessageResponse)
async def reply_to_mention(
    message_id: UUID,
    request: ReplyToMentionRequest,
    current_user: dict = Depends(get_current_user),
    engine: EngineService = Depends(get_engine),
):
    """Reply to a mentioning message without joining the chat as participant.

    Гейт прав: sender должен быть упомянут в `original.mentions`.
    Single-use: один реплай на одну (mentioning_message, sender) пару.

    org_id не нужен — извлекается из sender.org_id при необходимости.
    """
    user_id = current_user["user_id"]
    original = await engine.chat_service.get_message(message_id)
    if not original:
        raise HTTPException(status_code=404, detail="Message not found")

    sender = await engine.user_storage.get_by_id(user_id)
    if not sender:
        raise HTTPException(status_code=404, detail="User not found")

    if not _is_mentioned(original, sender):
        raise HTTPException(status_code=403, detail="Not mentioned in this message")

    if await engine.message_storage.find_reply(message_id, sender.id):
        raise HTTPException(status_code=409, detail="Already replied to this mention")

    reply = await engine.chat_service.send_message(
        chat_id=original.chat_id,
        sender_id=sender.id,
        content=request.content,
        reply_to_id=message_id,
    )

    # Publish to chat.events for real-time WS delivery via NestJS consumer.
    # Same pattern as agent_handler — engine
    # является единственным источником истины для broadcast'а.
    if engine.kafka_producer is not None:
        try:
            from ..config import Config
            await engine.kafka_producer.send(
                Config.KAFKA_TOPIC_CHAT_EVENTS,
                {"chat_id": str(reply.chat_id), "message": reply.to_dict()},
                key=str(reply.chat_id),
            )
        except Exception as e:
            logger.error(f"Failed to publish reply-to-mention to Kafka: {e}")

    return MessageResponse(**reply.to_dict())
