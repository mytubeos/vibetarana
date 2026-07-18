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
    "**Playback**\n"
    "/play <query or link> — YouTube, Spotify, Apple Music, SoundCloud, and "
    "direct audio/video links all work (Spotify/Apple Music/SoundCloud "
    "resolve to a matching YouTube stream — none of them let bots pull raw "
    "audio directly); plain text searches YouTube\n"
    "/vplay <query or link> — same as /play, but streams video too\n"
    "/pause — pause playback\n"
    "/resume — resume playback\n"
    "/skip — skip to the next queued track\n"
    "/stop — stop playback, clear the queue, leave the voice chat\n"
    "/seek <seconds> — jump to a position in the current track\n\n"
    "**Queue**\n"
    "/queue — show what's playing and queued\n"
    "/loop <off|one|all> — set repeat mode\n"
    "/autoplay <on|off> — when the queue empties, keep playing YouTube-related "
    "tracks instead of leaving\n"
    "/shuffle — randomize the upcoming tracks\n"
    "/export — download the current queue as a .json file\n"
    "/import — reply to a .json queue file to add its tracks\n\n"
    "**Admin**\n"
    "/addsudo <user_id> — grant sudo access (owner only)\n"
    "/delsudo <user_id> — revoke sudo access (owner only)\n\n"
    "**Other**\n"
    "/ping — check the bot is alive\n\n"
    "Playback commands are restricted to group admins, sudo users, and the bot owner."
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
