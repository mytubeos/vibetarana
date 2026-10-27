"""Durable one-shot jobs. Claimed jobs are never automatically replayed after a crash."""
import asyncio
import secrets
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from bot.core import db, service, player
from bot.core.client import bot
from bot.core.decorators import user_is_admin_or_owner
from bot.core.session import get_session
from bot.utils.logger import get_logger

logger = get_logger(__name__)
_running = {}


def parse_time(value, zone, *, now=None):
    try:
        local = datetime.fromisoformat(value)
        tz = ZoneInfo(zone)
        if local.tzinfo is not None:
            raise ValueError('Use a local date/time plus a named timezone.')
        first, second = local.replace(tzinfo=tz, fold=0), local.replace(tzinfo=tz, fold=1)
        if first.utcoffset() != second.utcoffset():
            raise ValueError('Ambiguous/nonexistent daylight-saving time; choose another time.')
        when = first.astimezone(timezone.utc)
        if when.astimezone(tz).replace(tzinfo=None) != local:
            raise ValueError('That local time does not exist.')
        now = now or datetime.now(timezone.utc)
        if not now + timedelta(seconds=30) < when <= now + timedelta(days=30):
            raise ValueError('Choose a time 30 seconds to 30 days in the future.')
        return when
    except (ZoneInfoNotFoundError, TypeError):
        raise ValueError('Use an IANA timezone, for example Asia/Kolkata.') from None


async def create(chat_id, user_id, payload, when, zone):
    if await db.db.scheduled_sessions.count_documents({'chat_id': chat_id, 'status': 'pending'}) >= 20:
        raise ValueError('Maximum 20 pending sessions per group.')
    job_id = secrets.token_hex(6)
    await db.db.scheduled_sessions.insert_one({'_id': job_id, 'chat_id': chat_id, 'user_id': user_id,
        'tracks': payload, 'due': when, 'timezone': zone, 'status': 'pending',
        'created_at': datetime.now(timezone.utc)})
    return job_id


async def cancel(chat_id, job_id):
    result = await db.db.scheduled_sessions.update_one(
        {'_id': job_id, 'chat_id': chat_id, 'status': {'$in': ['pending', 'running']}},
        {'$set': {'status': 'cancelled'}})
    task = _running.get(job_id)
    if result.modified_count and task:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    return bool(result.modified_count)


async def execute(job):
    status = 'failed'
    try:
        # A delayed start is surprising; jobs missed by >5 minutes are not played.
        due = job['due'].replace(tzinfo=timezone.utc)
        if datetime.now(timezone.utc) - due > timedelta(minutes=5):
            status = 'missed'
            return
        chat = await bot.get_chat(job['chat_id'])
        if not await user_is_admin_or_owner(bot, job['user_id'], chat):
            status = 'permission_revoked'
            return
        revision = get_session(chat.id).revision
        tracks = service.deserialize(job['tracks'])
        await service.enqueue(chat.id, tracks, revision=revision)
        status = 'done'
        await player.refresh(chat.id, create=True)
    except asyncio.CancelledError:
        status = 'interrupted'
        raise
    except Exception:
        logger.warning('Scheduled playback failed for %s', job['_id'], exc_info=True)
    finally:
        await db.db.scheduled_sessions.update_one({'_id': job['_id'], 'status': 'running'},
            {'$set': {'status': status, 'finished_at': datetime.now(timezone.utc)}})


async def loop():
    # Lease guarantees only one dispatcher. Mark crashed claims, never double play.
    await db.db.scheduled_sessions.update_many({'status': 'running'}, {'$set': {'status': 'interrupted'}})
    try:
        while True:
            try:
                for key, task in list(_running.items()):
                    if task.done():
                        if not task.cancelled() and task.exception():
                            logger.error('Schedule task failed: %s', key)
                        _running.pop(key)
                if len(_running) < 4:
                    from pymongo import ReturnDocument
                    job = await db.db.scheduled_sessions.find_one_and_update(
                        {'status': 'pending', 'due': {'$lte': datetime.now(timezone.utc)}},
                        {'$set': {'status': 'running'}}, sort=[('due', 1)], return_document=ReturnDocument.AFTER)
                    if job:
                        _running[job['_id']] = asyncio.create_task(execute(job))
                        continue
            except Exception:
                logger.warning('Scheduler temporarily unavailable', exc_info=True)
            await asyncio.sleep(2)
    finally:
        tasks = list(_running.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        _running.clear()
