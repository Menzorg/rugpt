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
        org_id: UUID
    ) -> List[Mention]:
        """
        Parse and resolve mentions to user IDs.

        Returns list of Mention objects with resolved user_ids.
        """
        parsed = self.parse_mentions(content)
        mentions = []

        for mention_type, username, position in parsed:
            user = await self.user_storage.get_by_username(username, org_id)
            if user:
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
