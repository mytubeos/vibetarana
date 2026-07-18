"""Small shared display-formatting helpers."""
from __future__ import annotations


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
