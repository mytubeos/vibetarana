"""/start and /help commands."""
from pyrogram import Client, filters
from pyrogram.types import Message

START_TEXT = (
    "Hi! I'm a voice-chat music bot.\n\n"
    "Add me and an assistant account to a group, start the voice chat, then "
    "use /play <song name, YouTube link, Spotify link, Apple Music link, or "
    "SoundCloud link> to get going. /help for the full command list."
)

HELP_TEXT = (
    "🎵 **Playback**\n"
    "/play — Play music\n"
    "/vplay — Play video\n"
    "/playforce — Force-play, skipping the queue\n"
    "/vplayforce — Force-play video, skipping the queue\n"
    "/player — Show the interactive player panel\n"
    "/pause — Pause playback\n"
    "/resume — Resume playback\n"
    "/skip — Skip track\n"
    "/stop — Stop player\n"
    "/seek — Seek position\n\n"
    "📋 **Queue**\n"
    "/queue — Show queue\n"
    "/loop — Repeat mode\n"
    "/autoplay — Auto play\n"
    "/shuffle — Shuffle queue\n"
    "/export — Export queue\n"
    "/import — Import queue\n\n"
    "👑 **Admin**\n"
    "/addsudo — Add sudo\n"
    "/delsudo — Remove sudo\n\n"
    "⚡ **Other**\n"
    "/ping — Bot status\n\n"
    "🔒 Playback commands: admins, sudo users, and the owner.\n"
    "Send a command with no arguments to see its exact usage."
)


@Client.on_message(filters.command("start") & filters.private)
async def start_cmd(_: Client, message: Message) -> None:
    await message.reply_text(START_TEXT)


@Client.on_message(filters.command("start") & filters.group)
async def start_group_cmd(_: Client, message: Message) -> None:
    await message.reply_text("I'm alive — use /help to see what I can do.")


@Client.on_message(filters.command("help"))
async def help_cmd(_: Client, message: Message) -> None:
    await message.reply_text(HELP_TEXT)
