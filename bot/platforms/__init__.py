"""Platform resolver registry — dispatches a /play query to the right source
module. Phase 1 only registers YouTube (as the catch-all default), but Phase 2
sources (Spotify/Apple Music/SoundCloud) plug in here without touching
bot/plugins/play.py or this dispatcher's call sites: each new module exposes
module-level `matches()`/`resolve()` and calls `register(module)` at import.
"""
from __future__ import annotations

from typing import Protocol, runtime_checkable

from bot.core.queue import Track
from bot.utils.logger import get_logger

logger = get_logger(__name__)


@runtime_checkable
class PlatformResolver(Protocol):
    def matches(self, query: str) -> bool: ...

    async def resolve(self, query: str, requested_by: int, requested_by_name: str) -> Track | None: ...


_resolvers: list[PlatformResolver] = []
_default: PlatformResolver | None = None


def register(resolver: PlatformResolver, *, default: bool = False) -> None:
    """Register a platform module. `default=True` marks it as the final
    fallback used when NO resolver's matches() — including its own — claims
    the query (there must be exactly one default; YouTube is Phase 1's,
    since its own resolve() already handles a plain-text search term)."""
    global _default
    if not isinstance(resolver, PlatformResolver):
        raise TypeError(f"{resolver!r} does not implement PlatformResolver")
    _resolvers.append(resolver)
    if default:
        _default = resolver


async def resolve(query: str, requested_by: int, requested_by_name: str) -> Track | None:
    # The default participates in this loop like any other resolver — it is
    # NOT skipped. It used to be, on the theory that "the default should
    # only be reached via explicit fallback" — but YouTube (Phase 1's
    # default) has its own narrow, real matches() (youtube.com/youtu.be), and
    # skipping it let a later catch-all resolver like direct_link (any
    # http(s) URL) wrongly claim YouTube links first, losing YouTube's real
    # title/duration/thumbnail lookup. `_default` now means only "what to
    # call if nothing — including itself — matched", i.e. a plain-text
    # search term.
    for resolver in _resolvers:
        if resolver.matches(query):
            return await resolver.resolve(query, requested_by, requested_by_name)
    if _default is None:
        logger.error("No default platform resolver registered")
        return None
    return await _default.resolve(query, requested_by, requested_by_name)


from bot.platforms import apple_music, direct_link, soundcloud, spotify, youtube  # noqa: E402

register(youtube, default=True)
register(spotify)
register(apple_music)
register(soundcloud)
# direct_link matches ANY http(s) URL, so it must be registered last — if it
# came first it would swallow every Spotify/Apple Music/SoundCloud link too.
register(direct_link)
