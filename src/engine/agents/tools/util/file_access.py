from typing import Optional
from uuid import UUID

from ....storage.chat_storage import ChatStorage
from ....storage.user_file_storage import UserFileStorage


async def can_access_file(
    file_id: str,
    org_id: str,
    owner_user_id: str,
    file_storage: UserFileStorage,
    mention_mode: bool = False,
    is_admin: bool = False,
    chat_storage: Optional[ChatStorage] = None,
    chat_id: Optional[str] = None,
) -> bool:
    """Check whether a file is accessible for an agent run.

    Access is granted when any of the following is true:
    - The file is attached to the originating chat (chat_id match).
    - The file belongs to owner_user_id and mention_mode is False (direct call).
    - The file belongs to owner_user_id, is public, and mention_mode is True (mention call).
    - The file does not belong to owner_user_id but is_admin is True or the file is public.
    """
    try:
        org_uuid = UUID(org_id)
        owner_uuid = UUID(owner_user_id)
        file_uuid = UUID(file_id)
    except ValueError:
        return False

    # Chat attachments bypass ownership checks entirely.
    if chat_id and chat_storage is not None:
        try:
            attachment_ids = await chat_storage.get_attachments(UUID(chat_id))
            if file_uuid in attachment_ids:
                return True
        except (ValueError, Exception):
            pass

    all_files = await file_storage.list_by_org(org_uuid)
    for f in all_files:
        if f.id != file_uuid:
            continue
        if f.user_id == owner_uuid:
            # Owner's own file: allow unless caller is acting via mention (public-only restriction).
            if mention_mode:
                return f.is_public
            return True
        # Non-owner file: admin or public.
        return is_admin or f.is_public
    return False
