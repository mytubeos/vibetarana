"""Play requests, reply-to-media, and expiring per-user search choices."""
import asyncio
import secrets
import time
from collections import OrderedDict

from pyrogram import Client, filters
from pyrogram.enums import ParseMode
from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from py_yt import VideosSearch

from bot.core import preferences, service, player
from bot.core.decorators import request_filter, control_filter, can_request
from bot.core.queue import Track
from bot.core.session import get_session
from bot.core.i18n import text
from bot.platforms import resolve
from bot.utils.formatting import format_seconds
from config import settings

_cooldowns = OrderedDict()
_choices = OrderedDict()


def allow_request(chat_id, user_id):
    now = time.monotonic()
    key = (chat_id, user_id)
    if now - _cooldowns.get(key, -999999) < settings.request_cooldown_seconds:
        return False
    _cooldowns[key] = now
    _cooldowns.move_to_end(key)
    while len(_cooldowns) > 5000:
        _cooldowns.popitem(last=False)
    return True


def replied_media(message, video):
    reply = message.reply_to_message
    media = None
    if reply:
        media = reply.audio or reply.video or reply.voice
    if media is None:
        return None
    if not media.file_size or media.file_size > settings.max_media_mb * 1024 * 1024:
        raise ValueError(f'Media must have a known size below {settings.max_media_mb} MB.')
    seconds = getattr(media, 'duration', 0)
    if seconds <= 0 or seconds > settings.duration_limit_min * 60:
        raise ValueError(f'Media must be shorter than {settings.duration_limit_min} minutes.')
    from bot.utils.formatting import format_ms
    return Track(getattr(media, 'title', None) or getattr(media, 'file_name', None) or 'Telegram media',
                 format_ms(seconds * 1000), 'tgfile:' + media.file_id, None,
                 message.from_user.id, message.from_user.first_name or 'listener', 'Telegram', video)


async def _resolve_and_queue(message, *, video=False, force=False, next_up=False):
    chat_id = message.chat.id
    revision = get_session(chat_id).revision
    pref = await preferences.get(chat_id)
    if not allow_request(chat_id, message.from_user.id):
        await message.reply_text(text('cooldown', pref['language']))
        return
    status = await message.reply_text(text('loading', pref['language']))
    get_session(chat_id).pending_requests += 1
    try:
        track = replied_media(message, video)
        if track is None:
            if len(message.command) < 2:
                raise ValueError('Usage: /play song or URL; or reply to Telegram audio/video. Use /search for choices.')
            query = message.text.split(None, 1)[1][:500]
            async with asyncio.timeout(30):
                track = await resolve(query, message.from_user.id, message.from_user.first_name or 'listener')
        if track is None:
            raise ValueError('No matching track found. Try /search with the artist and title.')
        track.video = video
        position, started = await service.enqueue(chat_id, [track], force=force, next_up=next_up, revision=revision)
        await status.edit_text(f'{text("playing" if started else "queued", pref["language"])}: {track.title[:200]} · #{position}', parse_mode=ParseMode.DISABLED)
        await player.refresh(chat_id, create=True)
    except asyncio.CancelledError:
        await status.edit_text(text('cancelled', pref['language']))
    except (ValueError, TimeoutError) as exc:
        await status.edit_text(str(exc) or 'Search timed out. Please try again.', parse_mode=ParseMode.DISABLED)
    except Exception:
        from bot.utils.logger import get_logger
        get_logger(__name__).exception('Play request failed')
        await status.edit_text(text('failed', pref['language']))
    finally:
        get_session(chat_id).pending_requests -= 1


async def _resolve_and_force_play(message, *, video=False):
    await _resolve_and_queue(message, video=video, force=True)


@Client.on_message(filters.command(['play', 'vplay']) & filters.group & request_filter)
async def play_cmd(_, message):
    await _resolve_and_queue(message, video=message.command[0].lower() == 'vplay')


@Client.on_message(filters.command(['playforce', 'vplayforce', 'playnext']) & filters.group & control_filter)
async def force_cmd(_, message):
    name = message.command[0].lower()
    await _resolve_and_queue(message, video=name == 'vplayforce', force=name != 'playnext', next_up=name == 'playnext')


@Client.on_message(filters.command('search') & filters.group & request_filter)
async def search_cmd(_, message):
    if len(message.command) < 2:
        await message.reply_text('Usage: /search artist song')
        return
    if not allow_request(message.chat.id, message.from_user.id):
        await message.reply_text('Please wait a few seconds.')
        return
    try:
        async with asyncio.timeout(20):
            result = await VideosSearch(message.text.split(None, 1)[1][:500], limit=5).next()
        items = result.get('result', [])
        rows, choices = [], []
        for item in items:
            video_id = item.get('id', '')
            from bot.core.media import youtube_id
            link = f'https://www.youtube.com/watch?v={video_id}'
            if youtube_id(link) is None or not item.get('duration'):
                continue
            choices.append(Track(item.get('title', 'Unknown')[:250], item['duration'], link, None,
                                 message.from_user.id, message.from_user.first_name or 'listener'))
        if not choices:
            await message.reply_text('No matching songs found.')
            return
        token = secrets.token_hex(6)
        _choices[token] = (time.monotonic() + 120, message.chat.id, message.from_user.id, get_session(message.chat.id).revision, choices)
        while len(_choices) > 500:
            _choices.popitem(last=False)
        for i, track in enumerate(choices):
            rows.append([InlineKeyboardButton(f'{track.title[:45]} · {track.duration}', callback_data=f'pick:{token}:{i}')])
        await message.reply_text('Choose your song (available for 2 minutes):', reply_markup=InlineKeyboardMarkup(rows))
    except Exception:
        await message.reply_text('Search is unavailable. Please try again shortly.')


@Client.on_callback_query(filters.regex(r'^pick:'))
async def pick_song(client, callback):
    parts = callback.data.split(':')
    entry = _choices.get(parts[1]) if len(parts) == 3 else None
    if not entry or entry[0] < time.monotonic():
        await callback.answer('Search expired. Run /search again.', show_alert=True)
        return
    _, chat_id, user_id, revision, tracks = entry
    if callback.message is None or callback.message.chat.id != chat_id or callback.from_user.id != user_id:
        await callback.answer('These results belong to another listener.', show_alert=True)
        return
    if not await can_request(client, user_id, callback.message.chat):
        await callback.answer('Permission denied.', show_alert=True)
        return
    try:
        index = int(parts[2])
        if not 0 <= index < len(tracks):
            raise ValueError('Invalid choice.')
        _choices.pop(parts[1], None)
        await callback.answer('Loading…')
        await service.enqueue(chat_id, [tracks[index]], revision=revision)
        await callback.message.edit_reply_markup(None)
        await player.refresh(chat_id, create=True)
    except asyncio.CancelledError:
        await callback.message.reply_text('Request cancelled.')
    except Exception as exc:
        await callback.message.reply_text(str(exc)[:200], parse_mode=ParseMode.DISABLED)
