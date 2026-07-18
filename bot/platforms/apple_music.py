"""Apple Music link resolution — extracts track metadata via Apple's public
iTunes Search/Lookup API (https://itunes.apple.com/lookup, no auth required).

Like Spotify, Apple Music doesn't expose raw audio to third-party apps, so
the actual stream still comes from YouTube: this module resolves
title+artist from Apple's catalog, then delegates to bot.platforms.youtube
for a playable link.

Only resolves URLs that point at a specific track (the `?i=<id>` query
param) — an album/playlist-only link (no `?i=`) returns None, same as "not
found", since there's no single track to queue.
"""
from __future__ import annotations

import re

import aiohttp

from bot.core.queue import Track
from bot.platforms.youtube import resolve as youtube_resolve
from bot.utils.formatting import format_ms
from bot.utils.logger import get_logger

logger = get_logger(__name__)

_HOST_RE = re.compile(r"music\.apple\.com", re.IGNORECASE)
_TRACK_ID_RE = re.compile(r"[?&]i=(\d+)")


def matches(query: str) -> bool:
    return bool(_HOST_RE.search(query))


async def resolve(query: str, requested_by: int, requested_by_name: str) -> Track | None:
    match = _TRACK_ID_RE.search(query)
    if not match:
        logger.info("Apple Music link has no track id (?i=), likely an album/playlist link: %r", query)
        return None

    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                "https://itunes.apple.com/lookup",
                params={"id": match.group(1)},
            ) as resp:
                if resp.status != 200:
                    logger.warning("Apple Music lookup failed with status %d", resp.status)
                    return None
                # content_type=None: iTunes serves this endpoint as
                # text/javascript, not application/json — aiohttp's default
                # strict content-type check raises ContentTypeError on that
                # even though the body is valid JSON (confirmed against the
                # live API).
                data = await resp.json(content_type=None)
    except aiohttp.ClientError:
        logger.warning("Apple Music lookup request failed", exc_info=True)
        return None

    results = data.get("results") or []
    if not results:
        return None
    item = results[0]

    title = item.get("trackName") or "Unknown title"
    artist = item.get("artistName") or ""
    search_query = f"{title} {artist}".strip()

    yt_track = await youtube_resolve(search_query, requested_by, requested_by_name)
    if yt_track is None:
        return None

    duration_ms = item.get("trackTimeMillis")

    return Track(
        title=f"{title} - {artist}" if artist else title,
        duration=format_ms(duration_ms) if isinstance(duration_ms, int) else yt_track.duration,
        link=yt_track.link,
        thumbnail=item.get("artworkUrl100") or yt_track.thumbnail,
        requested_by=requested_by,
        requested_by_name=requested_by_name,
        source="Apple Music",
    )
