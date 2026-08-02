"""YouTube search — resolves a text query or URL into a playable Track.

Playback extraction itself is handled internally by MediaStream's built-in
yt-dlp integration (see bot/core/calls.py); this module only resolves *which*
video to play and fetches display metadata (title/duration/thumbnail).
"""
from __future__ import annotations

import re

from py_yt import Recommendations, Video, VideosSearch

from bot.core.queue import Track
from bot.utils.formatting import format_ms, parse_duration_to_seconds
from bot.utils.logger import get_logger

logger = get_logger(__name__)

_YOUTUBE_HOST_RE = re.compile(r"(youtube\.com|youtu\.be)", re.IGNORECASE)
_VIDEO_ID_RE = re.compile(
    r"(?:youtube\.com/(?:watch\?v=|shorts/)|youtu\.be/)([A-Za-z0-9_-]{11})"
)

# Search took the literal #1 result with zero filtering — no duration
# check, no live-stream/cover/remix filtering. Usually fine, but fetching a
# few more candidates and picking a better one catches the cases where it
# isn't, at the cost of one still-cheap py_yt call (this is metadata search,
# not the yt-dlp extraction step — nowhere near the ~15-20s cost).
_SEARCH_CANDIDATES = 5


def _is_probably_live(item: dict) -> bool:
    # py_yt leaves `duration` falsy for live streams (this file's own
    # pre-existing "Live" display fallback below relies on the same signal).
    return not item.get("duration")


def _pick_best_result(items: list[dict], expected_duration_seconds: int | None) -> dict:
    """Prefer a non-live result; among those, the closest duration match if
    the caller (Spotify/Apple Music) knows the track's real length. Plain
    text searches have no expected duration to compare against, so they just
    get the first non-live candidate — unchanged from the old "take #1"
    behavior, just with live streams deprioritized instead of blindly taken."""
    non_live = [item for item in items if not _is_probably_live(item)]
    pool = non_live or items  # everything found is live: no better option
    if expected_duration_seconds is None:
        return pool[0]
    return min(
        pool,
        key=lambda item: abs(parse_duration_to_seconds(item.get("duration") or "0") - expected_duration_seconds),
    )


def matches(query: str) -> bool:
    return bool(_YOUTUBE_HOST_RE.search(query))


def _extract_video_id(url: str) -> str | None:
    match = _VIDEO_ID_RE.search(url)
    return match.group(1) if match else None


async def resolve(
    query: str, requested_by: int, requested_by_name: str, expected_duration_seconds: int | None = None
) -> Track | None:
    """Resolve `query` (plain text or a youtube.com/youtu.be link) into a
    Track, or None if nothing was found / the search failed. A direct link
    is unambiguous and never uses `expected_duration_seconds` (only the
    plain-text search path below picks among multiple candidates); Spotify/
    Apple Music pass their catalog's known duration through this to bias the
    pick toward the real track over a cover/remix/extended mix sharing its
    title."""
    video_id = _extract_video_id(query) if matches(query) else None

    if video_id:
        link = f"https://www.youtube.com/watch?v={video_id}"
        title, duration, thumbnail = link, "Unknown", None
        try:
            item = await Video.get(video_id)
        except Exception:
            logger.warning("Video.get metadata fetch failed for %s", video_id, exc_info=True)
            item = None
        if item:
            title = item.get("title") or title
            # Video.get()'s duration is {"secondsText": "<raw seconds>"} — a
            # nested dict, unlike VideosSearch's plain "M:SS" string below.
            # Confirmed against the installed py_yt source (core/video.py) —
            # using it unconverted would store a dict as Track.duration and
            # render literally as "({'secondsText': '227'})" in every message.
            seconds_text = (item.get("duration") or {}).get("secondsText")
            if seconds_text and str(seconds_text).isdigit():
                duration = format_ms(int(seconds_text) * 1000)
            thumbnails = item.get("thumbnails") or []
            thumbnail = thumbnails[-1]["url"] if thumbnails else None
        return Track(
            title=title,
            duration=duration,
            link=link,
            thumbnail=thumbnail,
            requested_by=requested_by,
            requested_by_name=requested_by_name,
            source="YouTube",
        )

    try:
        results = await VideosSearch(query, limit=_SEARCH_CANDIDATES).next()
    except Exception:
        logger.warning("YouTube search failed for query=%r", query, exc_info=True)
        return None

    items = results.get("result") or []
    if not items:
        return None
    item = _pick_best_result(items, expected_duration_seconds)
    thumbnails = item.get("thumbnails") or []
    thumbnail = thumbnails[-1]["url"] if thumbnails else None

    return Track(
        title=item.get("title") or "Unknown title",
        duration=item.get("duration") or "Live",
        link=item.get("link") or f"https://www.youtube.com/watch?v={item.get('id')}",
        thumbnail=thumbnail,
        requested_by=requested_by,
        requested_by_name=requested_by_name,
        source="YouTube",
    )


async def get_related(video_link: str) -> Track | None:
    """Find a related video for /autoplay to queue when the chat's queue
    empties. Best-effort like resolve() — never raises, returns None on any
    failure or empty result. `Recommendations.getRelated()`'s items use the
    same plain "M:SS" duration string as the VideosSearch path above (not
    Video.get()'s nested-dict format), confirmed against the installed
    py_yt source and a live call."""
    try:
        result = await Recommendations.getRelated(video_link, limit=10)
    except Exception:
        logger.warning("Related-videos lookup failed for %s", video_link, exc_info=True)
        return None

    for item in result.get("result") or []:
        if item.get("type") != "video" or not item.get("id"):
            continue
        thumbnails = item.get("thumbnails") or []
        return Track(
            title=item.get("title") or "Unknown title",
            duration=item.get("duration") or "Unknown",
            link=f"https://www.youtube.com/watch?v={item['id']}",
            thumbnail=thumbnails[-1]["url"] if thumbnails else None,
            requested_by=0,
            requested_by_name="Autoplay",
            source="YouTube (Autoplay)",
        )
    return None
