"""MongoDB wiring — actually used here, unlike TG where MONGO_URI was required
but never connected. Owns two Phase-1 collections: `sudo_users` (a persisted
allow-list beyond the single OWNER_ID env var) and `chat_settings` (a minimal
per-chat scaffold, write-through only in Phase 1 — nothing reads it back yet;
bot/core/queue.py stays pure in-memory until restart-persistence lands in a
later phase).

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


DEFAULT_CHAT_SETTINGS = {"loop_mode": "off"}


async def get_chat_settings(chat_id: int) -> dict:
    doc = await db.chat_settings.find_one({"_id": chat_id})
    return doc or {"_id": chat_id, **DEFAULT_CHAT_SETTINGS}


async def set_chat_setting(chat_id: int, **fields: object) -> None:
    await db.chat_settings.update_one({"_id": chat_id}, {"$set": fields}, upsert=True)
