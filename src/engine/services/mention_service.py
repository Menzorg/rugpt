"""
Mention Service

Parses @ and @@ mentions from message content.
"""
import logging
import re
from typing import List, Optional, Tuple
from uuid import UUID

from ..models.message import Mention, MentionType
from ..storage.user_storage import UserStorage

logger = logging.getLogger("rugpt.services.mention")


class MentionService:
    """Service for parsing and resolving mentions"""

    # Pattern for @@ (AI role) and @ (user) mentions
    # @@ must be checked first as it's more specific
    MENTION_PATTERN = re.compile(r'@@(\w+)|@(\w+)')

    def __init__(self, user_storage: UserStorage):
        self.user_storage = user_storage

    def parse_mentions(self, content: str) -> List[Tuple[MentionType, str, int]]:
        """
        Parse mentions from message content.

        Returns list of (type, username, position)
        """
        mentions = []
        for match in self.MENTION_PATTERN.finditer(content):
            if match.group(1):  # @@ mention (AI role)
                mentions.append((
                    MentionType.AI_ROLE,
                    match.group(1),
                    match.start()
                ))
            elif match.group(2):  # @ mention (user)
                mentions.append((
                    MentionType.USER,
                    match.group(2),
                    match.start()
                ))
        return mentions

    async def resolve_mentions(
        self,
        content: str,
        org_id: UUID,
        sender_id: UUID = None
    ) -> List[Mention]:
        """
        Parse and resolve mentions to user IDs.

        Returns list of Mention objects with resolved user_ids.
        """
        parsed = self.parse_mentions(content)
        mentions = []

        for mention_type, username, position in parsed:
            # First try to find user in the sender's organization
            user = await self.user_storage.get_by_username(username, org_id)

            # Fallback: try system users bound to a role (e.g. @@pm, @@reasoner,
            # @@doc_search). Explicitly skip mirror — mirror (system user without
            # role_id) channels the sender's own role, so allowing it as a
            # mention means observers in a shared chat would see a response that
            # reflects someone else's role and could wrongly validate it. Mirror
            # is only reachable via a direct 1-on-1 chat.
            if not user:
                candidate = await self.user_storage.get_system_user_by_username(username)
                if candidate is not None and candidate.role_id is None:
                    logger.info(
                        f"Mention @@{username} skipped: mirror is not mentionable"
                    )
                    candidate = None
                user = candidate

            if user:
                # Check visibility if sender_id provided
                if sender_id:
                    from .engine_service import get_engine_service
                    engine = get_engine_service()
                    visible = await engine.department_service.check_visible(sender_id, user.id, org_id)
                    if not visible:
                        logger.warning(f"Mention @{username} skipped: not visible to sender {sender_id}")
                        continue

                mentions.append(Mention(
                    type=mention_type,
                    user_id=user.id,
                    username=username,
                    position=position,
                ))
            else:
                logger.warning(f"Could not resolve mention @{username} in org {org_id}")

        return mentions

    def get_ai_mentions(self, mentions: List[Mention]) -> List[Mention]:
        """Filter only @@ (AI role) mentions"""
        return [m for m in mentions if m.type == MentionType.AI_ROLE]

    def get_user_mentions(self, mentions: List[Mention]) -> List[Mention]:
        """Filter only @ (user) mentions"""
        return [m for m in mentions if m.type == MentionType.USER]

    def strip_mentions(self, content: str) -> str:
        """Remove all mentions from content, leaving just the text"""
        return self.MENTION_PATTERN.sub('', content).strip()

    def extract_message_for_ai(self, content: str, target_username: str) -> str:
        """
        Extract the message intended for a specific AI role.

        Example: "@@lawyer проверь договор" -> "проверь договор"
        """
        # Remove the specific @@ mention
        pattern = re.compile(rf'@@{re.escape(target_username)}\s*', re.IGNORECASE)
        return pattern.sub('', content).strip()
