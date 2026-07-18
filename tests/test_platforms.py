"""Tests for the bot/platforms registry/dispatcher — no network involved.
Uses dummy resolvers and swaps out the module's registry state per-test so
the real YouTube resolver (which would hit the network) is never exercised.
"""
import asyncio

import pytest

import bot.platforms as platforms
from bot.core.queue import Track


class _DummyResolver:
    def __init__(self, name: str, prefix: str) -> None:
        self.name = name
        self.prefix = prefix

    def matches(self, query: str) -> bool:
        return query.startswith(self.prefix)

    async def resolve(self, query: str, requested_by: int, requested_by_name: str) -> Track:
        return Track(
            title=query, duration="0:00", link="https://example.com", thumbnail=None,
            requested_by=requested_by, requested_by_name=requested_by_name, source=self.name,
        )


@pytest.fixture
def clean_registry(monkeypatch):
    monkeypatch.setattr(platforms, "_resolvers", [])
    monkeypatch.setattr(platforms, "_default", None)
    yield


def test_matching_resolver_is_used_over_default(clean_registry):
    default = _DummyResolver("default", "zzz-never:")
    specific = _DummyResolver("specific", "special:")
    platforms.register(default, default=True)
    platforms.register(specific)

    track = asyncio.run(platforms.resolve("special: query", 1, "tester"))
    assert track.source == "specific"


def test_falls_back_to_default_when_nothing_matches(clean_registry):
    default = _DummyResolver("default", "zzz-never:")
    specific = _DummyResolver("specific", "special:")
    platforms.register(default, default=True)
    platforms.register(specific)

    track = asyncio.run(platforms.resolve("plain search terms", 1, "tester"))
    assert track.source == "default"


def test_default_with_catch_all_matches_still_resolves(clean_registry):
    # A default whose own matches() also happens to match everything is
    # found via the ordinary loop now (not specially skipped) — either path
    # calls the same object's resolve(), so the observable result is
    # unchanged either way.
    catch_all_default = _DummyResolver("default", "")  # matches() on "" -> always True
    platforms.register(catch_all_default, default=True)

    track = asyncio.run(platforms.resolve("anything", 1, "tester"))
    assert track.source == "default"


def test_default_resolvers_own_matches_is_not_skipped(clean_registry):
    # Regression test for a real bug: the default used to be excluded from
    # the matching loop entirely, so a later broad/catch-all resolver could
    # steal a query the default's own narrow matches() should have claimed
    # (this is exactly what happened with youtube vs. direct_link — see
    # test_youtube_link_is_not_shadowed_by_direct_link below for the real,
    # non-dummy version of this scenario).
    default_with_narrow_match = _DummyResolver("default", "narrow:")
    broad_catch_all = _DummyResolver("catch_all", "")  # matches everything
    platforms.register(default_with_narrow_match, default=True)
    platforms.register(broad_catch_all)

    track = asyncio.run(platforms.resolve("narrow: query", 1, "tester"))
    assert track.source == "default"


def test_register_rejects_object_missing_protocol_methods(clean_registry):
    class NotAResolver:
        pass

    with pytest.raises(TypeError):
        platforms.register(NotAResolver())


def test_real_youtube_link_is_claimed_before_direct_link_gets_a_chance():
    # Pure logic, no network: a real YouTube URL matches BOTH youtube.py
    # (narrow, real domain check) and direct_link.py (any http(s) URL) — the
    # only thing preventing direct_link from wrongly stealing it is (a)
    # youtube's own matches() actually being checked (see the fix in
    # bot/platforms/__init__.py's resolve() — it no longer skips the
    # default), and (b) youtube being registered before direct_link.
    import bot.platforms.direct_link as direct_link
    import bot.platforms.youtube as youtube

    url = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
    assert youtube.matches(url) is True
    assert direct_link.matches(url) is True  # confirms this scenario is real, not moot
    assert platforms._resolvers.index(youtube) < platforms._resolvers.index(direct_link)


def test_real_registry_orders_specific_resolvers_before_direct_link():
    # Deliberately does NOT use clean_registry — inspects the actual,
    # real registration bot/platforms/__init__.py performs at import time.
    # direct_link.matches() accepts ANY http(s) URL, so if it were registered
    # before (or instead of after) spotify/apple_music/soundcloud, it would
    # shadow every link from those three services.
    import bot.platforms.apple_music as apple_music
    import bot.platforms.direct_link as direct_link
    import bot.platforms.soundcloud as soundcloud
    import bot.platforms.spotify as spotify

    resolvers = platforms._resolvers
    direct_link_index = resolvers.index(direct_link)
    for specific in (spotify, apple_music, soundcloud):
        assert resolvers.index(specific) < direct_link_index
