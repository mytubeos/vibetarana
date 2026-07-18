"""/ping command."""
import time

from pyrogram import Client, filters
from pyrogram.types import Message


@Client.on_message(filters.command("ping"))
async def ping_cmd(_: Client, message: Message) -> None:
    start = time.monotonic()
    reply = await message.reply_text("Pinging...")
    elapsed_ms = (time.monotonic() - start) * 1000
    await reply.edit_text(f"Pong! `{elapsed_ms:.0f} ms`")
