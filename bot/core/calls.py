"""Join/play/pause/resume/stop/seek wiring on top of the assistant pool, plus
the PyTgCalls update handlers that auto-advance the queue and auto-leave an
empty voice chat.
"""
from __future__ import annotations

import asyncio
import re
import shlex
import time
from pathlib import Path
from typing import Optional, Tuple

from pyrogram.errors import ChannelInvalid
from pytgcalls import filters as fl
from pytgcalls.exceptions import YtDlpError
from pytgcalls.ffmpeg import cleanup_commands
from pytgcalls.types import ChatUpdate, MediaStream, StreamEnded
from pytgcalls.types.raw.video_parameters import VideoParameters
from pytgcalls.types.stream.video_quality import VideoQuality
from pytgcalls.ytdlp import YtDlp

from bot.core import db
from bot.core.session import get_session, operation, cancel_pending
from bot.core.media import youtube_id, local_media, validate_track
from config import settings
from bot.core.assistants import Assistant, pool
from bot.core.client import bot
from bot.core.queue import Track, queues
from bot.platforms import youtube
from bot.utils.formatting import playback_keyboard, send_track_card
from bot.utils.logger import get_logger

logger = get_logger(__name__)

AUTO_LEAVE_GRACE_SECONDS = 75
MAX_CONSECUTIVE_PLAY_FAILURES = 3

# The VPS/systemd convention — relative to the working directory, and always
# writable (the Dockerfile creates it: `RUN mkdir -p cookies downloads`).
# yt-dlp must point --cookies at a writable path: it rewrites the cookie jar
# on every run to persist rotated session cookies, and a read-only target
# crashes the whole extraction (confirmed from a live log: `OSError: [Errno
# 30] Read-only file system: '/etc/secrets/cookies.txt'`).
COOKIES_PATH = Path("cookies/cookies.txt")

# Render's Secret Files mount — filename field rejects slashes, so a Secret
# File named "cookies.txt" always lands here. Read-only, so setup_cookies()
# below copies it into COOKIES_PATH rather than pointing yt-dlp at it directly.
RENDER_SECRET_COOKIES_PATH = Path("/etc/secrets/cookies.txt")

# yt-dlp auto-loads this from its working directory (verified via `yt-dlp
# --verbose`, which logs `Home config "yt-dlp.conf": [...]` for exactly this
# file) with no extra flag needed. Deliberately NOT passed as
# MediaStream(ytdlp_parameters=...): py-tgcalls' cleanup_commands() drops any
# --long-form flag that has no single-dash alias in `yt-dlp -h full` output
# before it ever reaches the yt-dlp subprocess, and --cookies has no such
# alias — confirmed empirically, that flag was silently discarded every time.
YTDLP_CONFIG_PATH = Path("yt-dlp.conf")

_pending_leave_tasks: dict[int, asyncio.Task] = {}

# Resolving a track's stream URL(s) takes ~15-20s now (see
# patch_ytdlp_timeout's comment) — most of that is unavoidable YouTube-side,
# but for the *next* queued track we already know is coming, there's no
# reason to pay it again right at skip/auto-advance time. schedule_prefetch()
# resolves it in the background while the current track plays; _play_track()
# uses the cached URLs instead of re-resolving if they're ready in time.
# Keyed by id(track) — a track's own object identity is a fine cache key for
# its short lifetime in one chat's queue. Entries are popped as soon as
# they're consumed; a prefetched track that never gets played (cleared/
# shuffled/stopped first) instead ages out via _PREFETCH_CACHE_MAX_ENTRIES.
_prefetch_cache: dict[tuple[str, bool], tuple[tuple[str, str], float]] = {}
_local_paths: dict[int, Path] = {}
_extract_slots = asyncio.Semaphore(settings.extraction_concurrency)
_inflight: dict[tuple[str, bool], asyncio.Task] = {}
_audio_url_cache: dict[str, tuple[tuple[str, str], float]] = {}
_prefetch_tasks: dict[tuple[str, bool], asyncio.Task] = {}
_PREFETCH_CACHE_MAX_ENTRIES = 20

