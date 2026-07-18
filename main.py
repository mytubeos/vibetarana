"""Entrypoint — loads config, connects Mongo, starts the bot + assistant pool
+ call handlers, idles."""
from __future__ import annotations

import asyncio

from pyrogram import idle

from bot.core import db
from bot.core.assistants import pool
from bot.core.calls import register_all_handlers
from bot.core.client import bot
from bot.utils.logger import get_logger
from config import settings  # noqa: F401 — importing triggers fail-fast env validation

logger = get_logger(__name__)


async def main() -> None:
    # Connect + prime the sudo cache before the bot starts dispatching
    # messages, so admin_filter never wrongly rejects an early sudo command.
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


if __name__ == "__main__":
    asyncio.run(main())
