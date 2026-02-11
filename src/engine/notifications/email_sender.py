"""
Email Sender

Sends notifications via SMTP using aiosmtplib.
"""
import logging
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from typing import Optional

import aiosmtplib

from .base_sender import BaseSender, SendResult

logger = logging.getLogger("rugpt.notifications.email")


class EmailSender(BaseSender):
    """Send messages via SMTP"""

    def __init__(
        self,
        smtp_host: str = "",
        smtp_port: int = 587,
        smtp_user: str = "",
        smtp_password: str = "",
        from_name: str = "RuGPT",
    ):
        self.smtp_host = smtp_host
        self.smtp_port = smtp_port
        self.smtp_user = smtp_user
        self.smtp_password = smtp_password
        self.from_name = from_name

    async def send(self, config: dict, content: str) -> SendResult:
        """
        Send email notification.

        config must contain 'email'. Optional: 'subject'.
        """
        email_to = config.get("email")
        if not email_to:
            return SendResult(success=False, error="No email in channel config")

        if not self.smtp_host or not self.smtp_user:
            return SendResult(success=False, error="SMTP not configured")

        subject = config.get("subject", "RuGPT Notification")

        try:
            msg = MIMEMultipart("alternative")
            msg["From"] = f"{self.from_name} <{self.smtp_user}>"
            msg["To"] = email_to
            msg["Subject"] = subject

            msg.attach(MIMEText(content, "plain", "utf-8"))

            await aiosmtplib.send(
                msg,
                hostname=self.smtp_host,
                port=self.smtp_port,
                username=self.smtp_user,
                password=self.smtp_password,
                use_tls=False,
                start_tls=True,
            )

            logger.info(f"Email sent to {email_to}")
            return SendResult(success=True)

        except Exception as e:
            logger.error(f"Email send error: {e}")
            return SendResult(success=False, error=str(e))

    async def close(self):
        pass
