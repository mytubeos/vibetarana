"""SoundCloud link resolution — extracts a track title via SoundCloud's
public oEmbed endpoint (https://soundcloud.com/oembed, no auth required).

Deliberately does NOT attempt to stream directly from SoundCloud via
yt-dlp's SoundCloud extractor: as of mid-2026 that extractor has open
client_id/format-lookup reliability issues (intermittent 401s and 404s).
Metadata-then-YouTube-fallback is slower but consistent with how Spotify and
Apple Music are handled here, and doesn't depend on SoundCloud's extractor
working on any given day.
"""
from __future__ import annotations

import re

import aiohttp

from bot.core.queue import Track
from bot.platforms.youtube import resolve as youtube_resolve
from bot.utils.logger import get_logger

logger = get_logger(__name__)

_HOST_RE = re.compile(r"soundcloud\.com/", re.IGNORECASE)


def matches(query: str) -> bool:
    return bool(_HOST_RE.search(query))


async def resolve(query: str, requested_by: int, requested_by_name: str) -> Track | None:
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                "https://soundcloud.com/oembed",
                params={"url": query, "format": "json"},
            ) as resp:
                if resp.status != 200:
                    logger.warning("SoundCloud oEmbed lookup failed with status %d", resp.status)
                    return None
                # content_type=None defensively — verified SoundCloud's oEmbed
                # sends correct application/json today, but Apple's iTunes
                # API claiming text/javascript for valid JSON (see
                # apple_music.py) is a reminder not to trust that holding.
                data = await resp.json(content_type=None)
    except aiohttp.ClientError:
        logger.warning("SoundCloud oEmbed request failed", exc_info=True)
        return None

    # oEmbed titles are typically "Artist - Track Title", which also makes a
    # good YouTube search query as-is.
    title = data.get("title") or "Unknown title"

    yt_track = await youtube_resolve(title, requested_by, requested_by_name)
    if yt_track is None:
        return None

    return Track(
        title=title,
        duration=yt_track.duration,  # oEmbed doesn't provide duration
        link=yt_track.link,
        thumbnail=data.get("thumbnail_url") or yt_track.thumbnail,
        requested_by=requested_by,
        requested_by_name=requested_by_name,
        source="SoundCloud",
    )