# Bot-wide companion to _prefetch_cache above: that one is per-chat and
# consumed once (popped the moment a track plays), so replaying the same
# song a few minutes later — even in a different chat — paid full
# extraction again. This is keyed by video ID instead of object identity, so
# it survives across chats/queues. googlevideo.com URLs are typically valid
# for several hours; stay well under that so a cache "hit" is never a stale
# link the player rejects mid-playback.
_VIDEO_ID_RE = re.compile(r"(?:youtube\.com/(?:watch\?v=|shorts/)|youtu\.be/)([A-Za-z0-9_-]{11})")
_RESOLVED_URL_CACHE_TTL_SECONDS = 4 * 3600
# Bumped from 200 (2026-08-04) — each entry is just two URL strings plus a
# timestamp, a few KB at most, so even a few thousand entries costs single-
# digit MB. Render's free-tier memory isn't remotely the constraint here;
# the TTL above already bounds how long anything sticks around.
_RESOLVED_URL_CACHE_MAX_ENTRIES = 3000
# time.time() (wall-clock), not time.monotonic() — these entries are
# persisted to MongoDB (see restore_resolved_url_cache() below) so a
# redeploy doesn't throw the cache away, and monotonic's zero point resets
# on every process start, making a value saved by one process meaningless
# to compare against time.monotonic() in the next.
_resolved_url_cache: dict[str, tuple[str, str, float]] = {}


def _cached_urls_get(link: str) -> tuple[str, str] | None:
    video_id = youtube_id(link)
    match = _VIDEO_ID_RE.search(f"https://youtube.com/watch?v={video_id}") if video_id else None
    if match is None:
        return None
    entry = _resolved_url_cache.get(match.group(1))
    if entry is None:
        return None
    video_url, audio_url, resolved_at = entry
    if time.time() - resolved_at > _RESOLVED_URL_CACHE_TTL_SECONDS:
        del _resolved_url_cache[match.group(1)]
        return None
    return video_url, audio_url


async def _cached_urls_put(link: str, urls: tuple[str, str]) -> None:
    video_id = youtube_id(link)
    match = _VIDEO_ID_RE.search(f"https://youtube.com/watch?v={video_id}") if video_id else None
    if match is None:
        # Non-YouTube link (e.g. bot/platforms/direct_link.py) — no stable
        # ID to key a cross-chat cache on, so this is a silent no-op rather
        # than a cache that can never be a hit.
        return
    video_id = match.group(1)
    resolved_at = time.time()
    _resolved_url_cache[video_id] = (*urls, resolved_at)
    while len(_resolved_url_cache) > _RESOLVED_URL_CACHE_MAX_ENTRIES:
        _resolved_url_cache.pop(next(iter(_resolved_url_cache)))
    try:
        await db.save_resolved_url(video_id, urls[0], urls[1], resolved_at)
    except Exception:
        # Best-effort durability — an in-memory-only cache entry still
        # works fine for the rest of this process's life, it just won't
        # survive the next restart. Never let a Mongo hiccup break playback.
        logger.warning("Failed to persist resolved-URL cache entry for %s", video_id, exc_info=True)


async def restore_resolved_url_cache() -> None:
    """Call once at startup, after db.connect(). Repopulates the in-memory
    resolved-URL cache from MongoDB so a redeploy doesn't throw away every
    recently-resolved song's cache-hit — otherwise every restart (frequent
    during active development, and Render's own occasional restarts) resets
    this to fully cold. Skips anything already past its TTL; a googlevideo
    URL from hours ago is worthless regardless of how it got here."""
    try:
        stored = await db.get_resolved_url_cache()
    except Exception:
        logger.warning("Failed to restore resolved-URL cache from MongoDB", exc_info=True)
        return
    now = time.time()
    for video_id, (video_url, audio_url, resolved_at) in list(stored.items())[-_RESOLVED_URL_CACHE_MAX_ENTRIES:]:
        if now - resolved_at > _RESOLVED_URL_CACHE_TTL_SECONDS:
            continue
        _resolved_url_cache[video_id] = (video_url, audio_url, resolved_at)
    if _resolved_url_cache:
        logger.info("Restored %d cached resolved URL(s) from MongoDB", len(_resolved_url_cache))


async def _record_play(video_id: str) -> None:
    try:
        await db.record_play(video_id)
    except Exception:
        logger.warning("Failed to record play count for %s", video_id, exc_info=True)


