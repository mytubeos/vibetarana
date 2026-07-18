"""Bot account client — handles commands/UI only, never itself joins a voice chat."""
from pyrogram import Client

from config import settings

bot = Client(
    name="MusicBot",
    api_id=settings.api_id,
    api_hash=settings.api_hash,
    bot_token=settings.bot_token,
    plugins=dict(root="bot/plugins"),
)
