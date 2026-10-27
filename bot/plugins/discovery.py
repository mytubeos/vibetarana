"""Explicit mood/recognition requests offer selectable, resolvable songs."""
import asyncio
import secrets
import time
from pyrogram import Client, filters
from pyrogram.enums import ParseMode
from pyrogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from py_yt import VideosSearch
from bot.core import discovery, preferences
from bot.core.queue import Track, queues
from bot.core.media import youtube_id, validate_track, local_media
from bot.core.decorators import request_filter
from bot.core.session import get_session
from bot.plugins.play import _choices, allow_request, replied_media
from config import settings

async def choices(message, query, label, revision):
    async with asyncio.timeout(20):
        result = await VideosSearch(query, limit=5).next()
    tracks = []
    for item in result.get('result', []):
        link = f"https://www.youtube.com/watch?v={item.get('id', '')}"
        if not youtube_id(link) or not item.get('duration'):
            continue
        track = Track(str(item.get('title', 'Music'))[:250], item['duration'], link, None,
                      message.from_user.id, message.from_user.first_name or 'listener')
        try:
            validate_track(track)
            tracks.append(track)
        except ValueError:
            pass
    if not tracks:
        raise ValueError('No playable suggestions found. Try another mood or title.')
    token = secrets.token_hex(6)
    _choices[token] = (time.monotonic() + 120, message.chat.id, message.from_user.id, revision, tracks)
    while len(_choices) > 500:
        _choices.popitem(last=False)
    keyboard = InlineKeyboardMarkup([[InlineKeyboardButton(t.title[:55], callback_data=f'pick:{token}:{i}')] for i, t in enumerate(tracks)])
    await message.reply_text(label + '\nChoose a song:', reply_markup=keyboard, parse_mode=ParseMode.DISABLED)

@Client.on_message(filters.command(['mood', 'identify', 'lyrics']) & filters.group & request_filter)
async def discover(_, message):
    if not allow_request(message.chat.id, message.from_user.id):
        return
    revision = get_session(message.chat.id).revision
    name = message.command[0].lower()
    path = None
    try:
        if name == 'mood':
            prompt = message.text.split(None, 1)[1] if len(message.command) > 1 else ''
            pref = await preferences.get(message.chat.id)
            query, ai = await discovery.mood_query(prompt, pref.get('music_language', 'any'))
            await choices(message, query, 'AI-assisted suggestions' if ai else 'Mood search (AI provider not configured)', revision)
        elif name == 'identify':
            if not settings.audd_api_token:
                raise ValueError('Recognition is disabled. The operator must configure AudD first.')
            reply = message.reply_to_message
            media = (reply.audio or reply.voice) if reply else None
            if not media or not media.file_size or media.file_size > 1024 * 1024 or not 0 < media.duration <= 15:
                raise ValueError('Reply /identify to an audio/voice clip up to 15 seconds and 1 MB. This sends that clip to AudD for identification.')
            path = await local_media(replied_media(message, False))
            query = await discovery.recognize(path)
            await choices(message, query, 'Recognized: ' + query, revision)
        else:
            current = queues.get(message.chat.id).current
            query = message.text.split(None, 1)[1] if len(message.command) > 1 else (current.title if current else '')
            if not query:
                raise ValueError('Usage: /lyrics artist song, or use while a song is playing.')
            title, link = await discovery.lyrics_link(query)
            await message.reply_text(f'{title}\nLyrics provider: {link}', parse_mode=ParseMode.DISABLED, disable_web_page_preview=True)
    except (ValueError, TimeoutError) as exc:
        await message.reply_text(str(exc) or 'Provider timed out.', parse_mode=ParseMode.DISABLED)
    except Exception:
        await message.reply_text('Discovery provider is unavailable. Try /search instead.')
    finally:
        if path:
            path.unlink(missing_ok=True)