# How many of the most-played songs prewarm_popular_songs_loop() keeps
# perpetually fresh. Deliberately modest — each one that's actually gone
# cold costs a real extraction (~15-20s of subprocess/network work), and
# this runs unattended on Render's shared free-tier CPU; not worth hammering
# it for long-tail songs that were only ever played once anyway.
_PREWARM_TOP_N = 10


async def prewarm_popular_songs_loop(interval_seconds: int = 1800) -> None:
    """Periodically re-resolves the most-played songs (tracked via
    db.record_play(), fired from _play_track() on every successful play) so
    a request for one of them is always a cache hit instead of only ever
    caching reactively after someone already waited through a cold
    extraction once. Skips anything still within its TTL — cheap, since
    that's the common case once a song's actually warm."""
    while True:
        await asyncio.sleep(interval_seconds)
        try:
            top_video_ids = await db.get_top_played_video_ids(_PREWARM_TOP_N)
        except Exception:
            logger.warning("Failed to fetch top-played video IDs for pre-warming", exc_info=True)
            continue
        for video_id in top_video_ids:
            link = f"https://www.youtube.com/watch?v={video_id}"
            if _cached_urls_get(link) is not None:
                continue  # still warm, nothing to do
            try:
                urls = await extract_urls(link, True)
            except Exception:
                logger.warning("Pre-warm extraction failed for %s", video_id, exc_info=True)
                continue
            await _cached_urls_put(link, urls)


# Bumped from HD_720p — pytgcalls supports up to UHD_4K, but Render's free
# tier has a tight, shared CPU budget that a jump straight to 4K would blow
# through on /vplay (more ffmpeg encode/relay work per stream). FHD_1080p is
# a deliberate one-step-up test, not a max-out — if playback stability
# regresses, this is the first thing to revert.
_DEFAULT_VIDEO_PARAMETERS = VideoParameters(*VideoQuality.FHD_1080p.value, adjust_by_height=False)


def setup_cookies() -> None:
    """Call once at startup. Writes yt-dlp.conf pointing at a writable
    cookies.txt if one is found or can be made — YouTube throttles/blocks
    datacenter IPs (VPS/PaaS hosts) without one (see README's "Known
    operational risks"). No-op if no cookies file exists anywhere, so
    deploys that don't need it are unaffected."""
    if not COOKIES_PATH.exists():
        if not RENDER_SECRET_COOKIES_PATH.exists():
            return
        COOKIES_PATH.parent.mkdir(parents=True, exist_ok=True)
        COOKIES_PATH.write_bytes(RENDER_SECRET_COOKIES_PATH.read_bytes())
        logger.info("Copied cookies.txt from Render's (read-only) Secret Files mount to a writable path")
    YTDLP_CONFIG_PATH.write_text(f"--cookies {COOKIES_PATH}\n")
    logger.info("yt-dlp cookies config written, using %s", COOKIES_PATH)


# py-tgcalls hardcodes this at 20s (see its ytdlp.py), timing extraction from
# webpage fetch through format resolution. That was fine when yt-dlp could
# resolve a format list on its own; YouTube now additionally requires solving
# a signature/"n" JS challenge (see README/main.py's Deno note), which alone
# measured ~17s live even warm-cached — 20s total is too tight and intermittently
# times out. No public knob for this, so the whole function is copied from
# py-tgcalls' own source with only the timeout number changed.
YTDLP_SUBPROCESS_TIMEOUT_SECONDS = 60


async def _run_ytdlp_extract(
    link: str, video_parameters: VideoParameters, add_commands: Optional[str], player_client: str
) -> Tuple[str, str]:
    commands = [
        "yt-dlp",
        "-g",
        "-f",
        ('bestaudio/best' if add_commands == '__audio__' else 'bestvideo[vcodec~="(vp09|avc1)"]+m4a/best'),
        '--no-playlist',
        "-S",
        f"res:{min(video_parameters.width, video_parameters.height)}",
        "--extractor-args",
        f"youtube:player_client={player_client}",
        "--no-warnings",
    ]
    if add_commands and add_commands != "__audio__":
        commands += await cleanup_commands(shlex.split(add_commands), "yt-dlp", ["-f", "-g", "--no-warnings"])
    commands.append(link)
    proc = await asyncio.create_subprocess_exec(
        *commands, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), YTDLP_SUBPROCESS_TIMEOUT_SECONDS)
    except (asyncio.TimeoutError, asyncio.CancelledError):
        if proc.returncode is None:
            proc.kill()
        await proc.communicate()
        raise
    if proc.returncode:
        raise YtDlpError(stderr.decode() or "yt-dlp extraction failed")
    data = stdout.decode().strip().split("\n")
    if data and data[0]:
        return data[0], data[1] if len(data) >= 2 else data[0]
    raise YtDlpError("No video URLs found")


