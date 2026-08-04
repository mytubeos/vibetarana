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

from pytgcalls import filters as fl
from pytgcalls.exceptions import YtDlpError
from pytgcalls.ffmpeg import cleanup_commands
from pytgcalls.types import ChatUpdate, MediaStream, StreamEnded
from pytgcalls.types.raw.video_parameters import VideoParameters
from pytgcalls.types.stream.video_quality import VideoQuality
from pytgcalls.ytdlp import YtDlp

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
_prefetch_cache: dict[int, tuple[str, str]] = {}
_prefetch_tasks: dict[int, asyncio.Task] = {}
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
_RESOLVED_URL_CACHE_MAX_ENTRIES = 200
_resolved_url_cache: dict[str, tuple[str, str, float]] = {}


def _cached_urls_get(link: str) -> tuple[str, str] | None:
    match = _VIDEO_ID_RE.search(link)
    if match is None:
        return None
    entry = _resolved_url_cache.get(match.group(1))
    if entry is None:
        return None
    video_url, audio_url, resolved_at = entry
    if time.monotonic() - resolved_at > _RESOLVED_URL_CACHE_TTL_SECONDS:
        del _resolved_url_cache[match.group(1)]
        return None
    return video_url, audio_url


def _cached_urls_put(link: str, urls: tuple[str, str]) -> None:
    match = _VIDEO_ID_RE.search(link)
    if match is None:
        # Non-YouTube link (e.g. bot/platforms/direct_link.py) — no stable
        # ID to key a cross-chat cache on, so this is a silent no-op rather
        # than a cache that can never be a hit.
        return
    _resolved_url_cache[match.group(1)] = (*urls, time.monotonic())
    while len(_resolved_url_cache) > _RESOLVED_URL_CACHE_MAX_ENTRIES:
        _resolved_url_cache.pop(next(iter(_resolved_url_cache)))


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
        'bestvideo[vcodec~="(vp09|avc1)"]+m4a/best',
        "-S",
        f"res:{min(video_parameters.width, video_parameters.height)}",
        "--extractor-args",
        f"youtube:player_client={player_client}",
        "--no-warnings",
    ]
    if add_commands:
        commands += await cleanup_commands(shlex.split(add_commands), "yt-dlp", ["-f", "-g", "--no-warnings"])
    commands.append(link)
    proc = await asyncio.create_subprocess_exec(
        *commands, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), YTDLP_SUBPROCESS_TIMEOUT_SECONDS)
    except asyncio.TimeoutError:
        proc.terminate()
        raise YtDlpError("yt-dlp process timeout")
    if stderr:
        raise YtDlpError(stderr.decode())
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
    of py-tgcalls' hardcoded 20, and tries the "tv_embedded"+"android" player
    clients before falling back to "web"."""

    async def patched_extract(
        link: Optional[str],
        video_parameters: VideoParameters,
        add_commands: Optional[str],
    ) -> Tuple[Optional[str], Optional[str]]:
        if link is None:
            return None, None
        try:
            try:
                # The "web" client (yt-dlp's default) is what needs the slow
                # Deno JS-challenge in the first place; "tv_embedded" and
                # "android" both skip it. Measured live (2026-08-04): under
                # YouTube's current SABR-streaming restrictions (see the
                # cookie-check comment below's neighbor — a separate, active
                # YouTube-side rollout), "android" alone only had access to
                # the old 360p-capped itag 18 fallback format for every video
                # tested; "tv_embedded" got 720p on the same videos, for the
                # same ~5s cost. Listing both together here cost only ~6s
                # (not double — unlike the android+web combo, neither of
                # these needs the JS challenge, so querying both is just two
                # cheap API calls), so this tries them together first and
                # only pays for a separate, slower web attempt on the
                # (uncommon) videos neither can resolve at all — same
                # reliability as before, better common-case quality, no
                # meaningful speed cost (worth re-checking if extraction
                # quality/time regresses after a yt-dlp upgrade — YouTube's
                # SABR rollout and yt-dlp's support for it are both moving
                # targets right now).
                return await _run_ytdlp_extract(link, video_parameters, add_commands, "tv_embedded,android")
            except YtDlpError as first_attempt_error:
                logger.info("tv_embedded+android extraction failed for %s, retrying with web", link)
                try:
                    return await _run_ytdlp_extract(link, video_parameters, add_commands, "web")
                except YtDlpError as web_error:
                    if _COOKIE_AUTH_ERROR_MARKER in str(first_attempt_error) or _COOKIE_AUTH_ERROR_MARKER in str(web_error):
                        logger.warning(
                            "COOKIE AUTH FAILURE: YouTube rejected every player client for %s with "
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


def schedule_prefetch(chat_id: int) -> None:
    """Kick off a background resolve of the queue's next track (index 1 —
    index 0 is whatever's currently playing), if one exists and isn't
    already cached/in flight. Call after anything that could change what
    "next" is: a track starts playing, a new one is added to a queue that's
    already playing, etc. Safe to call speculatively — cheap no-op if
    there's nothing to prefetch or it's already covered."""
    state = queues.get(chat_id)
    if len(state.queue) < 2:
        return
    next_track = state.queue[1]
    key = id(next_track)
    if key in _prefetch_cache or key in _prefetch_tasks:
        return

    async def _prefetch() -> None:
        try:
            urls = await YtDlp.extract(next_track.link, _DEFAULT_VIDEO_PARAMETERS, None)
        except Exception:
            logger.warning("Prefetch failed for %r", next_track.title, exc_info=True)
        else:
            _prefetch_cache[key] = urls
            _cached_urls_put(next_track.link, urls)
            # A prefetched track that's cleared/shuffled/stopped before its
            # turn leaves an orphaned entry here forever (nothing else
            # references it to know it's safe to drop) — bound it rather
            # than track every such path, so months of unattended uptime
            # can't accumulate unbounded entries.
            while len(_prefetch_cache) > _PREFETCH_CACHE_MAX_ENTRIES:
                _prefetch_cache.pop(next(iter(_prefetch_cache)))
        finally:
            _prefetch_tasks.pop(key, None)

    _prefetch_tasks[key] = asyncio.create_task(_prefetch())


async def _play_track(assistant: Assistant, chat_id: int, track: Track) -> bool:
    """Returns True if playback started. False on any yt-dlp/ffmpeg failure
    (deleted video, region-blocked, unsupported format, network issue) —
    never raises, so callers can react (try the next track, report an
    error) instead of leaving the chat silently wedged."""
    # Three tiers, cheapest first: this chat's own prefetch (already
    # resolved in the background for exactly this track), the bot-wide
    # cross-chat cache (someone resolved this same video recently), and only
    # then a fresh, explicit extraction — which also populates the
    # bot-wide cache so the *next* request for this song, in any chat,
    # can hit it. Resolving explicitly here (rather than handing
    # MediaStream(track.link, ...) to py-tgcalls and letting it call
    # YtDlp.extract internally, as before) is what makes that caching
    # possible — same underlying (patched) extractor either way.
    urls = _prefetch_cache.pop(id(track), None) or _cached_urls_get(track.link)
    try:
        if urls is None:
            urls = await YtDlp.extract(track.link, _DEFAULT_VIDEO_PARAMETERS, None)
            _cached_urls_put(track.link, urls)
        # Already googlevideo.com URLs, not youtube.com ones — pytgcalls' own
        # is_valid() check (see ytdlp.py) fails on them, so check_stream()
        # skips straight past re-extracting and uses them as-is.
        video_url, audio_url = urls
        stream = MediaStream(video_url, audio_path=audio_url, video_flags=_video_flags(track))
        await assistant.call_py.play(chat_id, stream)
    except Exception:
        logger.warning("Failed to start playback for %r in chat %d", track.title, chat_id, exc_info=True)
        return False
    queues.get(chat_id).is_paused = False
    schedule_prefetch(chat_id)
    return True


async def _try_autoplay(chat_id: int) -> bool:
    """Called only when the queue just went empty. If /autoplay is on for
    this chat, look up a YouTube-related track to the last one played and
    start it instead of leaving. Returns True if it started something."""
    state = queues.get(chat_id)
    if not state.autoplay or state.last_track is None:
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
    if pool.get_assigned(chat_id) is not assistant or queues.get(chat_id) is not state:
        return False
    queues.add(chat_id, related)
    if not await _play_track(assistant, chat_id, related):
        queues.advance(chat_id, force=True)  # pop the broken pick back out
        return False
    try:
        await send_track_card(
            bot, chat_id, related, heading="🔁 AUTOPLAY", footer="▶️ Playing", keyboard=playback_keyboard(paused=False)
        )
    except Exception:
        logger.warning("Failed to send autoplay announcement in %d", chat_id, exc_info=True)
    return True


async def _advance_and_play(chat_id: int, *, force: bool = False) -> None:
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

    if await _try_autoplay(chat_id):
        return
    await _schedule_auto_leave(chat_id)


async def join_and_play(chat_id: int, track: Track) -> Assistant | None:
    """Add-and-play entry point used by /play (and /import when idle).
    Returns the assistant if playback actually started, or None if every
    assistant was at capacity OR the track failed to play (pool slot is
    released either way, so a retry can get a fresh assignment)."""
    assistant = await pool.get_or_assign(chat_id)
    if assistant is None:
        return None
    _cancel_pending_leave(chat_id)
    if not await _play_track(assistant, chat_id, track):
        pool.release(chat_id)
        return None
    return assistant


async def force_play(chat_id: int, track: Track) -> Assistant | None:
    """Entry point for /playforce and /vplayforce — immediately hot-swaps
    the chat's stream to `track`, same as join_and_play() if the chat was
    idle, but if it already has an assistant this reuses it directly
    (re-calling .play() on a live call swaps the stream in place rather
    than leaving/rejoining, same mechanism seek() relies on) instead of
    going through pool assignment again. Caller is responsible for having
    already put `track` at queue index 0 (queues.force_add)."""
    assistant = _get_healthy_assistant(chat_id)
    if assistant is None:
        return await join_and_play(chat_id, track)
    _cancel_pending_leave(chat_id)
    if not await _play_track(assistant, chat_id, track):
        return None
    return assistant


async def pause(chat_id: int) -> bool:
    assistant = _get_healthy_assistant(chat_id)
    if assistant is None:
        return False
    try:
        await assistant.call_py.pause(chat_id)
    except Exception:
        logger.warning("pause failed for chat %d", chat_id, exc_info=True)
        return False
    queues.get(chat_id).is_paused = True
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
    queues.get(chat_id).is_paused = False
    return True


async def skip(chat_id: int) -> Track | None:
    """Manually advance to the next track (always moves on, even under
    loop_mode="one"). Returns the new current track, or None if the queue is
    now empty (auto-leave timer has been started)."""
    await _advance_and_play(chat_id, force=True)
    return queues.get(chat_id).current


async def stop(chat_id: int) -> bool:
    """Leave the voice chat, free the assistant, and clear the queue."""
    assistant = pool.get_assigned(chat_id)
    _cancel_pending_leave(chat_id)
    queues.clear(chat_id)
    if assistant is None:
        return False
    pool.release(chat_id)
    try:
        await assistant.call_py.leave_call(chat_id)
    except Exception:
        logger.warning("leave_call failed for chat %d", chat_id, exc_info=True)
    return True


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
    """Restart the current track at an absolute offset. Not frame-accurate
    scrubbing of an already-buffered stream (py-tgcalls exposes no such API)
    — restarts decode at a fast input-side ffmpeg seek (`-ss`, placed before
    `-i`), and re-calling .play() on a chat already in-call hot-swaps the
    stream in place rather than leaving/rejoining."""
    assistant = _get_healthy_assistant(chat_id)
    track = queues.get(chat_id).current
    if assistant is None or track is None:
        return False
    try:
        await assistant.call_py.play(
            chat_id,
            MediaStream(track.link, ffmpeg_parameters=f"-ss {seconds}", video_flags=_video_flags(track)),
        )
    except Exception:
        logger.warning("seek failed for chat %d", chat_id, exc_info=True)
        return False
    queues.get(chat_id).is_paused = False
    return True


def _register(call_py) -> None:
    @call_py.on_update(fl.stream_end())
    async def _stream_end(_, update: StreamEnded) -> None:
        logger.info("Stream ended in %d", update.chat_id)
        await _advance_and_play(update.chat_id)

    @call_py.on_update(
        fl.chat_update(ChatUpdate.Status.KICKED | ChatUpdate.Status.LEFT_GROUP)
    )
    async def _left(_, update: ChatUpdate) -> None:
        logger.info("Removed from voice chat in %d", update.chat_id)
        _cancel_pending_leave(update.chat_id)
        queues.clear(update.chat_id)
        pool.release(update.chat_id)


def register_all_handlers() -> None:
    """Call once after pool.start() — wires stream-end/kicked handlers onto
    every assistant's PyTgCalls instance."""
    for assistant in pool.assistants:
        _register(assistant.call_py)
