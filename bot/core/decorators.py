"""Command filters — restricts playback control commands to chat admins,
sudo users, and the bot owner; sudo-management commands to the owner only."""
from __future__ import annotations

from pyrogram import filters
from pyrogram.enums import ChatMemberStatus, ChatType
from pyrogram.types import Message

from bot.core import db
from config import settings


def _is_owner_id(user_id: int) -> bool:
    return user_id == settings.owner_id


async def _is_owner(_, __, message: Message) -> bool:
    return message.from_user is not None and _is_owner_id(message.from_user.id)


async def _is_admin_or_owner(_, client, message: Message) -> bool:
    if message.from_user is None:
        return False
    user_id = message.from_user.id
    if _is_owner_id(user_id) or db.is_sudo(user_id):
        return True
    if message.chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP, ChatType.CHANNEL):
        return False
    try:
        member = await client.get_chat_member(message.chat.id, user_id)
    except Exception:
        return False
    return member.status in (ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.OWNER)


owner_filter = filters.create(_is_owner)
admin_filter = filters.create(_is_admin_or_owner)
