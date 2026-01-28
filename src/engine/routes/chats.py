"""
Chat Routes

API endpoints for chats and messages.
"""
from typing import List, Optional
from uuid import UUID

from fastapi import APIRouter, HTTPException, Depends
from pydantic import BaseModel

from ..services.engine_service import get_engine_service, EngineService
from ..models.message import MentionType

router = APIRouter(prefix="/api/v1/chats", tags=["chats"])


# Request/Response models

class CreateDirectChatRequest(BaseModel):
    other_user_id: UUID


class CreateGroupChatRequest(BaseModel):
    name: str
    participant_ids: List[UUID]


class SendMessageRequest(BaseModel):
    content: str
    reply_to_id: Optional[UUID] = None


class ValidateMessageRequest(BaseModel):
    edited_content: Optional[str] = None


class ChatResponse(BaseModel):
    id: str
    org_id: str
    type: str
    name: Optional[str]
    participants: List[str]
    created_by: Optional[str]
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


class MessageResponse(BaseModel):
    id: str
    chat_id: str
    sender_type: str
    sender_id: str
    content: str
    mentions: List[MentionResponse]
    reply_to_id: Optional[str]
    ai_validated: bool
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
    engine: EngineService = Depends(get_engine)
):
    """List current user's chats"""
    chats = await engine.chat_service.list_user_chats(user_id)
    return [ChatResponse(**chat.to_dict()) for chat in chats]


@router.get("/main", response_model=ChatResponse)
async def get_main_chat(
    user_id: UUID,  # In real app, get from JWT
    org_id: UUID,
    engine: EngineService = Depends(get_engine)
):
    """Get or create user's main chat"""
    chat = await engine.chat_service.get_or_create_main_chat(user_id, org_id)
    return ChatResponse(**chat.to_dict())


@router.post("/direct", response_model=ChatResponse)
async def create_direct_chat(
    request: CreateDirectChatRequest,
    user_id: UUID,  # In real app, get from JWT
    org_id: UUID,
    engine: EngineService = Depends(get_engine)
):
    """Create direct chat with another user"""
    chat = await engine.chat_service.create_direct_chat(
        user_id, request.other_user_id, org_id
    )
    return ChatResponse(**chat.to_dict())


@router.post("/group", response_model=ChatResponse)
async def create_group_chat(
    request: CreateGroupChatRequest,
    user_id: UUID,  # In real app, get from JWT
    org_id: UUID,
    engine: EngineService = Depends(get_engine)
):
    """Create group chat"""
    participants = [user_id] + request.participant_ids
    chat = await engine.chat_service.create_group_chat(
        request.name, participants, user_id, org_id
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
    engine: EngineService = Depends(get_engine)
):
    """Add participant to chat"""
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
    limit: int = 50,
    before_id: Optional[UUID] = None,
    engine: EngineService = Depends(get_engine)
):
    """List messages in chat"""
    messages = await engine.chat_service.list_messages(chat_id, limit, before_id)
    return [MessageResponse(**msg.to_dict()) for msg in messages]


class SendMessageResponse(BaseModel):
    """Response for send message including AI responses"""
    user_message: MessageResponse
    ai_responses: List[MessageResponse] = []


@router.post("/{chat_id}/messages", response_model=SendMessageResponse)
async def send_message(
    chat_id: UUID,
    request: SendMessageRequest,
    user_id: UUID,  # In real app, get from JWT
    org_id: UUID,
    engine: EngineService = Depends(get_engine)
):
    """Send message to chat"""
    # Parse mentions
    mentions = await engine.mention_service.resolve_mentions(request.content, org_id)

    # Send user message
    message = await engine.chat_service.send_message(
        chat_id=chat_id,
        sender_id=user_id,
        content=request.content,
        mentions=mentions,
        reply_to_id=request.reply_to_id,
    )

    # Process @@ mentions - trigger AI responses
    ai_responses = []
    ai_mentions = [m for m in mentions if m.type.value == "ai_role"]

    if ai_mentions:
        ai_messages = await engine.ai_service.process_ai_mentions(message, org_id)
        ai_responses = [MessageResponse(**msg.to_dict()) for msg in ai_messages]

    return SendMessageResponse(
        user_message=MessageResponse(**message.to_dict()),
        ai_responses=ai_responses
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


@router.get("/unvalidated", response_model=List[MessageResponse])
async def get_unvalidated_messages(
    user_id: UUID,  # In real app, get from JWT
    engine: EngineService = Depends(get_engine)
):
    """Get AI messages pending validation by user"""
    messages = await engine.chat_service.get_unvalidated_messages(user_id)
    return [MessageResponse(**msg.to_dict()) for msg in messages]
