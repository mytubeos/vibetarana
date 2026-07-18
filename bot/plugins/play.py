"""/play and /vplay — resolve a query via the platform registry, queue it,
and start playback if idle. /vplay streams with video; /play is audio-only.
Both share _resolve_and_queue(); the only difference is the `video` flag set
on the resolved Track before it's queued (bot/core/calls.py reads that flag
per-track when it actually starts the stream)."""
from pyrogram import Client, filters
from pyrogram.enums import ParseMode
from pyrogram.types import Message

from bot.core import calls
from bot.core.decorators import admin_filter
from bot.core.queue import queues
from bot.platforms import resolve
from bot.utils.formatting import track_block


async def _resolve_and_queue(message: Message, *, video: bool) -> None:
    command_name = "vplay" if video else "play"
    if len(message.command) < 2:
        # parse_mode disabled: the <song name, ...> placeholder gets misread
        # as an HTML tag by Pyrogram's combined Markdown+HTML parser, which
        # corrupts the backtick-code entity and throws EntityBoundsInvalid.
        await message.reply_text(
            f"Usage: /{command_name} <song name, YouTube/Spotify/Apple Music/SoundCloud link>",
            parse_mode=ParseMode.DISABLED,
        )
        return

    query = message.text.split(None, 1)[1]
    chat_id = message.chat.id
    # query is arbitrary user input — could itself contain backticks/`<>`,
    # so it goes in unparsed rather than risk the same entity error.
    status = await message.reply_text(f"Searching for {query}...", parse_mode=ParseMode.DISABLED)

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
        await status.edit_text(track_block(track, heading=f"➕ ADDED TO QUEUE — #{position}"))
        return

    assistant = await calls.join_and_play(chat_id, track)
    if assistant is None:
        queues.clear(chat_id)
        # join_and_play() returns None for two different reasons — every
        # assistant busy, or the track itself failed to play (deleted/
        # region-blocked/unsupported) — it doesn't distinguish, so this
        # message has to stay accurate for both.
        await status.edit_text(
            "Couldn't start playback — either all assistants are busy right now, "
            "or that track failed to load. Try again in a bit or pick a different track."
        )
        return

    prefix = "🎥" if video else "🎵"
    await status.edit_text(track_block(track, heading=f"{prefix} NOW PLAYING", footer="▶️ Playing"))


@Client.on_message(filters.command("play") & filters.group & admin_filter)
async def play_cmd(_: Client, message: Message) -> None:
    await _resolve_and_queue(message, video=False)


@Client.on_message(filters.command("vplay") & filters.group & admin_filter)
async def vplay_cmd(_: Client, message: Message) -> None:
    await _resolve_and_queue(message, video=True)
