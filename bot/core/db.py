"""MongoDB wiring — actually used here, unlike TG where MONGO_URI was required
but never connected. Owns four collections: `sudo_users` (a persisted
allow-list beyond the single OWNER_ID env var), `chat_settings` (a minimal
per-chat scaffold, write-through only in Phase 1 — nothing reads it back yet;
bot/core/queue.py stays pure in-memory until restart-persistence lands in a
later phase), and `resolved_url_cache` + `play_counts` (added 2026-08-04 —
bot/core/calls.py's cross-restart cache-warming; see its own docstrings).

AsyncMongoClient (not motor — deprecated) must be constructed inside a running
event loop, so `connect()` is called from main.py, not at import time.
"""
from __future__ import annotations

from pymongo import AsyncMongoClient

from bot.utils.logger import get_logger
from config import settings

logger = get_logger(__name__)

_client: AsyncMongoClient | None = None
db = None  # set by connect()
_sudo_cache: set[int] = set()


async def connect() -> None:
    global _client, db
    _client = AsyncMongoClient(settings.mongo_uri)
    db = _client.get_default_database(default="tg2_musicbot")
    await db.command("ping")
    await _refresh_sudo_cache()
    logger.info("Connected to MongoDB (%d sudo user(s) cached)", len(_sudo_cache))


async def disconnect() -> None:
    if _client is not None:
        await _client.close()


async def _refresh_sudo_cache() -> None:
    _sudo_cache.clear()
    async for doc in db.sudo_users.find({}, {"_id": 1}):
        _sudo_cache.add(doc["_id"])


def is_sudo(user_id: int) -> bool:
    """Sync, O(1), no Mongo round-trip — safe to call on every message."""
    return user_id in _sudo_cache


async def add_sudo(user_id: int) -> bool:
    """Returns True if the user was newly added, False if already sudo."""
    result = await db.sudo_users.update_one(
        {"_id": user_id}, {"$setOnInsert": {"_id": user_id}}, upsert=True
    )
    is_new = result.upserted_id is not None
    if is_new:
        _sudo_cache.add(user_id)
    return is_new


async def remove_sudo(user_id: int) -> bool:
    """Returns True if the user was removed, False if they weren't sudo."""
    result = await db.sudo_users.delete_one({"_id": user_id})
    _sudo_cache.discard(user_id)
    return result.deleted_count > 0


DEFAULT_CHAT_SETTINGS = {"loop_mode": "off", "autoplay": False}


async def get_chat_settings(chat_id: int) -> dict:
    doc = await db.chat_settings.find_one({"_id": chat_id})
    return doc or {"_id": chat_id, **DEFAULT_CHAT_SETTINGS}


async def set_chat_setting(chat_id: int, **fields: object) -> None:
    await db.chat_settings.update_one({"_id": chat_id}, {"$set": fields}, upsert=True)


async def get_resolved_url_cache() -> dict[str, tuple[str, str, float]]:
    """Every stored resolved-URL cache entry, keyed by YouTube video ID —
    bot/core/calls.py reads this once at startup to restore its in-memory
    cache across a restart (otherwise every redeploy throws away every
    recently-resolved song). Doesn't filter by TTL — that's calls.py's
    policy to own, not this module's; the result may include stale entries
    the caller should skip."""
    result: dict[str, tuple[str, str, float]] = {}
    async for doc in db.resolved_url_cache.find({}):
        result[doc["_id"]] = (doc["video_url"], doc["audio_url"], doc["resolved_at"])
    return result


async def save_resolved_url(video_id: str, video_url: str, audio_url: str, resolved_at: float) -> None:
    await db.resolved_url_cache.update_one(
        {"_id": video_id},
        {"$set": {"video_url": video_url, "audio_url": audio_url, "resolved_at": resolved_at}},
        upsert=True,
    )


async def record_play(video_id: str) -> None:
    """Increments a per-video play counter — feeds calls.py's pre-warming
    loop, which keeps the most-played songs' cache entries perpetually
    fresh instead of only ever caching reactively after someone waits
    through a cold extraction."""
    await db.play_counts.update_one({"_id": video_id}, {"$inc": {"count": 1}}, upsert=True)


async def get_top_played_video_ids(limit: int) -> list[str]:
    cursor = db.play_counts.find({}, {"_id": 1}).sort("count", -1).limit(limit)
    return [doc["_id"] async for doc in cursor]
