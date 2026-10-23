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
    tasks = []
    # Downloads are cache files, not the durable library. A crashed previous
    # single worker may have left them behind; restored Telegram IDs redownload.
    from bot.core.media import ROOT
    if ROOT.exists():
        for pattern in ('*.media', '*.media.temp'):
            for path in ROOT.glob(pattern):
                path.unlink(missing_ok=True)
    setup_cookies()
    patch_ytdlp_timeout()

    # Connect + prime the sudo cache before the bot starts dispatching
    # messages, so admin_filter never wrongly rejects an early sudo command.
    # Bind the (Render-only) keep-alive port before anything slower, so a
    # Mongo/Telegram hiccup below shows up as a clear log line instead of a
    # confusing "no open port detected" deploy timeout.
    keepalive_runner = await start_keepalive_server()
    try:
        await db.connect()
        await restore_resolved_url_cache()
        await service.restore()
        try:
            await bot.start()
            try:
                me = await bot.get_me()
                logger.info("Bot started as @%s", me.username)

                await pool.start()
                try:
                    register_all_handlers()
                    tasks = [asyncio.create_task(pool.health_check_loop()),
                             asyncio.create_task(stale_assignment_check_loop()),
                             asyncio.create_task(prewarm_popular_songs_loop()),
                             asyncio.create_task(player.refresh_loop())]

                    logger.info(
                        "Ready — %d assistant(s) online, %d chat(s) each",
                        len(pool.assistants),
                        settings.max_vc_per_assistant,
                    )
                    from bot.core.alerts import notify
                    await notify(f"Vibetarana ready: {len(pool.assistants)} assistants. Restored queues wait for /resume.", key="startup")
                    await idle()
                finally:
                    logger.info("Shutting down...")
                    for task in tasks:
                        task.cancel()
                    await asyncio.gather(*tasks, return_exceptions=True)
                    from bot.core.calls import _inflight, _prefetch_tasks
                    pending = list(_inflight.values()) + list(_prefetch_tasks.values())
                    for task in pending:
                        task.cancel()
                    await asyncio.gather(*pending, return_exceptions=True)
                    await pool.stop()
            finally:
                await bot.stop()
        finally:
            # Nested try/finally so a failure at any stage (bad bot token, every
            # assistant session invalid, idle() itself erroring) still cleanly
            # releases whatever was already acquired, instead of leaving a
            # dangling Pyrogram session or an idle Mongo connection open.
            await db.disconnect()
    finally:
        if keepalive_runner is not None:
            await keepalive_runner.cleanup()


if __name__ == "__main__":
    asyncio.run(main())
