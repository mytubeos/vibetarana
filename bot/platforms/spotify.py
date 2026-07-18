"""Spotify link resolution — extracts track metadata via the Spotify Web API
(Client Credentials flow: app-level auth, no user login, only public catalog
data — https://developer.spotify.com/documentation/web-api/tutorials/client-credentials-flow).

Spotify does not expose raw audio to third-party apps, so the actual stream
still comes from YouTube: this module resolves title+artist from Spotify,
then delegates to bot.platforms.youtube for a playable link, keeping
Spotify's own title/artist/thumbnail for display (source="Spotify").

Gracefully degrades to "unsupported" (returns None, logs a warning) if
SPOTIFY_CLIENT_ID/SPOTIFY_CLIENT_SECRET aren't configured — Spotify support
is optional, not required to run the bot.
"""
from __future__ import annotations

import asyncio
import re
import time

import aiohttp

from bot.core.queue import Track
from bot.platforms.youtube import resolve as youtube_resolve
from bot.utils.formatting import format_ms
from bot.utils.logger import get_logger

logger = get_logger(__name__)

_TRACK_ID_RE = re.compile(r"open\.spotify\.com/track/([A-Za-z0-9]+)")

_token: str | None = None
_token_expiry: float = 0.0


def matches(query: str) -> bool:
    return bool(_TRACK_ID_RE.search(query))


async def _get_access_token() -> str | None:
    """Cached app-level access token; refreshed a minute before it expires.

    Imports `config.settings` lazily (not at module level) so merely
    importing this module — e.g. to register it, or to unit-test matches()
    — never triggers config.py's fail-fast .env validation. Only actually
    resolving a Spotify link needs real settings, and by then the real bot
    process has already validated config in main.py anyway.
    """
    from config import settings

    global _token, _token_expiry
    if not settings.spotify_client_id or not settings.spotify_client_secret:
        return None
    if _token is not None and time.monotonic() < _token_expiry:
        return _token

    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                "https://accounts.spotify.com/api/token",
                data={"grant_type": "client_credentials"},
                auth=aiohttp.BasicAuth(settings.spotify_client_id, settings.spotify_client_secret),
            ) as resp:
                if resp.status != 200:
                    logger.warning("Spotify token request failed with status %d", resp.status)
                    return None
                data = await resp.json(content_type=None)
    except (aiohttp.ClientError, asyncio.TimeoutError):
        # asyncio.TimeoutError isn't an aiohttp.ClientError subclass (verified
        # against the installed aiohttp) — a session-level total-timeout raises
        # it directly, so it needs its own catch or a slow Spotify response
        # would propagate uncaught out of this "never raises" resolver.
        logger.warning("Spotify token request failed", exc_info=True)
        return None

    _token = data["access_token"]
    _token_expiry = time.monotonic() + data.get("expires_in", 3600) - 60
    return _token


async def resolve(query: str, requested_by: int, requested_by_name: str) -> Track | None:
    match = _TRACK_ID_RE.search(query)
    if not match:
        return None

    token = await _get_access_token()
    if token is None:
        logger.warning(
            "Spotify link received but SPOTIFY_CLIENT_ID/SPOTIFY_CLIENT_SECRET "
            "aren't configured — see .env.example."
        )
        return None

    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                f"https://api.spotify.com/v1/tracks/{match.group(1)}",
                headers={"Authorization": f"Bearer {token}"},
            ) as resp:
                if resp.status != 200:
                    logger.warning("Spotify track lookup failed with status %d", resp.status)
                    return None
                data = await resp.json(content_type=None)
    except (aiohttp.ClientError, asyncio.TimeoutError):
        logger.warning("Spotify track lookup request failed", exc_info=True)
        return None

    title = data.get("name") or "Unknown title"
    artists = ", ".join(a["name"] for a in data.get("artists", []) if a.get("name"))
    search_query = f"{title} {artists}".strip()

    yt_track = await youtube_resolve(search_query, requested_by, requested_by_name)
    if yt_track is None:
        return None

    images = (data.get("album") or {}).get("images") or []
    duration_ms = data.get("duration_ms")

    return Track(
        title=f"{title} - {artists}" if artists else title,
        duration=format_ms(duration_ms) if isinstance(duration_ms, int) else yt_track.duration,
        link=yt_track.link,  # actual playable stream is still YouTube's
        thumbnail=(images[0]["url"] if images else yt_track.thumbnail),
        requested_by=requested_by,
        requested_by_name=requested_by_name,
        source="Spotify",
    )
