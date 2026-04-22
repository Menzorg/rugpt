"""
RuGPT Data Models

Domain models for the RuGPT corporate AI assistant.
"""
from .organization import Organization
from .user import User
from .role import Role
from .chat import Chat, ChatType
from .message import Message, Mention, SenderType, MentionType
from .calendar_event import CalendarEvent
from .notification import NotificationChannel, NotificationLog
from .memory_snapshot import MemorySnapshot
from .correction_rule import CorrectionRule

__all__ = [
    'Organization',
    'User',
    'Role',
    'Chat',
    'ChatType',
    'Message',
    'Mention',
    'SenderType',
    'MentionType',
    'CalendarEvent',
    'NotificationChannel',
    'NotificationLog',
    'MemorySnapshot',
    'CorrectionRule',
]
