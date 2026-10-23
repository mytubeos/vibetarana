"""Minimal HTTP server so Render's free Web Service tier has a port to bind
(otherwise the deploy times out waiting for one) and an external pinger like
UptimeRobot has a URL to hit to prevent the 15-minute idle sleep.

Only starts when PORT is set. Render only sets that for Web Services, not
Background Workers, so this is a no-op on the systemd/VPS and Background
Worker deploy paths documented in the README.
"""
from __future__ import annotations

import os

from aiohttp import web

from bot.utils.logger import get_logger

logger = get_logger(__name__)


async def start_keepalive_server() -> web.AppRunner | None:
    port = os.environ.get("PORT")
    if not port:
        return None

    async def health(_request: web.Request) -> web.Response:
        from bot.core import db
        from bot.core.client import bot
        from bot.core.assistants import pool
        connected = bool(getattr(bot, 'is_connected', False))
        assistants = any(a.healthy for a in pool.assistants)
        try:
            await db.db.command('ping')
            database = True
        except Exception:
            database = False
        ready = connected and assistants and database
        return web.json_response({'ready': ready, 'telegram': connected, 'assistants': assistants, 'database': database}, status=200 if ready else 503)

    app = web.Application()
    app.router.add_get("/", health)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", int(port))
    await site.start()
    logger.info("Keep-alive HTTP server listening on port %s", port)
    return runner
