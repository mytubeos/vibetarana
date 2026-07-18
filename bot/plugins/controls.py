"""Playback control commands: /pause /resume /skip /stop /seek, plus the
inline-button equivalents attached to "now playing" messages."""
from pyrogram import Client, filters
from pyrogram.enums import ParseMode
from pyrogram.types import CallbackQuery, Message

from bot.core import calls
from bot.core.decorators import admin_filter, user_is_admin_or_owner
from bot.core.queue import queues
from bot.utils.formatting import playback_keyboard, track_block


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
        await message.reply_text(
            track_block(next_track, heading="⏭ NOW PLAYING", footer="▶️ Playing"),
            reply_markup=playback_keyboard(paused=False),
        )


@Client.on_message(filters.command("stop") & filters.group & admin_filter)
async def stop_cmd(_: Client, message: Message) -> None:
    ok = await calls.stop(message.chat.id)
    await message.reply_text("⏹ Stopped and left the voice chat." if ok else "Nothing was playing.")


@Client.on_message(filters.command("seek") & filters.group & admin_filter)
async def seek_cmd(_: Client, message: Message) -> None:
    if len(message.command) < 2 or not message.command[1].lstrip("-").isdigit():
        # parse_mode disabled: Pyrogram parses Markdown+HTML together by
        # default, and the <seconds> placeholder gets misread as an HTML
        # tag, which corrupts the backtick-code entity and throws
        # EntityBoundsInvalid — confirmed live, same for /loop and
        # /autoplay's usage messages below.
        await message.reply_text("Usage: /seek <seconds>", parse_mode=ParseMode.DISABLED)
        return

    seconds = int(message.command[1])
    if seconds < 0:
        await message.reply_text("Seek position must be 0 or greater.")
        return

    ok = await calls.seek(message.chat.id, seconds)
    await message.reply_text(f"⏩ Seeked to {seconds}s." if ok else "Nothing is playing right now.")


@Client.on_callback_query(filters.regex(r"^vt:(pause|resume|skip|stop)$"))
async def playback_callback(client: Client, callback_query: CallbackQuery) -> None:
    """Inline-button equivalents of /pause /resume /skip /stop, attached to
    "now playing" messages via playback_keyboard() (bot/utils/formatting.py).
    admin_filter only applies to Message updates, so callback queries need
    the same admin/sudo/owner check done directly here instead."""
    chat = callback_query.message.chat
    if not await user_is_admin_or_owner(client, callback_query.from_user.id, chat):
        await callback_query.answer("⛔ Admins, sudo users, and the owner only.", show_alert=True)
        return

    action = callback_query.data.split(":", 1)[1]
    chat_id = chat.id

    if action == "skip":
        next_track = await calls.skip(chat_id)
        await callback_query.answer("⏭ Skipped")
        if next_track is None:
            await callback_query.message.edit_text(
                "Queue is empty — leaving the voice chat shortly if nothing else is added."
            )
        else:
            await callback_query.message.edit_text(
                track_block(next_track, heading="⏭ NOW PLAYING", footer="▶️ Playing"),
                reply_markup=playback_keyboard(paused=False),
            )
        return

    if action == "stop":
        await calls.stop(chat_id)
        await callback_query.answer("⏹ Stopped")
        await callback_query.message.edit_text("⏹ Stopped and left the voice chat.")
        return

    ok = await (calls.pause(chat_id) if action == "pause" else calls.resume(chat_id))
    await callback_query.answer(("⏸ Paused" if action == "pause" else "▶️ Resumed") if ok else "Nothing is playing.")
    if not ok:
        return

    current = queues.get(chat_id).current
    if current is not None:
        await callback_query.message.edit_text(
            track_block(
                current,
                heading="🎵 NOW PLAYING",
                footer="⏸ Paused" if action == "pause" else "▶️ Playing",
            ),
            reply_markup=playback_keyboard(paused=action == "pause"),
        )
