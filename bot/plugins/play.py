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
from bot.utils.formatting import playback_keyboard, track_block


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
        calls.schedule_prefetch(chat_id)
        return

    # This is the one case with no earlier warning that a wait is coming
    # (queueing onto an active chat skips straight to "Added to Queue";
    # skip/auto-advance likely already have this track prefetched) — resolving
    # a fresh stream URL genuinely takes ~10-20s (see calls.py's
    # patch_ytdlp_timeout), so say so here instead of leaving "Searching for
    # ..." on screen with nothing happening, which reads as stuck/broken.
    await status.edit_text(f"🔗 Loading stream for {track.title}... (up to 20s)", parse_mode=ParseMode.DISABLED)
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
    await status.edit_text(
        track_block(track, heading=f"{prefix} NOW PLAYING", footer="▶️ Playing"),
        reply_markup=playback_keyboard(paused=False),
    )


@Client.on_message(filters.command("play") & filters.group & admin_filter)
async def play_cmd(_: Client, message: Message) -> None:
    await _resolve_and_queue(message, video=False)


@Client.on_message(filters.command("vplay") & filters.group & admin_filter)
async def vplay_cmd(_: Client, message: Message) -> None:
    await _resolve_and_queue(message, video=True)


async def _resolve_and_force_play(message: Message, *, video: bool) -> None:
    """/playforce and /vplayforce — unlike _resolve_and_queue(), never adds
    to the queue: immediately hot-swaps whatever's currently playing for the
    requested track, discarding it (queues.force_add), and picks up the
    rest of the existing queue normally once this one finishes."""
    command_name = "vplayforce" if video else "playforce"
    if len(message.command) < 2:
        await message.reply_text(
            f"Usage: /{command_name} <song name, YouTube/Spotify/Apple Music/SoundCloud link>",
            parse_mode=ParseMode.DISABLED,
        )
        return

    query = message.text.split(None, 1)[1]
    chat_id = message.chat.id
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

    await status.edit_text(f"🔗 Loading stream for {track.title}... (up to 20s)", parse_mode=ParseMode.DISABLED)
    queues.force_add(chat_id, track)
    assistant = await calls.force_play(chat_id, track)
    if assistant is None:
        queues.clear(chat_id)
        await status.edit_text(
            "Couldn't start playback — either all assistants are busy right now, "
            "or that track failed to load. Try again in a bit or pick a different track."
        )
        return

    prefix = "🎥" if video else "🎵"
    await status.edit_text(
        track_block(track, heading=f"{prefix} NOW PLAYING (forced)", footer="▶️ Playing"),
        reply_markup=playback_keyboard(paused=False),
    )


@Client.on_message(filters.command("playforce") & filters.group & admin_filter)
async def playforce_cmd(_: Client, message: Message) -> None:
    await _resolve_and_force_play(message, video=False)


@Client.on_message(filters.command("vplayforce") & filters.group & admin_filter)
async def vplayforce_cmd(_: Client, message: Message) -> None:
    await _resolve_and_force_play(message, video=True)