# yt-dlp's own wording for YouTube's datacenter-IP bot-check (confirmed
# live from a real Render failure, 2026-08-03 — cookies.txt had gone stale).
# Distinct from every other extraction failure (SABR format-unavailability,
# a dead/region-blocked video, etc.), and the fix is completely different
# (re-export cookies, not a code change) — worth its own loud, greppable log
# line instead of making whoever's debugging this at 2am read a traceback to
# tell the two apart.
_COOKIE_AUTH_ERROR_MARKER = "Sign in to confirm"


def patch_ytdlp_timeout() -> None:
    """Call once at startup, after setup_cookies(). Replaces YtDlp.extract
    with a copy of itself that waits YTDLP_SUBPROCESS_TIMEOUT_SECONDS instead
    of py-tgcalls' hardcoded 20.

    Deliberately back to a single "web" attempt, no "android"/"tv_embedded"
    speed trick. Both were measured working locally (WSL2, residential IP —
    android ~6s, tv_embedded ~5-7s at 720p) but Render's live logs
    (2026-08-04) showed "tv_embedded+android extraction failed, retrying
    with web" for every single video over a ~15min window with zero
    exceptions — a 100% failure rate that never showed up locally. Most
    likely YouTube specifically discriminates against these
    JS-challenge-skipping clients by IP reputation, punishing datacenter
    ranges harder than residential ones — exactly the kind of thing this
    codebase's own README already warned "if playback works locally but
    fails on the VPS" about, just for a client trick instead of cookies.
    Net effect on Render: every extraction paid for a failing first attempt
    AND the slow web fallback — slower than just using web alone. If
    revisiting this, verify against a real Render deploy, not just a local
    residential-IP test — that gap is exactly what bit this once already."""

    async def patched_extract(
        link: Optional[str],
        video_parameters: VideoParameters,
        add_commands: Optional[str],
    ) -> Tuple[Optional[str], Optional[str]]:
        if link is None:
            return None, None
        try:
            try:
                return await _run_ytdlp_extract(link, video_parameters, add_commands, "web")
            except YtDlpError as error:
                if _COOKIE_AUTH_ERROR_MARKER in str(error):
                    logger.warning(
                        "COOKIE AUTH FAILURE: YouTube rejected the web player client for %s with "
                        "'Sign in to confirm you're not a bot' — this means cookies.txt is stale/invalid, "
                        "NOT a code bug. Re-export fresh cookies (full export incl. google.com auth "
                        "cookies, not youtube.com-only) and update Render's cookies.txt Secret File. "
                        "See README's 'Known operational risks'.",
                        link,
                    )
                raise
        except FileNotFoundError:
            raise YtDlpError("yt-dlp is not installed on your system")

    YtDlp.extract = staticmethod(patched_extract)
    logger.info("Patched py-tgcalls' yt-dlp subprocess timeout to %ds", YTDLP_SUBPROCESS_TIMEOUT_SECONDS)


def _cancel_pending_leave(chat_id: int) -> None:
    task = _pending_leave_tasks.pop(chat_id, None)
    # The grace timer's own callback calls stop() (below), which calls this
    # — at that point `task` IS the currently-running task, and
    # task.cancel()-ing yourself schedules a CancelledError at your own next
    # await. That await is `leave_call()` a few lines into stop(), so the
    # real Telegram "leave the voice chat" call could get interrupted
    # mid-flight every single time the bot auto-leaves from silence —
    # normal /stop or a fresh /play cancelling *someone else's* pending
    # timer is unaffected, since `task` is never the caller's own there.
    if task and not task.done() and task is not asyncio.current_task():
        task.cancel()


async def _schedule_auto_leave(chat_id: int) -> None:
    _cancel_pending_leave(chat_id)

    async def _leave_after_grace() -> None:
        await asyncio.sleep(AUTO_LEAVE_GRACE_SECONDS)
        if queues.get(chat_id).current is None:
            await stop(chat_id)

    _pending_leave_tasks[chat_id] = asyncio.create_task(_leave_after_grace())


