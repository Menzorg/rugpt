"""
SupportTicketService — business logic for tech support tickets.

Status changes (take/close/reopen) write to support_ticket_events (audit
trail) and trigger in-app notification via the injected notification_service.
They do NOT write messages into the chat's message stream — UI is updated
via WS push of the new ticket state, not via in-chat system messages.
"""

from src.engine.unified_logger import get_logger
from datetime import datetime, timedelta
from typing import Optional, Tuple
from uuid import UUID

from ..config import Config
from ..models.chat import Chat, ChatType
from ..models.message import Message, SenderType
from ..models.support_ticket import (
    SupportTicket,
    SupportTicketCategory,
    SupportTicketStatus,
    ClosedByRole,
)
from ..models.support_ticket_event import (
    SupportTicketEvent,
    SupportTicketEventType,
    SupportTicketActorRole,
)
from ..models.user import User

logger = get_logger("services")

class SupportTicketService:
    """Business logic on top of SupportTicketStorage and friends.

    Routing rules at create_ticket time:
      - HOW_TO  → AI (`support_ai`) is added to chat.participants;
                  ai_handoff_at left None; queue is NOT notified.
      - BUG / OTHER → ai_handoff_at stamped immediately; AI is NOT in
                      participants; operator queue is notified.

    Status transitions (take, close, reopen) record an event on
    support_ticket_events (append-only audit trail) and call the
    injected notification_service. They never write a system-style
    message into the chat — the UI sees status changes via WS push of
    the ticket state, not via in-chat system messages.
    """

    def __init__(
        self,
        ticket_storage,
        event_storage,
        chat_storage,
        message_storage,
        user_storage,
        notification_service,
        kafka_producer=None,
        ai_service=None,
    ):
        self.ticket_storage = ticket_storage
        self.event_storage = event_storage
        self.chat_storage = chat_storage
        self.message_storage = message_storage
        self.user_storage = user_storage
        self.notification_service = notification_service
        # kafka_producer: wired here, used for WS ticket_update pushes (later task).
        self.kafka_producer = kafka_producer
        self.ai_service = ai_service

    # ============================================
    # create
    # ============================================

    async def create_ticket(
        self,
        requester: User,
        category: SupportTicketCategory,
        initial_message: str,
    ) -> Tuple[SupportTicket, Chat]:
        """Create ticket + chat + first user message.

        Routing per category:
          - HOW_TO:  AI in chat, no handoff yet, no queue notify.
          - BUG / OTHER: handoff stamped, queue notified.
        """
        title = (initial_message or "").strip().split("\n")[0][:200] or "(без темы)"
        now = datetime.utcnow()

        # TODO(atomicity): create_ticket spans 5 storage writes (ticket → chat →
        # message → event) on separate connections. A crash mid-flow can leave
        # an OPEN ticket without a chat (orphan). Fix requires threading a
        # transaction through the storages — out of scope for this task.
        # See https://internal-tracker/issues/<TBD>.
        ticket = SupportTicket(
            requester_user_id=requester.id,
            requester_org_id=requester.org_id,
            category=category,
            status=SupportTicketStatus.OPEN,
            title=title,
            ai_handoff_at=None if category == SupportTicketCategory.HOW_TO else now,
        )
        ticket = await self.ticket_storage.create(ticket)

        # Build chat participants.
        participants = [requester.id]
        if category == SupportTicketCategory.HOW_TO:
            support_ai = await self.user_storage.get_by_username(
                Config.SUPPORT_AI_USERNAME, Config.SYSTEM_ORG_ID,
            )
            if support_ai is not None:
                participants.append(support_ai.id)
            else:
                logger.warning(
                    "support_ai system user not found in org %s — proceeding without AI",
                    Config.SYSTEM_ORG_ID,
                )

        chat = Chat(
            org_id=requester.org_id,
            type=ChatType.SUPPORT,
            participants=participants,
            created_by=requester.id,
            support_ticket_id=ticket.id,
        )
        chat = await self.chat_storage.create(chat)

        # First user message.
        first_message = await self._add_user_message(chat, requester, initial_message)

        # Audit event.
        await self._record_event(
            ticket.id,
            requester.id,
            SupportTicketActorRole.REQUESTER,
            SupportTicketEventType.CREATED,
            {"category": category.value},
        )

        if category == SupportTicketCategory.HOW_TO:
            # The opening message is inserted directly (bypassing the send_message
            # route, the only other place try_auto_respond runs), so the AI must be
            # kicked off here or it never replies to the first message. No-op if
            # ai_service isn't wired (tests / sync fallback without AI).
            if self.ai_service is not None:
                await self.ai_service.try_auto_respond(
                    first_message, chat.id, requester.id,
                )
        else:
            # BUG / OTHER: immediate handoff → notify operator queue.
            await self.notification_service.notify_new_in_queue(ticket)

        logger.info(
            "create_ticket: id=%s category=%s requester=%s chat=%s",
            ticket.id, category.value, requester.id, chat.id,
        )
        return ticket, chat

    # ============================================
    # escalate (AI -> human)
    # ============================================

    async def escalate(self, ticket_id: UUID, by_user: User) -> SupportTicket:
        """Client clicks 'Call operator': mark ai_handoff_at + notify queue.

        Idempotent: if ai_handoff_at is already set, returns the ticket
        without re-notifying the queue.
        """
        ticket = await self.ticket_storage.get_by_id(ticket_id)
        if ticket is None:
            raise ValueError(f"ticket {ticket_id} not found")
        if ticket.requester_user_id != by_user.id:
            raise PermissionError("only requester can escalate")
        if ticket.status == SupportTicketStatus.CLOSED:
            raise ValueError("cannot escalate a closed ticket")
        if ticket.ai_handoff_at is not None:
            return ticket  # already handed off — no-op

        await self.ticket_storage.set_ai_handoff(ticket_id)
        await self._record_event(
            ticket_id,
            by_user.id,
            SupportTicketActorRole.REQUESTER,
            SupportTicketEventType.AI_HANDOFF,
            {},
        )
        ticket = await self.ticket_storage.get_by_id(ticket_id)
        await self.notification_service.notify_new_in_queue(ticket)
        logger.info("escalate: ticket=%s by=%s", ticket_id, by_user.id)
        return ticket

    # ============================================
    # take (operator -> ticket)
    # ============================================

    async def take_ticket(self, ticket_id: UUID, operator: User) -> SupportTicket:
        """Operator from RuGPT Support takes a ticket from the queue.

        Atomic CAS in storage layer guarantees exactly-one-winner under race.
        """
        if operator.org_id != Config.RUGPT_SUPPORT_ORG_ID:
            raise PermissionError("only RuGPT Support operators can take tickets")

        taken = await self.ticket_storage.take(ticket_id, operator.id)
        if taken is None:
            raise ValueError("ticket already taken or not in open state")

        # Add operator to chat.participants (idempotent at storage level).
        chat = await self.chat_storage.get_by_support_ticket(ticket_id)
        if chat is not None:
            await self.chat_storage.add_participant(chat.id, operator.id)

        await self._record_event(
            ticket_id,
            operator.id,
            SupportTicketActorRole.OPERATOR,
            SupportTicketEventType.TAKEN,
            {},
        )
        await self.notification_service.notify_taken(taken)
        await self._publish_ticket_update(taken)
        logger.info("take_ticket: ticket=%s operator=%s", ticket_id, operator.id)
        return taken

    # ============================================
    # close
    # ============================================

    async def close_ticket(self, ticket_id: UUID, by_user: User) -> SupportTicket:
        """Either party can close. Records who closed (requester / operator)."""
        ticket = await self.ticket_storage.get_by_id(ticket_id)
        if ticket is None:
            raise ValueError(f"ticket {ticket_id} not found")

        if by_user.id == ticket.requester_user_id:
            role = ClosedByRole.REQUESTER
            actor_role = SupportTicketActorRole.REQUESTER
        elif by_user.org_id == Config.RUGPT_SUPPORT_ORG_ID:
            role = ClosedByRole.OPERATOR
            actor_role = SupportTicketActorRole.OPERATOR
        else:
            raise PermissionError("not authorized to close this ticket")

        closed = await self.ticket_storage.close_ticket(ticket_id, by_user.id, role)
        if closed is None:
            raise ValueError("ticket already closed or not in closeable state")

        await self._record_event(
            ticket_id,
            by_user.id,
            actor_role,
            SupportTicketEventType.CLOSED,
            {"by_role": role.value},
        )
        await self.notification_service.notify_closed(closed)
        await self._publish_ticket_update(closed)
        logger.info(
            "close_ticket: ticket=%s by=%s role=%s",
            ticket_id, by_user.id, role.value,
        )
        return closed

    # ============================================
    # reopen
    # ============================================

    async def reopen_if_within_window(
        self,
        ticket_id: UUID,
        by_user: Optional[User] = None,
    ) -> Optional[SupportTicket]:
        """Reopen a closed ticket if within Config.SUPPORT_REOPEN_WINDOW_DAYS.

        `by_user` is the actor whose action triggered the reopen (e.g. the
        sender of a new message in the closed chat). Used for audit trail
        attribution. If None, the event is recorded as SYSTEM-actor with
        the requester's id for traceability.

        Returns the reopened ticket on success, None if not closed or out of window.
        """
        ticket = await self.ticket_storage.get_by_id(ticket_id)
        if ticket is None or ticket.status != SupportTicketStatus.CLOSED:
            return None
        if ticket.closed_at is None:
            return None
        deadline = ticket.closed_at + timedelta(days=Config.SUPPORT_REOPEN_WINDOW_DAYS)
        if datetime.utcnow() > deadline:
            return None

        reopened = await self.ticket_storage.reopen(ticket_id)
        if reopened is None:
            return None

        # Determine actor for audit attribution
        if by_user is None:
            actor_id = ticket.requester_user_id
            actor_role = SupportTicketActorRole.SYSTEM
        elif by_user.id == ticket.requester_user_id:
            actor_id = by_user.id
            actor_role = SupportTicketActorRole.REQUESTER
        elif by_user.org_id == Config.RUGPT_SUPPORT_ORG_ID:
            actor_id = by_user.id
            actor_role = SupportTicketActorRole.OPERATOR
        else:
            # Defensive — unknown actor, attribute to system + requester id
            actor_id = ticket.requester_user_id
            actor_role = SupportTicketActorRole.SYSTEM

        await self._record_event(
            ticket_id, actor_id, actor_role,
            SupportTicketEventType.REOPENED, {},
        )
        await self.notification_service.notify_reopened(reopened)
        await self._publish_ticket_update(reopened)
        logger.info("reopen_if_within_window: ticket=%s reopened", ticket_id)
        return reopened

    # ============================================
    # route hook: incoming message (auto-reopen / archived guard)
    # ============================================

    async def handle_incoming_message(self, chat, sender_id: UUID) -> None:
        """Pre-send hook called by the chat route before persisting a user message.

        Behavior:
        - For SUPPORT chats with a CLOSED ticket within the reopen window:
          reopens the ticket (+ event + notification) and lets the message through.
        - For SUPPORT chats with a CLOSED ticket past the reopen window:
          raises HTTPException(403) — frontend renders "create a new ticket".
        - For all other chats (DIRECT/TASK/PROJECT, or SUPPORT with no closed
          ticket): silent no-op.

        Args:
            chat: The Chat object from chat_storage.get_by_id (already fetched
                by the route handler).
            sender_id: UUID of the user sending the message.

        Raises:
            HTTPException(403): if the ticket is archived (closed past the
                reopen window).
        """
        from fastapi import HTTPException

        if chat.type != ChatType.SUPPORT or chat.support_ticket_id is None:
            return  # not a support-ticket chat — no hook activity

        ticket = await self.ticket_storage.get_by_id(chat.support_ticket_id)
        if ticket is None or ticket.status != SupportTicketStatus.CLOSED:
            return  # not closed — proceed normally

        # Resolve sender for audit attribution
        sender = await self.user_storage.get_by_id(sender_id)

        reopened = await self.reopen_if_within_window(
            chat.support_ticket_id, by_user=sender,
        )
        if reopened is None:
            raise HTTPException(
                status_code=403,
                detail="Тикет архивирован, создайте новый",
            )
        # reopened is not None → ticket is now in_progress, message can proceed

    # ============================================
    # internal helpers
    # ============================================

    async def _publish_ticket_update(self, ticket) -> None:
        """Publish ticket-state change to chat.events so NestJS broadcasts it to
        the ticket's chat room (live status for both parties). No-op without
        kafka_producer (sync/test mode); best-effort on failure."""
        if self.kafka_producer is None or ticket is None:
            return
        chat = await self.chat_storage.get_by_support_ticket(ticket.id)
        if chat is None:
            return
        try:
            await self.kafka_producer.send(
                Config.KAFKA_TOPIC_CHAT_EVENTS,
                {
                    "kind": "ticket_update",
                    "chat_id": str(chat.id),
                    "ticket": ticket.to_dict(),
                },
                key=str(chat.id),
            )
        except Exception:
            logger.exception(
                "Failed to publish ticket_update for ticket=%s", ticket.id,
            )

    async def _record_event(
        self,
        ticket_id: UUID,
        actor_id: UUID,
        actor_role: SupportTicketActorRole,
        event_type: SupportTicketEventType,
        payload: dict,
    ) -> None:
        event = SupportTicketEvent(
            ticket_id=ticket_id,
            actor_user_id=actor_id,
            actor_role=actor_role,
            event_type=event_type,
            payload=payload,
        )
        await self.event_storage.insert(event)

    async def _add_user_message(
        self, chat: Chat, sender: User, content: str,
    ) -> Message:
        """Insert the very first client message into the support chat.

        Subsequent messages flow through normal chat APIs (chat_service.send_message
        or REST). Status transitions (take/close/reopen) are NOT messages — they
        are recorded as events and signalled via WS push of ticket state.
        """
        msg = Message(
            chat_id=chat.id,
            sender_type=SenderType.USER,
            sender_id=sender.id,
            content=content,
            ai_is_valid=True,  # human messages are auto-valid
        )
        created = await self.message_storage.create(msg)
        await self.chat_storage.update_last_message(chat.id)
        return created
