from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from src.engine.models.message import Message, SenderType
from src.engine.models.role import Role
from src.engine.models.user import User
from src.engine.services.ai_service import AIService


@pytest.mark.asyncio
async def test_build_conversation_wraps_ai_history_with_role_code():
    role_id = uuid4()
    ai_sender_id = uuid4()
    chat_id = uuid4()

    ai_message = Message(
        chat_id=chat_id,
        sender_type=SenderType.AI_ROLE,
        sender_id=ai_sender_id,
        content="Привет",
    )
    current_message = Message(
        chat_id=chat_id,
        sender_type=SenderType.USER,
        sender_id=uuid4(),
        content="Что дальше?",
    )

    user_storage = AsyncMock()
    user_storage.get_by_id = AsyncMock(
        return_value=User(id=ai_sender_id, username="display_user", role_id=role_id)
    )
    role_storage = AsyncMock()
    role_storage.get_by_id = AsyncMock(
        return_value=Role(id=role_id, name="Display Agent", code="support_agent")
    )
    message_storage = AsyncMock()
    message_storage.list_by_chat = AsyncMock(return_value=[ai_message, current_message])

    service = AIService(
        role_storage=role_storage,
        user_storage=user_storage,
        chat_storage=AsyncMock(),
        message_storage=message_storage,
    )

    messages = await service._build_conversation(current_message)

    assert messages[0] == {
        "role": "assistant",
        "content": "<name>support_agent</name><content>Привет</content>",
    }


def test_postprocess_content_removes_inline_agent_wrapper():
    assert (
        AIService._postprocess_content(
            "\n<name>support_agent</name><content>Готово.</content>\n"
        )
        == "Готово."
    )


def test_postprocess_content_removes_partial_edge_tags_only():
    assert (
        AIService._postprocess_content(
            "<name>support_agent</name><content>Готово.\n\nСпасибо.</content>"
        )
        == "Готово.\n\nСпасибо."
    )
    assert (
        AIService._postprocess_content("Текст с внутренним <name>x</name> тегом")
        == "Текст с внутренним <name>x</name> тегом"
    )
