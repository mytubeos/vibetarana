"""Join/play/pause/resume/stop/seek wiring on top of the assistant pool, plus
the PyTgCalls update handlers that auto-advance the queue and auto-leave an
empty voice chat.
"""
from __future__ import annotations

import asyncio

from pytgcalls import filters as fl
from pytgcalls.types import ChatUpdate, MediaStream, StreamEnded

from bot.core.assistants import Assistant, pool
from bot.core.queue import Track, queues
from bot.utils.logger import get_logger

logger = get_logger(__name__)

AUTO_LEAVE_GRACE_SECONDS = 75

_pending_leave_tasks: dict[int, asyncio.Task] = {}


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


async def _play_track(assistant: Assistant, chat_id: int, track: Track) -> None:
    await assistant.call_py.play(
        chat_id,
        MediaStream(track.link, video_flags=_video_flags(track)),
    )
    queues.get(chat_id).is_paused = False


async def _advance_and_play(chat_id: int, *, force: bool = False) -> None:
    """Shared by the stream-end handler (force=False — natural end-of-track,
    loop_mode="one" replays) and manual /skip (force=True — always moves on,
    otherwise loop_mode="one" would make /skip a permanent no-op), then
    either plays the next one or starts the auto-leave timer."""
    next_track = queues.advance(chat_id, force=force)
    if next_track is None:
        await _schedule_auto_leave(chat_id)
        return
    assistant = pool.get_assigned(chat_id)
    if assistant is None:
        return
    await _play_track(assistant, chat_id, next_track)


async def join_and_play(chat_id: int, track: Track) -> Assistant | None:
    """Add-and-play entry point used by /play (and /import when idle).
    Returns the assistant that took the chat, or None if every assistant is
    at capacity."""
    assistant = await pool.get_or_assign(chat_id)
    if assistant is None:
        return None
    _cancel_pending_leave(chat_id)
    await _play_track(assistant, chat_id, track)
    return assistant


async def pause(chat_id: int) -> bool:
    assistant = pool.get_assigned(chat_id)
    if assistant is None:
        return False
    await assistant.call_py.pause(chat_id)
    queues.get(chat_id).is_paused = True
    return True


async def resume(chat_id: int) -> bool:
    assistant = pool.get_assigned(chat_id)
    if assistant is None:
        return False
    await assistant.call_py.resume(chat_id)
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
    assistant = pool.get_assigned(chat_id)
    track = queues.get(chat_id).current
    if assistant is None or track is None:
        return False
    await assistant.call_py.play(
        chat_id,
        MediaStream(track.link, ffmpeg_parameters=f"-ss {seconds}", video_flags=_video_flags(track)),
    )
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
