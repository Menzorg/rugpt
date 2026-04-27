"""
Chat Routes

API endpoints for chats and messages.
"""
from typing import List, Optional
from uuid import UUID

from fastapi import APIRouter, HTTPException, Depends, Query
from pydantic import BaseModel

from .auth import get_current_user
from ..services.engine_service import get_engine_service, EngineService

router = APIRouter(prefix="/api/v1/chats", tags=["chats"])


# Request/Response models

class CreateDirectChatRequest(BaseModel):
    other_user_id: UUID


class SendMessageRequest(BaseModel):
    content: str
    reply_to_id: Optional[UUID] = None


class ValidateMessageRequest(BaseModel):
    edited_content: Optional[str] = None


class RejectMessageRequest(BaseModel):
    correction_text: str


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

    class Config:
        from_attributes = True


# Dependency
def get_engine() -> EngineService:
    return get_engine_service()


# Endpoints

@router.get("/my", response_model=List[ChatResponse])
async def list_my_chats(
    user_id: UUID,  # In real app, get from JWT
    type: Optional[str] = Query(None, description="direct | task | project"),
    engine: EngineService = Depends(get_engine)
):
    """List current user's chats. Optional ?type filter."""
    if type is not None and type not in ("direct", "task", "project"):
        raise HTTPException(status_code=400, detail="Invalid chat type")
    chats = await engine.chat_service.list_user_chats(user_id, chat_type=type)
    return [ChatResponse(**chat.to_dict()) for chat in chats]


@router.post("/direct", response_model=ChatResponse)
async def create_direct_chat(
    request: CreateDirectChatRequest,
    user_id: UUID,  # In real app, get from JWT
    org_id: UUID,
    engine: EngineService = Depends(get_engine)
):
    """Create direct chat with another user"""
    # Visibility check
    if not await engine.department_service.check_visible(
        user_id, request.other_user_id, org_id,
    ):
        raise HTTPException(status_code=403, detail="User not visible")

    chat = await engine.chat_service.create_direct_chat(
        user_id, request.other_user_id, org_id
    )
    return ChatResponse(**chat.to_dict())


@router.get("/{chat_id}", response_model=ChatResponse)
async def get_chat(
    chat_id: UUID,
    engine: EngineService = Depends(get_engine)
):
    """Get chat by ID"""
    chat = await engine.chat_service.get_chat(chat_id)
    if not chat:
        raise HTTPException(status_code=404, detail="Chat not found")
    return ChatResponse(**chat.to_dict())


@router.post("/{chat_id}/participants/{participant_id}")
async def add_participant(
    chat_id: UUID,
    participant_id: UUID,
    user_id: UUID,  # In real app, get from JWT
    org_id: UUID,
    engine: EngineService = Depends(get_engine)
):
    """Add participant to chat"""
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
    engine: EngineService = Depends(get_engine)
):
    """Remove participant from chat"""
    success = await engine.chat_service.remove_participant(chat_id, participant_id)
    if not success:
        raise HTTPException(status_code=400, detail="Could not remove participant")
    return {"success": True}


@router.delete("/{chat_id}")
async def archive_chat(
    chat_id: UUID,
    engine: EngineService = Depends(get_engine)
):
    """Archive chat"""
    success = await engine.chat_service.archive_chat(chat_id)
    if not success:
        raise HTTPException(status_code=400, detail="Could not archive chat")
    return {"success": True}


# Message endpoints

@router.get("/{chat_id}/messages", response_model=List[MessageResponse])
async def list_messages(
    chat_id: UUID,
    user_id: UUID,  # viewer — needed to compute per-viewer reference accessibility
    limit: int = 50,
    before_id: Optional[UUID] = None,
    engine: EngineService = Depends(get_engine)
):
    """List messages in chat. Response includes a `references` field per message
    with per-viewer resolution of !<task_uuid> and !!<project_uuid>."""
    messages = await engine.chat_service.list_messages(chat_id, limit, before_id)

    actor = await engine.user_storage.get_by_id(user_id)
    refs_by_msg = {}
    if actor is not None and messages:
        refs_by_msg = await engine.reference_service.resolve_batch(
            [(m.id, m.content or "") for m in messages], actor,
        )

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
    user_id: UUID,  # In real app, get from JWT
    org_id: UUID,
    engine: EngineService = Depends(get_engine)
):
    """Send message to chat"""
    # Pre-send hook: support-ticket reopen-on-message + archived-ticket guard.
    # No-op for non-SUPPORT chats; raises HTTPException(403) for archived tickets.
    chat = await engine.chat_service.get_chat(chat_id)
    if chat is None:
        raise HTTPException(status_code=404, detail="Chat not found")
    if engine.support_ticket_service is not None:
        await engine.support_ticket_service.handle_incoming_message(chat, user_id)

    # Parse mentions
    mentions = await engine.mention_service.resolve_mentions(request.content, org_id, sender_id=user_id)

    # Send user message
    message = await engine.chat_service.send_message(
        chat_id=chat_id,
        sender_id=user_id,
        content=request.content,
        mentions=mentions,
        reply_to_id=request.reply_to_id,
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
    engine: EngineService = Depends(get_engine)
):
    """Validate AI message"""
    message = await engine.chat_service.validate_ai_message(
        message_id, request.edited_content
    )
    if not message:
        raise HTTPException(status_code=404, detail="Message not found or not AI message")
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


@router.get("/pending-review", response_model=List[MessageResponse])
async def get_pending_review_messages(
    user_id: UUID,  # In real app, get from JWT
    engine: EngineService = Depends(get_engine)
):
    """Get AI messages pending review by user (ai_is_valid IS NULL)"""
    messages = await engine.chat_service.get_pending_review_messages(user_id)
    return [MessageResponse(**msg.to_dict()) for msg in messages]


# Keep old endpoint for backward compatibility
@router.get("/unvalidated", response_model=List[MessageResponse])
async def get_unvalidated_messages(
    user_id: UUID,
    engine: EngineService = Depends(get_engine)
):
    """Deprecated: use /pending-review instead"""
    messages = await engine.chat_service.get_pending_review_messages(user_id)
    return [MessageResponse(**msg.to_dict()) for msg in messages]


class CorrectionRuleResponse(BaseModel):
    id: str
    role_id: str
    org_id: str
    original_message_id: str
    ai_message_id: str
    chat_id: str
    user_question: str
    ai_answer: str
    correction_text: str
    rule_text: Optional[str]
    created_by_user_id: str
    is_active: bool
    created_at: str
    updated_at: str

    class Config:
        from_attributes = True


@router.post("/messages/{message_id}/reject", response_model=CorrectionRuleResponse)
async def reject_message(
    message_id: UUID,
    request: RejectMessageRequest,
    user_id: UUID,  # In real app, get from JWT
    engine: EngineService = Depends(get_engine),
    current_user: dict = Depends(get_current_user)
):
    """Reject AI message and create correction rule"""
    if not current_user["is_admin"]:
        raise HTTPException(status_code=403, detail="Admin access required")
    
    rule = await engine.correction_rule_service.reject_and_create_rule(
        ai_message_id=message_id,
        user_id=user_id,
        correction_text=request.correction_text,
    )
    return CorrectionRuleResponse(**rule.to_dict())
