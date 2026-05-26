"""
SupportNotificationService — fan-out and single-target notifications for tickets.

Uses InAppNotificationService (which validates type against an allowlist).
Since "support_*" types are not in the allowlist, we route via:
  type='system' + reference_type='support_ticket' + reference_id=ticket.id
The frontend uses reference_type to render support-specific notifications.
"""

from src.engine.unified_logger import get_logger

from src.engine.config import Config
from src.engine.models.support_ticket import (
    SupportTicket, ClosedByRole,
)

logger = get_logger("services")

class SupportNotificationService:

    def __init__(self, in_app_notification_service, user_storage):
        self.in_app = in_app_notification_service
        self.user_storage = user_storage

    async def notify_new_in_queue(self, ticket: SupportTicket) -> None:
        """Fan-out to all active operators in RuGPT Support org."""
        operators = await self.user_storage.list_by_org(
            Config.RUGPT_SUPPORT_ORG_ID, active_only=True
        )
        for op in operators:
            await self.in_app.create(
                user_id=op.id,
                org_id=op.org_id,
                type="system",
                title=f"Новый тикет в очереди — {ticket.category.value}",
                content=ticket.title,
                reference_type="support_ticket",
                reference_id=ticket.id,
            )

    async def notify_taken(self, ticket: SupportTicket) -> None:
        """Single ping to the requester when an operator takes the ticket."""
        await self.in_app.create(
            user_id=ticket.requester_user_id,
            org_id=ticket.requester_org_id,
            type="system",
            title="Тех. поддержка взяла ваш тикет в работу",
            content=ticket.title,
            reference_type="support_ticket",
            reference_id=ticket.id,
        )

    async def notify_closed(self, ticket: SupportTicket) -> None:
        """Notify the OTHER party. If closed by operator → ping requester.
        If closed by requester → ping assignee (if any).
        """
        if ticket.closed_by_role == ClosedByRole.OPERATOR:
            target_user_id = ticket.requester_user_id
            target_org_id = ticket.requester_org_id
            title = "Тех. поддержка закрыла ваш тикет"
        elif ticket.closed_by_role == ClosedByRole.REQUESTER:
            if ticket.assignee_user_id is None:
                return  # nothing to notify on operator side
            target_user_id = ticket.assignee_user_id
            target_org_id = Config.RUGPT_SUPPORT_ORG_ID
            title = "Клиент закрыл тикет"
        else:
            # State-machine violation: close_ticket should always set closed_by_role
            # before this notifier is invoked. Surface it as a warning so the upstream
            # bug can be diagnosed instead of silently dropping notifications.
            logger.warning(
                "notify_closed called with closed_by_role=%r on ticket %s — "
                "upstream state-machine bug; no notification will be sent.",
                ticket.closed_by_role, ticket.id,
            )
            return

        await self.in_app.create(
            user_id=target_user_id,
            org_id=target_org_id,
            type="system",
            title=title,
            content=ticket.title,
            reference_type="support_ticket",
            reference_id=ticket.id,
        )

    async def notify_reopened(self, ticket: SupportTicket) -> None:
        """Reopened ticket:
          - no assignee + handed off → back in the operator queue; fan-out.
          - no assignee + never handed off → still AI first-line (a how_to that
            was never escalated); the AI handles it, so do NOT ping operators
            about a ticket `list_queue` won't surface (it requires ai_handoff_at).
          - has assignee → returned to that operator; single ping.
        """
        if ticket.assignee_user_id is None:
            if ticket.ai_handoff_at is None:
                return  # still AI-handled — not in the operator queue
            operators = await self.user_storage.list_by_org(
                Config.RUGPT_SUPPORT_ORG_ID, active_only=True
            )
            for op in operators:
                await self.in_app.create(
                    user_id=op.id,
                    org_id=op.org_id,
                    type="system",
                    title="Тикет переоткрыт и вернулся в очередь",
                    content=ticket.title,
                    reference_type="support_ticket",
                    reference_id=ticket.id,
                )
            return
        await self.in_app.create(
            user_id=ticket.assignee_user_id,
            org_id=Config.RUGPT_SUPPORT_ORG_ID,
            type="system",
            title="Клиент переоткрыл тикет",
            content=ticket.title,
            reference_type="support_ticket",
            reference_id=ticket.id,
        )