def _video_flags(track: Track) -> MediaStream.Flags:
    # AUTO_DETECT (py-tgcalls' own default) probes for a video stream and
    # falls back to audio-only if none is found; IGNORE skips that probe
    # entirely, which is the right choice for plain /play — no reason to
    # pay the detection cost when we already know it's audio-only intent.
    return MediaStream.Flags.AUTO_DETECT if track.video else MediaStream.Flags.IGNORE


def _get_healthy_assistant(chat_id: int) -> Assistant | None:
    """Like pool.get_assigned(), but treats an unhealthy assistant (flagged
    by health_check_loop) as if none were assigned. Use this before actively
    trying to DO something with the assistant (play/pause/resume/seek) —
    routing a command through a connection already known to be dead just
    trades one silent failure for another. Cleanup paths (stop(), the
    kicked/left-group handler) deliberately use pool.get_assigned() directly
    instead, since they need to release the pool slot regardless of health."""
    assistant = pool.get_assigned(chat_id)
    if assistant is not None and not assistant.healthy:
        return None
    return assistant


async def extract_urls(link: str, video: bool) -> tuple[str, str]:
    video_id = youtube_id(link)
    if video_id is None:
        raise ValueError("Only canonical YouTube links go to the extractor.")
    canonical = f"https://www.youtube.com/watch?v={video_id}"
    key = (canonical, video)
    task = _inflight.get(key)
    if task is None:
        async def extract():
            async with asyncio.timeout(90):
                async with _extract_slots:
                    return await YtDlp.extract(canonical, _DEFAULT_VIDEO_PARAMETERS, None if video else '__audio__')
        task = asyncio.create_task(extract())
        _inflight[key] = task
        def finished(done):
            if _inflight.get(key) is done:
                _inflight.pop(key, None)
            if not done.cancelled():
                done.exception()  # retrieve failures even if all waiters stopped
        task.add_done_callback(finished)
    return await asyncio.shield(task)


def schedule_prefetch(chat_id: int) -> None:
    state = queues.get(chat_id)
    if len(state.queue) < 2:
        return
    track = state.queue[1]
    if not youtube_id(track.link):
        return
    key = (track.link, track.video)
    if key in _prefetch_cache or key in _prefetch_tasks:
        return
    async def prefetch():
        try:
            urls = await extract_urls(track.link, track.video)
            _prefetch_cache[key] = (urls, time.monotonic())
            if track.video:
                await _cached_urls_put(track.link, urls)
            while len(_prefetch_cache) > _PREFETCH_CACHE_MAX_ENTRIES:
                _prefetch_cache.pop(next(iter(_prefetch_cache)))
        except Exception:
            logger.info("Prefetch failed; foreground playback will retry")
        finally:
            _prefetch_tasks.pop(key, None)
    _prefetch_tasks[key] = asyncio.create_task(prefetch())


