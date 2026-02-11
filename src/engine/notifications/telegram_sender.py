"""
Telegram Sender

Sends notifications via Telegram Bot API using httpx.
"""
import logging
from typing import Optional

import httpx

from .base_sender import BaseSender, SendResult

logger = logging.getLogger("rugpt.notifications.telegram")

TELEGRAM_API_BASE = "https://api.telegram.org/bot{token}"


class TelegramSender(BaseSender):
    """Send messages via Telegram Bot API"""

    def __init__(self, bot_token: str):
        self.bot_token = bot_token
        self.api_base = TELEGRAM_API_BASE.format(token=bot_token)
        self._client: Optional[httpx.AsyncClient] = None

    def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(timeout=30.0)
        return self._client

    async def send(self, config: dict, content: str) -> SendResult:
        """
        Send message to Telegram chat.

        config must contain 'chat_id'.
        """
        chat_id = config.get("chat_id")
        if not chat_id:
            return SendResult(success=False, error="No chat_id in channel config")

        if not self.bot_token:
            return SendResult(success=False, error="TELEGRAM_BOT_TOKEN not configured")

        try:
            client = self._get_client()
            response = await client.post(
                f"{self.api_base}/sendMessage",
                json={
                    "chat_id": chat_id,
                    "text": content,
                    "parse_mode": "Markdown",
                },
            )

            if response.status_code == 200:
                data = response.json()
                if data.get("ok"):
                    logger.info(f"Telegram message sent to chat_id={chat_id}")
                    return SendResult(success=True)
                else:
                    err = data.get("description", "Unknown Telegram error")
                    logger.error(f"Telegram API error: {err}")
                    return SendResult(success=False, error=err)
            else:
                err = f"HTTP {response.status_code}: {response.text[:200]}"
                logger.error(f"Telegram request failed: {err}")
                return SendResult(success=False, error=err)

        except Exception as e:
            logger.error(f"Telegram send error: {e}")
            return SendResult(success=False, error=str(e))

    async def get_bot_info(self) -> Optional[dict]:
        """Get bot info (for health checks)"""
        try:
            client = self._get_client()
            response = await client.get(f"{self.api_base}/getMe")
            if response.status_code == 200:
                data = response.json()
                if data.get("ok"):
                    return data["result"]
        except Exception as e:
            logger.error(f"Failed to get bot info: {e}")
        return None

    async def close(self):
        if self._client and not self._client.is_closed:
            await self._client.aclose()
            self._client = None
