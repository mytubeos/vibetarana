"""Entrypoint — loads config, connects Mongo, starts the bot + assistant pool
+ call handlers, idles."""
from __future__ import annotations

import asyncio

from pyrogram import idle

from bot.core import db
from bot.core.assistants import pool
from bot.core.calls import register_all_handlers, setup_cookies
from bot.core.client import bot
from bot.utils.keepalive import start_keepalive_server
from bot.utils.logger import get_logger
from config import settings  # noqa: F401 — importing triggers fail-fast env validation

logger = get_logger(__name__)


async def main() -> None:
    setup_cookies()

    # Connect + prime the sudo cache before the bot starts dispatching
    # messages, so admin_filter never wrongly rejects an early sudo command.
    # Bind the (Render-only) keep-alive port before anything slower, so a
    # Mongo/Telegram hiccup below shows up as a clear log line instead of a
    # confusing "no open port detected" deploy timeout.
    keepalive_runner = await start_keepalive_server()
    try:
        await db.connect()
        try:
            await bot.start()
            try:
                me = await bot.get_me()
                logger.info("Bot started as @%s", me.username)

                await pool.start()
                try:
                    register_all_handlers()
                    asyncio.create_task(pool.health_check_loop())

                    logger.info(
                        "Ready — %d assistant(s) online, %d chat(s) each",
                        len(pool.assistants),
                        settings.max_vc_per_assistant,
                    )
                    await idle()
                finally:
                    logger.info("Shutting down...")
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
