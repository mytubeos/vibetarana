"""Join/play/pause/resume/stop/seek wiring on top of the assistant pool, plus
the PyTgCalls update handlers that auto-advance the queue and auto-leave an
empty voice chat.
"""
from __future__ import annotations

import asyncio
from pathlib import Path

from pytgcalls import filters as fl
from pytgcalls.types import ChatUpdate, MediaStream, StreamEnded

from bot.core.assistants import Assistant, pool
from bot.core.client import bot
from bot.core.queue import Track, queues
from bot.platforms import youtube
from bot.utils.logger import get_logger

logger = get_logger(__name__)

AUTO_LEAVE_GRACE_SECONDS = 75
MAX_CONSECUTIVE_PLAY_FAILURES = 3

# Checked in order: the VPS/systemd convention (relative to the working
# directory) first, then Render's Secret Files location — Render's filename
# field rejects slashes, so a Secret File named "cookies.txt" always lands at
# /etc/secrets/cookies.txt regardless of service type.
COOKIES_CANDIDATES = [Path("cookies/cookies.txt"), Path("/etc/secrets/cookies.txt")]

# yt-dlp auto-loads this from its working directory (verified via `yt-dlp
# --verbose`, which logs `Home config "yt-dlp.conf": [...]` for exactly this
# file) with no extra flag needed. Deliberately NOT passed as
# MediaStream(ytdlp_parameters=...): py-tgcalls' cleanup_commands() drops any
# --long-form flag that has no single-dash alias in `yt-dlp -h full` output
# before it ever reaches the yt-dlp subprocess, and --cookies has no such
# alias — confirmed empirically, that flag was silently discarded every time.
YTDLP_CONFIG_PATH = Path("yt-dlp.conf")

_pending_leave_tasks: dict[int, asyncio.Task] = {}


def setup_cookies() -> None:
    """Call once at startup. Writes yt-dlp.conf pointing at cookies.txt if one
    is found — YouTube throttles/blocks datacenter IPs (VPS/PaaS hosts)
    without one (see README's "Known operational risks"). No-op if no
    cookies file is found, so deploys that don't need it are unaffected."""
    for path in COOKIES_CANDIDATES:
        if path.exists():
            YTDLP_CONFIG_PATH.write_text(f"--cookies {path}\n")
            logger.info("yt-dlp cookies config written, using %s", path)
            return


def _cancel_pending_leave(chat_id: int) -> None:
    task = _pending_leave_tasks.pop(chat_id, None)
    if task and not task.done():
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


async def _play_track(assistant: Assistant, chat_id: int, track: Track) -> bool:
    """Returns True if playback started. False on any yt-dlp/ffmpeg failure
    (deleted video, region-blocked, unsupported format, network issue) —
    never raises, so callers can react (try the next track, report an
    error) instead of leaving the chat silently wedged."""
    try:
        await assistant.call_py.play(
            chat_id,
            MediaStream(track.link, video_flags=_video_flags(track)),
        )
    except Exception:
        logger.warning("Failed to start playback for %r in chat %d", track.title, chat_id, exc_info=True)
        return False
    queues.get(chat_id).is_paused = False
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
        await bot.send_message(chat_id, f"🔁 Autoplay: **{related.title}** ({related.duration})")
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
