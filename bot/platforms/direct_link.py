"""Direct playable-URL resolution — the catch-all for a raw http(s) link that
no other resolver claimed (YouTube/Spotify/Apple Music/SoundCloud all get a
chance to match first — see registration order in bot/platforms/__init__.py).

No metadata lookup is possible for an arbitrary URL, so this just wraps it
as a Track with the URL's filename as a placeholder title — MediaStream/
ffmpeg handles the actual streaming directly from `link`, same as every
other resolver, it just skips the title/duration/thumbnail step entirely.

Deliberately registered LAST and non-default: if it matched first (or were
the default), it would wrongly swallow every Spotify/Apple Music/SoundCloud
link too, since those are also http(s) URLs.
"""
from __future__ import annotations

import re
from urllib.parse import urlparse

from bot.core.queue import Track
from bot.utils.logger import get_logger

logger = get_logger(__name__)

_URL_RE = re.compile(r"^https?://", re.IGNORECASE)


def matches(query: str) -> bool:
    return bool(_URL_RE.match(query.strip()))


async def resolve(query: str, requested_by: int, requested_by_name: str) -> Track | None:
    link = query.strip()
    filename = urlparse(link).path.rsplit("/", 1)[-1]
    return Track(
        title=filename or link,
        duration="Unknown",
        link=link,
        thumbnail=None,
        requested_by=requested_by,
        requested_by_name=requested_by_name,
        source="Direct Link",
    )
