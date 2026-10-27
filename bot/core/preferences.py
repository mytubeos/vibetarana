"""Durable per-group preferences, separate from transient playback state."""
import asyncio
from bot.core import db
from bot.core.queue import queues
from config import settings

DEFAULTS = {'loop_mode': 'off', 'autoplay': False, 'play_mode': 'admins',
            'language': settings.default_lang, 'dj_users': [], 'fair_queue': False,
            'vote_skip': 3, 'music_language': 'any', 'video_quality': '720'}
_cache: dict[int, dict] = {}
_locks: dict[int, asyncio.Lock] = {}


async def get(chat_id: int) -> dict:
    async with _locks.setdefault(chat_id, asyncio.Lock()):
        if chat_id not in _cache:
            doc = await db.get_chat_settings(chat_id)
            _cache[chat_id] = {**DEFAULTS, **doc}
        value = _cache[chat_id]
        state = queues.get(chat_id)
        state.loop_mode = value['loop_mode']
        state.autoplay = value['autoplay']
        return dict(value)


async def set(chat_id: int, **fields) -> dict:
    await get(chat_id)
    async with _locks.setdefault(chat_id, asyncio.Lock()):
        await db.set_chat_setting(chat_id, **fields)
        _cache[chat_id].update(fields)
    return await get(chat_id)
