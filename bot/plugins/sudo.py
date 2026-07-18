"""Sudo user management: /addsudo /delsudo (owner only, works in DM)."""
from pyrogram import Client, filters
from pyrogram.types import Message

from bot.core import db
from bot.core.decorators import owner_filter


def _parse_user_id(message: Message) -> int | None:
    if len(message.command) < 2:
        return None
    try:
        return int(message.command[1])
    except ValueError:
        return None


@Client.on_message(filters.command("addsudo") & owner_filter)
async def addsudo_cmd(_: Client, message: Message) -> None:
    user_id = _parse_user_id(message)
    if user_id is None:
        await message.reply_text("Usage: `/addsudo <user_id>`")
        return
    added = await db.add_sudo(user_id)
    await message.reply_text(f"✅ `{user_id}` added as sudo." if added else f"`{user_id}` is already sudo.")


@Client.on_message(filters.command("delsudo") & owner_filter)
async def delsudo_cmd(_: Client, message: Message) -> None:
    user_id = _parse_user_id(message)
    if user_id is None:
        await message.reply_text("Usage: `/delsudo <user_id>`")
        return
    removed = await db.remove_sudo(user_id)
    await message.reply_text(f"✅ `{user_id}` removed from sudo." if removed else f"`{user_id}` wasn't sudo.")
