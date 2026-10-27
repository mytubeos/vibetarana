"""Entrypoint — loads config, connects Mongo, starts the bot + assistant pool
+ call handlers, idles."""
from __future__ import annotations

import asyncio

from pyrogram import idle

from bot.core import db, service, player
from bot.core.assistants import pool
from bot.core.calls import (
    patch_ytdlp_timeout,
    prewarm_popular_songs_loop,
    register_all_handlers,
    restore_resolved_url_cache,
    setup_cookies,
    stale_assignment_check_loop,
)
from bot.core.client import bot
from bot.utils.keepalive import start_keepalive_server
from bot.utils.logger import get_logger
from config import settings  # noqa: F401 — importing triggers fail-fast env validation

logger = get_logger(__name__)


async def main() -> None:
    from bot.core.lease import lease
    from bot.core import scheduler
    from bot.core.session import sessions
    from bot.core.media import ROOT
    tasks = []
    bot_started = False
    pool_attempted = False
    keepalive_runner = await start_keepalive_server()
    try:
        await db.connect()
        await lease.acquire()
        tasks.append(asyncio.create_task(lease.renew_loop()))
        # Only the lease owner may clean files or restore queues.
        if ROOT.exists():
            for pattern in ('*.media', '*.media.temp'):
                for path in ROOT.glob(pattern):
                    path.unlink(missing_ok=True)
        setup_cookies()
        patch_ytdlp_timeout()
        await restore_resolved_url_cache()
        await service.restore()
        lease.check()
        await bot.start()
        bot_started = True
        pool_attempted = True
        await pool.start()
        lease.check()
        register_all_handlers()
        tasks.extend([asyncio.create_task(pool.health_check_loop()),
                      asyncio.create_task(stale_assignment_check_loop()),
                      asyncio.create_task(prewarm_popular_songs_loop()),
                      asyncio.create_task(player.refresh_loop()),
                      asyncio.create_task(scheduler.loop())])
        from bot.core.alerts import notify
        await notify(f"Vibetarana ready: {len(pool.assistants)} assistants. Restored queues wait for /resume.", key="startup")
        idle_task = asyncio.create_task(idle())
        lost_task = asyncio.create_task(lease.lost.wait())
        tasks.extend([idle_task, lost_task])
        done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        if lease.lost.is_set():
            raise RuntimeError('Deployment lease lost; shutting down to avoid duplicate playback.')
        if any(task is not idle_task for task in done):
            raise RuntimeError('A background worker stopped unexpectedly; restarting safely.')
    finally:
        logger.info('Shutting down...')
        from bot.core.calls import _inflight, _prefetch_tasks
        active = [s.task for s in sessions.values() if s.task and s.task is not asyncio.current_task()]
        tasks.extend(active + list(_inflight.values()) + list(_prefetch_tasks.values()))
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        try:
            if pool_attempted:
                await pool.stop()
        finally:
            try:
                if bot_started:
                    await bot.stop()
            finally:
                try:
                    await lease.release()
                finally:
                    await db.disconnect()
                    if keepalive_runner is not None:
                        await keepalive_runner.cleanup()


if __name__ == "__main__":
    asyncio.run(main())
