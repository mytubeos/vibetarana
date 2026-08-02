"""Command filters — restricts playback control commands to chat admins,
sudo users, and the bot owner; sudo-management commands to the owner only.
user_is_admin_or_owner() is the same check, factored out so the inline-button
callback handler (bot/plugins/controls.py) can reuse it outside the
Message-shaped filters.create() form Pyrogram commands use."""
from __future__ import annotations

import time

from pyrogram import filters
from pyrogram.enums import ChatMemberStatus, ChatType
from pyrogram.types import Chat, Message

from bot.core import db
from config import settings


def _is_owner_id(user_id: int) -> bool:
    return user_id == settings.owner_id


# Every non-owner/non-sudo command (i.e. the common case: a regular group
# admin using /play, /pause, ...) used to pay a live get_chat_member()
# Telegram API round-trip on every single message before doing anything
# else — the biggest avoidable per-command latency source that had nothing
# to do with yt-dlp. A short-TTL cache removes that for repeat commands from
# the same admin. 5 minutes is a deliberate tradeoff for a music bot: a
# demoted admin stays able to control playback for up to 5 more minutes,
# acceptable here since nothing security-sensitive is gated by this check.
_ADMIN_CACHE_TTL_SECONDS = 300
_ADMIN_CACHE_MAX_ENTRIES = 2000
_admin_cache: dict[tuple[int, int], tuple[bool, float]] = {}


async def user_is_admin_or_owner(client, user_id: int, chat: Chat) -> bool:
    if _is_owner_id(user_id) or db.is_sudo(user_id):
        return True
    if chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP, ChatType.CHANNEL):
        return False

    cache_key = (chat.id, user_id)
    cached = _admin_cache.get(cache_key)
    if cached is not None and time.monotonic() - cached[1] < _ADMIN_CACHE_TTL_SECONDS:
        return cached[0]

    try:
        member = await client.get_chat_member(chat.id, user_id)
        is_admin = member.status in (ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.OWNER)
    except Exception:
        is_admin = False

    _admin_cache[cache_key] = (is_admin, time.monotonic())
    # Bound growth over months of unattended uptime across many chats/users —
    # same pattern as bot/core/calls.py's prefetch/resolved-URL caches.
    while len(_admin_cache) > _ADMIN_CACHE_MAX_ENTRIES:
        _admin_cache.pop(next(iter(_admin_cache)))
    return is_admin


async def _is_owner(_, __, message: Message) -> bool:
    return message.from_user is not None and _is_owner_id(message.from_user.id)


async def _is_admin_or_owner(_, client, message: Message) -> bool:
    if message.from_user is None:
        return False
    return await user_is_admin_or_owner(client, message.from_user.id, message.chat)


owner_filter = filters.create(_is_owner)
admin_filter = filters.create(_is_admin_or_owner)