async def _play_track(assistant: Assistant, chat_id: int, track: Track, *, offset: int = 0) -> bool:
    session = get_session(chat_id)
    revision = session.revision
    local_path = None
    try:
        validate_track(track)
        if youtube_id(track.link):
            cached = _prefetch_cache.pop((track.link, track.video), None)
            urls = cached[0] if cached and time.monotonic() - cached[1] < 600 else None
            # Audio and video have different format requirements.
            if urls is None and track.video:
                urls = _cached_urls_get(track.link)
            elif urls is None:
                audio_entry = _audio_url_cache.get(youtube_id(track.link))
                if audio_entry and time.monotonic() - audio_entry[1] < 600:
                    urls = audio_entry[0]
            was_cached = urls is not None
            if urls is None:
                urls = await extract_urls(track.link, track.video)
        else:
            local_path = await local_media(track)
            urls = (str(local_path), str(local_path))
            was_cached = False
        for attempt in range(2):
            if revision != session.revision:
                return False
            video_url, audio_url = urls
            flags = f"-ss {offset} -t {max(1, settings.duration_limit_min * 60 - offset)}"
            if local_path:
                flags += " -protocol_whitelist file,pipe -format_whitelist mov,mp3,wav,flac,ogg,matroska,webm,aac"
            stream = MediaStream(video_url, audio_path=audio_url, video_flags=_video_flags(track), ffmpeg_parameters=flags)
            try:
                try:
                    await assistant.call_py.play(chat_id, stream)
                except ChannelInvalid:
                    async for _ in assistant.client.get_dialogs():
                        pass
                    if revision != session.revision:
                        return False
                    await assistant.call_py.play(chat_id, stream)
                break
            except Exception:
                if not was_cached or attempt:
                    raise
                video_id = youtube_id(track.link)
                _resolved_url_cache.pop(video_id, None)
                _audio_url_cache.pop(video_id, None)
                urls = await extract_urls(track.link, track.video)
        if revision != session.revision:
            return False
        old_path = _local_paths.pop(chat_id, None)
        if old_path:
            old_path.unlink(missing_ok=True)
        if local_path:
            _local_paths[chat_id] = local_path
            local_path = None
        state = queues.get(chat_id)
        state.is_paused = False
        state.needs_resume = False
        state.started_at = time.monotonic()
        state.elapsed = float(offset)
        schedule_prefetch(chat_id)
        video_id = youtube_id(track.link)
        if video_id:
            asyncio.create_task(_record_play(video_id))
            if track.video:
                asyncio.create_task(_cached_urls_put(track.link, urls))
            else:
                _audio_url_cache[video_id] = (urls, time.monotonic())
                while len(_audio_url_cache) > _RESOLVED_URL_CACHE_MAX_ENTRIES:
                    _audio_url_cache.pop(next(iter(_audio_url_cache)))
        return True
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.warning("Playback failed in chat %d", chat_id, exc_info=True)
        from bot.core.alerts import notify
        asyncio.create_task(notify(f"Playback failed in group {chat_id}. Check server logs or /doctor.", key=f"play:{chat_id}"))
        return False
    finally:
        if local_path:
            local_path.unlink(missing_ok=True)


async def _try_autoplay(chat_id: int) -> bool:
    """Called only when the queue just went empty. If /autoplay is on for
    this chat, look up a YouTube-related track to the last one played and
    start it instead of leaving. Returns True if it started something."""
    state = queues.get(chat_id)
    if not state.autoplay or state.last_track is None or get_session(chat_id).pending_requests:
        return False
    assistant = _get_healthy_assistant(chat_id)
    if assistant is None:
        return False
    related = await youtube.get_related(state.last_track.link)
    if related is None:
        return False
    # Re-check after the network round-trip above: a concurrent /stop (or
    # /play racing in with its own new track) could have released this chat
    # or replaced its queue state while we were awaiting get_related(). Don't
    # resurrect playback the user already told the bot to stop, and don't
    # stomp a track a fresh /play just started.
    if pool.get_assigned(chat_id) is not assistant or queues.get(chat_id) is not state or state.current is not None or not state.autoplay or get_session(chat_id).pending_requests:
        return False
    queues.add(chat_id, related)
    if not await _play_track(assistant, chat_id, related):
        queues.advance(chat_id, force=True)  # pop the broken pick back out
        return False
    try:
        from bot.core.player import refresh
        await refresh(chat_id)
    except Exception:
        logger.warning("Failed to refresh autoplay panel in %d", chat_id, exc_info=True)
    return True


async def _advance_and_play_unlocked(chat_id: int, *, force: bool = False) -> None:
    """Shared by the stream-end handler (force=False — natural end-of-track,
    loop_mode="one" replays) and manual /skip (force=True — always moves on,
    otherwise loop_mode="one" would make /skip a permanent no-op).

    Plays the next track; if it fails to start (broken link), tries up to
    MAX_CONSECUTIVE_PLAY_FAILURES more before giving up, so one dead link
    mid-queue doesn't wedge the chat — then autoplays a related track or
    starts the auto-leave timer."""
    next_track = queues.advance(chat_id, force=force)
    attempts = 0
    while next_track is not None:
        assistant = _get_healthy_assistant(chat_id)
        if assistant is None:
            return
        if await _play_track(assistant, chat_id, next_track):
            return
        attempts += 1
        if attempts >= MAX_CONSECUTIVE_PLAY_FAILURES:
            # Clear rather than leave the next untried track sitting as
            # "current" — nothing is actually playing at this point, and a
            # queue whose `current` track isn't really streaming would
            # confuse both /queue and the auto-leave check below (which
            # treats a non-empty queue as "still busy").
            queues.clear(chat_id)
            try:
                await bot.send_message(
                    chat_id, "⚠️ Several tracks in a row failed to play — clearing the queue. Try /play again."
                )
            except Exception:
                logger.warning("Failed to send playback-failure notice in %d", chat_id, exc_info=True)
            break
        next_track = queues.advance(chat_id, force=True)

    if force:
        assistant = _get_healthy_assistant(chat_id)
        if assistant is not None:
            try:
                await assistant.call_py.pause(chat_id)
            except Exception:
                logger.warning("Could not silence skipped track", exc_info=True)
    if await _try_autoplay(chat_id):
        return
    await _schedule_auto_leave(chat_id)


