"""Pure regex/URL-matching tests for the Phase 2a resolvers — no network.
Each module's resolve() hits a real API, so it's exercised only indirectly
via bot/platforms/__init__.py's registration (see test_platforms.py); these
tests cover the offline-testable matching/ID-extraction logic directly.
"""
import asyncio

import bot.platforms.apple_music as apple_music
import bot.platforms.direct_link as direct_link
import bot.platforms.soundcloud as soundcloud
import bot.platforms.spotify as spotify


def test_spotify_matches_track_link():
    assert spotify.matches("https://open.spotify.com/track/4cOdK2wGLETKBW3PvgPWqT") is True


def test_spotify_does_not_match_other_links():
    assert spotify.matches("https://www.youtube.com/watch?v=dQw4w9WgXcQ") is False
    assert spotify.matches("just a plain search query") is False


def test_spotify_track_id_regex_extracts_id():
    match = spotify._TRACK_ID_RE.search(
        "https://open.spotify.com/track/4cOdK2wGLETKBW3PvgPWqT?si=abc123"
    )
    assert match is not None
    assert match.group(1) == "4cOdK2wGLETKBW3PvgPWqT"


def test_apple_music_matches_host():
    assert apple_music.matches("https://music.apple.com/us/album/song/1440818419?i=1440818425") is True


def test_apple_music_does_not_match_other_links():
    assert apple_music.matches("https://open.spotify.com/track/4cOdK2wGLETKBW3PvgPWqT") is False


def test_apple_music_track_id_regex_requires_i_param():
    with_track = apple_music._TRACK_ID_RE.search(
        "https://music.apple.com/us/album/song/1440818419?i=1440818425"
    )
    assert with_track is not None
    assert with_track.group(1) == "1440818425"

    album_only = apple_music._TRACK_ID_RE.search(
        "https://music.apple.com/us/album/some-album/1440818419"
    )
    assert album_only is None


def test_soundcloud_matches_host():
    assert soundcloud.matches("https://soundcloud.com/artist/track-name") is True


def test_soundcloud_does_not_match_other_links():
    assert soundcloud.matches("https://music.apple.com/us/album/song/1440818419?i=1440818425") is False
    assert soundcloud.matches("plain text query") is False


def test_direct_link_matches_generic_http_url():
    assert direct_link.matches("https://example.com/song.mp3") is True
    assert direct_link.matches("http://example.com/video.mp4") is True


def test_direct_link_does_not_match_plain_text():
    assert direct_link.matches("just a plain search query") is False
    assert direct_link.matches("ftp://example.com/song.mp3") is False


def test_direct_link_resolve_uses_filename_as_title():
    track = asyncio.run(
        direct_link.resolve("https://example.com/path/to/song.mp3", requested_by=1, requested_by_name="tester")
    )
    assert track.title == "song.mp3"
    assert track.link == "https://example.com/path/to/song.mp3"
    assert track.source == "Direct Link"


def test_direct_link_resolve_falls_back_to_full_url_when_no_filename():
    track = asyncio.run(
        direct_link.resolve("https://example.com/", requested_by=1, requested_by_name="tester")
    )
    assert track.title == "https://example.com/"
