"""Vibetarana onboarding and discoverable command help."""
from pyrogram import Client, filters
from pyrogram.enums import ParseMode
from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from bot.core import preferences
from bot.core.i18n import text
from config import settings

HELP_TEXT = """Vibetarana

PLAY: /play song or URL · /vplay · reply to audio/video with /play
DISCOVER: /search artist song · /favourite
PLAYER: /player · /pause · /resume · /skip · /stop · /seek seconds
QUEUE: /queue page · /playnext song · /remove 2 · /move 3 2
/shuffle · /clearqueue (keeps current) · /history · /export · /import
REPEAT: /loop off|one|all · /autoplay on|off
LIBRARY: /playlist list|save name|load name|delete name
/gplaylist for shared group playlists · /playlist load favourites
ADMIN: /settings · /dj add|remove user_id · /doctor
FORCE: /playforce · /vplayforce (admins/DJs)
COMMUNITY: /voteskip · /policy fair on|off · /policy votes 0..20
DISCOVERY: /mood your mood · /identify (reply to a short clip; sends it to AudD)
/lyrics artist song (provider link) · /importplaylist YouTube_playlist_URL
SESSIONS: /schedule list|add dateTtime timezone group_playlist|cancel id
/radio station (operator-configured YouTube streams) · /app
OWNER: /addsudo · /delsudo · /stopall

Queue positions start at 1 (current track). Requests default to admins/DJs.
Admins can enable /settings mode members. Saved queues wait for /resume after restart.
Language: /settings language en|hi|hinglish (core player/onboarding messages).
"""

@Client.on_message(filters.command('start'))
async def start_cmd(client, message):
    if len(message.command) > 1 and message.command[1].startswith("app_"):
        from bot.plugins.webapp import launch
        await launch(client, message, message.command[1][4:])
        return
    language = settings.default_lang
    if message.chat.type.value in ('group', 'supergroup'):
        language = (await preferences.get(message.chat.id))['language']
    me = await client.get_me()
    keyboard = InlineKeyboardMarkup([[InlineKeyboardButton('Add Vibetarana to a group', url=f'https://t.me/{me.username}?startgroup=true')]]) if me.username else None
    await message.reply_text(text('welcome', language), reply_markup=keyboard, parse_mode=ParseMode.DISABLED)

@Client.on_message(filters.command('help'))
async def help_cmd(_, message):
    await message.reply_text(HELP_TEXT, parse_mode=ParseMode.DISABLED)