async def _advance_and_play(chat_id: int, *, force: bool = False) -> None:
    async with operation(chat_id):
        await _advance_and_play_unlocked(chat_id, force=force)
        await persist_queue(chat_id)


async def persist_queue(chat_id: int) -> None:
    try:
        await db.save_queue(chat_id, queues.export(chat_id))
    except Exception:
        logger.warning("Queue snapshot failed for %d", chat_id, exc_info=True)


async def join_and_play(chat_id: int, track: Track) -> tuple[Assistant | None, str | None]:
    """Add-and-play entry point used by /play (and /import when idle).
    Returns (assistant, None) if playback actually started. On failure,
    (None, "busy") if every assistant was at capacity or (None, "failed")
    if the track itself didn't play — distinct so callers can tell the user
    which one actually happened instead of always hedging both (pool slot
    is released either way, so a retry can get a fresh assignment)."""
    assistant = await pool.get_or_assign(chat_id)
    if assistant is None:
        return None, "busy"
    _cancel_pending_leave(chat_id)
    if not await _play_track(assistant, chat_id, track):
        pool.release(chat_id)
        return None, "failed"
    return assistant, None


async def force_play(chat_id: int, track: Track) -> tuple[Assistant | None, str | None]:
    """Entry point for /playforce and /vplayforce — immediately hot-swaps
    the chat's stream to `track`, same as join_and_play() if the chat was
    idle, but if it already has an assistant this reuses it directly
    (re-calling .play() on a live call swaps the stream in place rather
    than leaving/rejoining, same mechanism seek() relies on) instead of
    going through pool assignment again. Caller is responsible for having
    already put `track` at queue index 0 (queues.force_add). Same
    (assistant, reason) contract as join_and_play()."""
    assistant = _get_healthy_assistant(chat_id)
    if assistant is None:
        return await join_and_play(chat_id, track)
    _cancel_pending_leave(chat_id)
    if not await _play_track(assistant, chat_id, track):
        return None, "failed"
    return assistant, None


async def pause(chat_id: int) -> bool:
    assistant = _get_healthy_assistant(chat_id)
    if assistant is None:
        return False
    try:
        await assistant.call_py.pause(chat_id)
    except Exception:
        logger.warning("pause failed for chat %d", chat_id, exc_info=True)
        return False
    state = queues.get(chat_id)
    if state.started_at is not None and not state.is_paused:
        state.elapsed += time.monotonic() - state.started_at
    state.started_at = None
    state.is_paused = True
    return True


async def resume(chat_id: int) -> bool:
    assistant = _get_healthy_assistant(chat_id)
    if assistant is None:
        return False
    try:
        await assistant.call_py.resume(chat_id)
    except Exception:
        logger.warning("resume failed for chat %d", chat_id, exc_info=True)
        return False
    state = queues.get(chat_id)
    state.is_paused = False
    state.started_at = time.monotonic()
    return True


async def skip(chat_id: int) -> Track | None:
    """Manually advance to the next track (always moves on, even under
    loop_mode="one"). Returns the new current track, or None if the queue is
    now empty (auto-leave timer has been started)."""
    await _advance_and_play(chat_id, force=True)
    return queues.get(chat_id).current


async def stop(chat_id: int) -> bool:
    session = await cancel_pending(chat_id)
    async with session.lock:
        assistant = pool.get_assigned(chat_id)
        _cancel_pending_leave(chat_id)
        for track in queues.get(chat_id).queue:
            task = _prefetch_tasks.pop((track.link, track.video), None)
            if task:
                task.cancel()
            _prefetch_cache.pop((track.link, track.video), None)
        queues.clear(chat_id)
        pool.release(chat_id)
        try:
            if assistant is not None:
                await assistant.call_py.leave_call(chat_id)
        except Exception:
            logger.warning("leave_call failed for chat %d", chat_id, exc_info=True)
        finally:
            path = _local_paths.pop(chat_id, None)
            if path:
                path.unlink(missing_ok=True)
        await persist_queue(chat_id)
        return assistant is not None


