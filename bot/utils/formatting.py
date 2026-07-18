"""Small shared display-formatting helpers."""
from __future__ import annotations

from bot.core.queue import Track


def format_ms(duration_ms: int) -> str:
    """Convert a millisecond duration (as returned by Spotify/Apple Music
    metadata APIs) into a `M:SS` (or `H:MM:SS`) string matching the format
    youtube.py already produces."""
    total_seconds = max(0, duration_ms) // 1000
    hours, remainder = divmod(total_seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{seconds:02d}"
    return f"{minutes}:{seconds:02d}"


def track_block(track: Track, *, heading: str, footer: str | None = None) -> str:
    """Multi-line track display shared by /play, /vplay, and /skip's
    "now playing"/"added to queue" replies, instead of cramming title +
    duration + requester onto one long line (YouTube titles routinely run
    60+ characters). `footer` adds a trailing status line (e.g. "▶️
    Playing") separated by a rule — used for "now playing", omitted for
    "added to queue" since that track isn't playing yet."""
    lines = [
        heading,
        "━━━━━━━━━━━━━━",
        f"🎧 {track.title}",
        f"📡 Source: {track.source}",
        f"⏱ Duration: {track.duration}",
        f"🙋 Requested By: {track.requested_by_name}",
    ]
    if footer:
        lines += ["━━━━━━━━━━━━━━", footer]
    return "\n".join(lines)


def parse_duration_to_seconds(duration: str) -> int:
    """Inverse of format_ms — parses an `M:SS`/`H:MM:SS` string back to
    seconds, for summing a queue's total run time. Returns 0 for anything
    that doesn't parse as plain colon-separated integers, so one odd entry
    doesn't blow up the whole total."""
    try:
        parts = [int(p) for p in duration.split(":")]
    except ValueError:
        return 0
    seconds = 0
    for part in parts:
        seconds = seconds * 60 + part
    return seconds


def format_seconds(total_seconds: int) -> str:
    """Format a raw seconds total as e.g. '23m 18s' or '1h 5m' — used for
    a queue's total running time, coarser than format_ms's M:SS since exact
    seconds don't matter much once you're summing several tracks."""
    hours, remainder = divmod(max(0, total_seconds), 3600)
    minutes, seconds = divmod(remainder, 60)
    if hours:
        return f"{hours}h {minutes}m"
    if minutes:
        return f"{minutes}m {seconds}s"
    return f"{seconds}s"
