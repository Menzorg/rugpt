"""
RuGPT Notification Senders

Delivery channels: Telegram, Email, In-app chat.
"""
from .base_sender import BaseSender, SendResult
from .telegram_sender import TelegramSender
from .email_sender import EmailSender

__all__ = [
    'BaseSender',
    'SendResult',
    'TelegramSender',
    'EmailSender',
]