# How many consecutive stale_assignment_check_loop() cycles a chat must miss
# before its assistant slot is force-released. Not 1 — a chat that just
# called get_or_assign() legitimately has no active call yet for the first
# few seconds while _play_track() is still resolving/joining, and the
# android-then-web extraction fallback can legitimately take up to ~120s in
# the worst case (two 60s yt-dlp timeouts back to back). 3 cycles at the
# default 60s interval is comfortably above that, so this only ever fires
# for a chat_id that's genuinely been abandoned, not one still starting up.
_STALE_ASSIGNMENT_THRESHOLD = 3
_stale_sightings: dict[int, int] = {}


async def stale_assignment_check_loop(interval_seconds: int = 60) -> None:
    """Periodically cross-checks the pool's own bookkeeping (which chats
    each assistant thinks it's assigned to) against pytgcalls' own view of
    which calls are actually still active (assistant.call_py.calls) —
    catches an assistant staying permanently "assigned" to a chat whose
    voice chat ended some way other than /stop or the queue naturally
    emptying (e.g. a group admin ends the Telegram voice chat directly,
    with no bot command involved) — nothing else in this codebase notices
    that, so the slot would otherwise sit unusable by any other chat until
    someone remembers to run /stop there. loop_mode/queue state can't
    substitute for this check: both stay however they were the instant the
    real call died, so they look identical to a genuinely-still-playing
    chat from inside the bot."""
    while True:
        await asyncio.sleep(interval_seconds)
        for assistant in pool.assistants:
            if not assistant.healthy:
                continue
            try:
                active_calls = await assistant.call_py.calls
            except Exception:
                logger.warning("Failed to list active calls for assistant %d", assistant.index, exc_info=True)
                continue
            for chat_id in list(assistant.chats):
                if chat_id in active_calls:
                    _stale_sightings.pop(chat_id, None)
                    continue
                count = _stale_sightings.get(chat_id, 0) + 1
                if count < _STALE_ASSIGNMENT_THRESHOLD:
                    _stale_sightings[chat_id] = count
                    continue
                _stale_sightings.pop(chat_id, None)
                logger.warning(
                    "Chat %d held assistant %d's slot with no active call for %d consecutive checks — releasing it",
                    chat_id, assistant.index, count,
                )
                await stop(chat_id)


async def seek(chat_id: int, seconds: int) -> bool:
    assistant = _get_healthy_assistant(chat_id)
    track = queues.get(chat_id).current
    if assistant is None or track is None or seconds < 0 or seconds >= settings.duration_limit_min * 60:
        return False
    from bot.utils.formatting import parse_duration_to_seconds
    duration = parse_duration_to_seconds(track.duration)
    if duration and seconds >= duration:
        return False
    return await _play_track(assistant, chat_id, track, offset=seconds)


def _register(call_py) -> None:
    @call_py.on_update(fl.stream_end())
    async def _stream_end(_, update: StreamEnded) -> None:
        if update.stream_type != StreamEnded.Type.AUDIO:
            return
        assistant = pool.get_assigned(update.chat_id)
        if assistant is None or assistant.call_py is not call_py:
            return
        state = queues.get(update.chat_id)
        expected = state.current
        async with operation(update.chat_id):
            if queues.get(update.chat_id).current is not expected or expected is None:
                return
            await _advance_and_play_unlocked(update.chat_id)
            await persist_queue(update.chat_id)
            from bot.core.player import refresh
            await refresh(update.chat_id)

    @call_py.on_update(
        fl.chat_update(ChatUpdate.Status.KICKED | ChatUpdate.Status.LEFT_GROUP)
    )
    async def _left(_, update: ChatUpdate) -> None:
        logger.info("Removed from voice chat in %d", update.chat_id)
        assistant = pool.get_assigned(update.chat_id)
        if assistant is not None and assistant.call_py is call_py:
            await stop(update.chat_id)


def register_all_handlers() -> None:
    """Call once after pool.start() — wires stream-end/kicked handlers onto
    every assistant's PyTgCalls instance."""
    for assistant in pool.assistants:
        _register(assistant.call_py)
