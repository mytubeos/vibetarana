"""Playback control commands: /pause /resume /skip /stop /seek."""
from pyrogram import Client, filters
from pyrogram.types import Message

from bot.core import calls
from bot.core.decorators import admin_filter


@Client.on_message(filters.command("pause") & filters.group & admin_filter)
async def pause_cmd(_: Client, message: Message) -> None:
    ok = await calls.pause(message.chat.id)
    await message.reply_text("⏸ Paused." if ok else "Nothing is playing right now.")


@Client.on_message(filters.command("resume") & filters.group & admin_filter)
async def resume_cmd(_: Client, message: Message) -> None:
    ok = await calls.resume(message.chat.id)
    await message.reply_text("▶️ Resumed." if ok else "Nothing is playing right now.")


@Client.on_message(filters.command("skip") & filters.group & admin_filter)
async def skip_cmd(_: Client, message: Message) -> None:
    next_track = await calls.skip(message.chat.id)
    if next_track is None:
        await message.reply_text("Queue is empty — leaving the voice chat shortly if nothing else is added.")
    else:
        await message.reply_text(f"⏭ Now playing: **{next_track.title}** ({next_track.duration})")


@Client.on_message(filters.command("stop") & filters.group & admin_filter)
async def stop_cmd(_: Client, message: Message) -> None:
    ok = await calls.stop(message.chat.id)
    await message.reply_text("⏹ Stopped and left the voice chat." if ok else "Nothing was playing.")


@Client.on_message(filters.command("seek") & filters.group & admin_filter)
async def seek_cmd(_: Client, message: Message) -> None:
    if len(message.command) < 2 or not message.command[1].lstrip("-").isdigit():
        await message.reply_text("Usage: `/seek <seconds>`")
        return

    seconds = int(message.command[1])
    if seconds < 0:
        await message.reply_text("Seek position must be 0 or greater.")
        return

    ok = await calls.seek(message.chat.id, seconds)
    await message.reply_text(f"⏩ Seeked to {seconds}s." if ok else "Nothing is playing right now.")
