"""Command filters — restricts playback control commands to chat admins,
sudo users, and the bot owner; sudo-management commands to the owner only.
user_is_admin_or_owner() is the same check, factored out so the inline-button
callback handler (bot/plugins/controls.py) can reuse it outside the
Message-shaped filters.create() form Pyrogram commands use."""
from __future__ import annotations

from pyrogram import filters
from pyrogram.enums import ChatMemberStatus, ChatType
from pyrogram.types import Chat, Message

from bot.core import db
from config import settings


def _is_owner_id(user_id: int) -> bool:
    return user_id == settings.owner_id


async def user_is_admin_or_owner(client, user_id: int, chat: Chat) -> bool:
    if _is_owner_id(user_id) or db.is_sudo(user_id):
        return True
    if chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP, ChatType.CHANNEL):
        return False
    try:
        member = await client.get_chat_member(chat.id, user_id)
    except Exception:
        return False
    return member.status in (ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.OWNER)


async def _is_owner(_, __, message: Message) -> bool:
    return message.from_user is not None and _is_owner_id(message.from_user.id)


async def _is_admin_or_owner(_, client, message: Message) -> bool:
    if message.from_user is None:
        return False
    return await user_is_admin_or_owner(client, message.from_user.id, message.chat)


owner_filter = filters.create(_is_owner)
admin_filter = filters.create(_is_admin_or_owner)
