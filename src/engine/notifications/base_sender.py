"""
Base Sender

Abstract interface for notification delivery channels.
"""
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Optional


@dataclass
class SendResult:
    """Result of a send attempt"""
    success: bool
    error: Optional[str] = None


class BaseSender(ABC):
    """Abstract notification sender"""

    @abstractmethod
    async def send(self, config: dict, content: str) -> SendResult:
        """
        Send a notification.

        Args:
            config: Channel-specific config (e.g. {"chat_id": "123"} for Telegram)
            content: Message text to send
        Returns:
            SendResult with success flag and optional error message
        """
        ...

    @abstractmethod
    async def close(self):
        """Cleanup resources"""
        ...
