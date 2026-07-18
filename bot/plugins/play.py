"""/play and /vplay — resolve a query via the platform registry, queue it,
and start playback if idle. /vplay streams with video; /play is audio-only.
Both share _resolve_and_queue(); the only difference is the `video` flag set
on the resolved Track before it's queued (bot/core/calls.py reads that flag
per-track when it actually starts the stream)."""
from pyrogram import Client, filters
from pyrogram.types import Message

from bot.core import calls
from bot.core.decorators import admin_filter
from bot.core.queue import queues
from bot.platforms import resolve


async def _resolve_and_queue(message: Message, *, video: bool) -> None:
    command_name = "vplay" if video else "play"
    if len(message.command) < 2:
        await message.reply_text(f"Usage: `/{command_name} <song name, YouTube/Spotify/Apple Music/SoundCloud link>`")
        return

    query = message.text.split(None, 1)[1]
    chat_id = message.chat.id
    status = await message.reply_text(f"Searching for `{query}`...")

    track = await resolve(
        query,
        requested_by=message.from_user.id,
        requested_by_name=message.from_user.first_name or "someone",
    )
    if track is None:
        await status.edit_text("Couldn't find anything for that query.")
        return
    track.video = video

    was_idle = queues.get(chat_id).current is None
    position = queues.add(chat_id, track)

    if not was_idle:
        await status.edit_text(f"Added to queue at position {position}: **{track.title}** ({track.duration})")
        return

    assistant = await calls.join_and_play(chat_id, track)
    if assistant is None:
        queues.clear(chat_id)
        await status.edit_text(
            "All assistants are busy right now — try again in a bit, or ask "
            "the bot owner to add another assistant account."
        )
        return

    prefix = "🎥" if video else "▶️"
    await status.edit_text(f"{prefix} Now playing: **{track.title}** ({track.duration})")


@Client.on_message(filters.command("play") & filters.group & admin_filter)
async def play_cmd(_: Client, message: Message) -> None:
    await _resolve_and_queue(message, video=False)


@Client.on_message(filters.command("vplay") & filters.group & admin_filter)
async def vplay_cmd(_: Client, message: Message) -> None:
    await _resolve_and_queue(message, video=True)
