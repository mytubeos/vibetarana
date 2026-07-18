"""Queue commands: /queue /loop /shuffle /export /import."""
import json
from io import BytesIO

from pyrogram import Client, filters
from pyrogram.enums import ParseMode
from pyrogram.types import Message

from bot.core import calls, db
from bot.core.decorators import admin_filter
from bot.core.queue import queues

MAX_IMPORT_FILE_BYTES = 512 * 1024


@Client.on_message(filters.command("queue") & filters.group)
async def queue_cmd(_: Client, message: Message) -> None:
    state = queues.get(message.chat.id)
    if state.current is None:
        await message.reply_text("Queue is empty.")
        return

    lines = [f"Loop: {state.loop_mode}", "", f"Now playing: {state.current.title} ({state.current.duration})"]
    upcoming = state.queue[1:21]
    if upcoming:
        lines.append("")
        lines.append("Up next:")
        lines.extend(f"{i}. {track.title} ({track.duration})" for i, track in enumerate(upcoming, start=1))
    remaining = len(state.queue) - 1 - len(upcoming)
    if remaining > 0:
        lines.append(f"...and {remaining} more.")

    # Track titles may come from an imported (untrusted) queue file — disable
    # markdown parsing so a hostile title can't inject formatting/links.
    await message.reply_text("\n".join(lines), parse_mode=ParseMode.DISABLED)


@Client.on_message(filters.command("loop") & filters.group & admin_filter)
async def loop_cmd(_: Client, message: Message) -> None:
    if len(message.command) < 2 or message.command[1].lower() not in ("off", "one", "all"):
        await message.reply_text("Usage: `/loop <off|one|all>`")
        return

    mode = message.command[1].lower()
    queues.get(message.chat.id).loop_mode = mode
    await db.set_chat_setting(message.chat.id, loop_mode=mode)
    await message.reply_text(f"🔁 Loop mode set to `{mode}`.")


@Client.on_message(filters.command("shuffle") & filters.group & admin_filter)
async def shuffle_cmd(_: Client, message: Message) -> None:
    ok = queues.shuffle(message.chat.id)
    await message.reply_text("🔀 Queue shuffled." if ok else "Not enough tracks queued to shuffle.")


@Client.on_message(filters.command("export") & filters.group)
async def export_cmd(_: Client, message: Message) -> None:
    state = queues.get(message.chat.id)
    if not state.queue:
        await message.reply_text("Queue is empty — nothing to export.")
        return

    payload = json.dumps(queues.export(message.chat.id), indent=2).encode("utf-8")
    file = BytesIO(payload)
    file.name = f"queue_{message.chat.id}.json"
    await message.reply_document(file, caption=f"Exported {len(state.queue)} track(s).")


@Client.on_message(filters.command("import") & filters.group & admin_filter)
async def import_cmd(client: Client, message: Message) -> None:
    reply = message.reply_to_message
    file_name = reply.document.file_name if reply and reply.document else None
    if not file_name or not file_name.endswith(".json"):
        await message.reply_text("Reply to a `.json` queue file (from /export) with `/import`.")
        return
    if reply.document.file_size and reply.document.file_size > MAX_IMPORT_FILE_BYTES:
        await message.reply_text("That file is too large to import (max 512 KB).")
        return

    chat_id = message.chat.id
    was_idle = queues.get(chat_id).current is None
    try:
        buffer = await client.download_media(reply, in_memory=True)
        added = queues.import_queue(chat_id, buffer.getvalue())
    except ValueError as exc:
        await message.reply_text(f"Couldn't import that queue: {exc}")
        return
    except Exception:
        await message.reply_text("Couldn't download or read that file.")
        return

    if was_idle:
        next_track = queues.get(chat_id).current
        if next_track is not None:
            assistant = await calls.join_and_play(chat_id, next_track)
            if assistant is None:
                # Mirror play.py's rollback: leaving imported tracks queued
                # with no assistant playing them would make `current` look
                # "playing" to every check that follows (was_idle in a later
                # /play, /skip reporting a track that never actually streams)
                # even though nothing is — so discard the batch instead.
                queues.clear(chat_id)
                await message.reply_text(
                    "All assistants are busy right now — try again in a bit, or ask "
                    "the bot owner to add another assistant account. Nothing was imported."
                )
                return

    await message.reply_text(f"📥 Imported {added} track(s).")
