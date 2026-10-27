"""Exclusive deployment lease: overlapping replicas must not share Telegram sessions.

This guards one active worker, not sharded active/active playback. Use the same
Mongo database on every replica. Do not share assistant accounts between bots.
"""
import asyncio
import hashlib
import time
import uuid
from datetime import datetime, timezone, timedelta
from pymongo.errors import DuplicateKeyError
from bot.core import db
from config import settings

class WorkerLease:
    def __init__(self):
        self.owner = uuid.uuid4().hex
        self.keys = []
        self.lost = asyncio.Event()
        self.deadline = 0.0
        self.enforced = False

    async def acquire(self):
        keys = [f'bot:{settings.bot_token.split(":")[0]}'] + [
            'assistant:' + hashlib.sha256(s.encode()).hexdigest() for s in settings.assistant_sessions]
        try:
            for key in sorted(set(keys)):
                now = datetime.now(timezone.utc)
                try:
                    result = await db.db.worker_leases.update_one(
                        {'_id': key, '$or': [{'owner': self.owner}, {'expires': {'$lt': now}}]},
                        {'$set': {'owner': self.owner, 'expires': now + timedelta(seconds=settings.worker_lease_seconds)}}, upsert=True)
                except DuplicateKeyError:
                    raise RuntimeError('Another worker owns this bot/assistant deployment. Stop it before starting a replacement.') from None
                if not (result.matched_count or result.upserted_id):
                    raise RuntimeError('Could not acquire deployment lease.')
                self.keys.append(key)
            self.deadline = time.monotonic() + settings.worker_lease_seconds
            self.enforced = True
        except BaseException:
            await self.release()
            raise

    async def renew_loop(self):
        try:
            while True:
                await asyncio.sleep(settings.worker_lease_seconds / 4)
                for key in self.keys:
                    result = await db.db.worker_leases.update_one({'_id': key, 'owner': self.owner},
                        {'$set': {'expires': datetime.now(timezone.utc) + timedelta(seconds=settings.worker_lease_seconds)}})
                    if result.matched_count != 1:
                        raise RuntimeError('Lease ownership lost')
                self.deadline = time.monotonic() + settings.worker_lease_seconds
        except asyncio.CancelledError:
            raise
        except Exception:
            self.lost.set()  # main shuts down calls and Telegram clients immediately

    def valid(self):
        return bool(self.keys) and not self.lost.is_set() and time.monotonic() < self.deadline

    def check(self):
        if self.enforced and not self.valid():
            self.lost.set()
            raise RuntimeError("Worker ownership expired; restarting safely.")

    async def release(self):
        for key in self.keys:
            await db.db.worker_leases.delete_one({'_id': key, 'owner': self.owner})
        self.keys.clear()

lease = WorkerLease()
