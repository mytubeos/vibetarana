"""Application boundary: permissions-independent queue transactions and recovery."""
import asyncio
from dataclasses import asdict

from bot.core import calls, db, preferences
from bot.core.queue import Track, queues, _track_from_dict
from bot.core.media import validate_track
from bot.core.session import operation, get_session
from config import settings


async def enqueue(chat_id: int, tracks: list[Track], *, force=False, next_up=False, revision=None):
    async with operation(chat_id):
        if revision is not None and revision != get_session(chat_id).revision:
            raise asyncio.CancelledError
        pref = await preferences.get(chat_id)
        state = queues.get(chat_id)
        if not tracks:
            raise ValueError('No tracks found.')
        if len(state.queue) + len(tracks) - int(force and bool(state.queue)) > settings.max_queue_tracks:
            raise ValueError(f'Queue limit: {settings.max_queue_tracks} tracks.')
        for track in tracks:
            validate_track(track)
        old = list(state.queue)
        was_idle = state.current is None or state.needs_resume or calls._get_healthy_assistant(chat_id) is None
        if force:
            state.queue = [tracks[0], *state.queue[1:]] if state.queue else [tracks[0]]
        elif next_up and state.queue:
            state.queue[1:1] = tracks
        else:
            state.queue.extend(tracks)
            if pref.get('fair_queue'):
                queues.fair(chat_id)
        if was_idle or force:
            starter = state.current
            assistant, reason = await (calls.force_play(chat_id, starter) if force else calls.join_and_play(chat_id, starter))
            if assistant is None:
                state.queue = old
                state.needs_resume = bool(old)
                state.is_paused = bool(old)
                assigned = calls.pool.get_assigned(chat_id)
                recovered = False
                if force and old and assigned is not None:
                    recovered = await calls._play_track(assigned, chat_id, old[0])
                if not recovered and assigned is not None:
                    try:
                        await assigned.call_py.leave_call(chat_id)
                    except Exception:
                        calls.logger.warning('Failed to leave after replacement failure', exc_info=True)
                    calls.pool.release(chat_id)
                if recovered:
                    state.needs_resume = False
                    state.is_paused = False
                await calls.persist_queue(chat_id)
                raise ValueError('All assistants are busy.' if reason == 'busy' else ('Track failed; previous playback restored.' if recovered else 'Track failed; the previous queue has been kept. Use /resume to recover.'))
        calls.schedule_prefetch(chat_id)
        await calls.persist_queue(chat_id)
        return len(state.queue), was_idle or force


async def resume(chat_id: int, *, expected_playback_id=None) -> bool:
    async with operation(chat_id):
        state = queues.get(chat_id)
        if expected_playback_id is not None and state.playback_id != expected_playback_id:
            raise ValueError("Track changed. Refresh the player.")
        if state.current is None:
            return False
        if state.needs_resume or calls.pool.get_assigned(chat_id) is None:
            assistant, _ = await calls.join_and_play(chat_id, state.current)
            if assistant is None:
                return False
        else:
            if not await calls.resume(chat_id):
                return False
        await calls.persist_queue(chat_id)
        return True


async def restore() -> None:
    for snapshot in await db.get_queues():
        chat_id = snapshot['_id']
        try:
            tracks = [_track_from_dict(t) for t in snapshot.get('tracks', [])[:settings.max_queue_tracks]]
            tracks = [t for t in tracks if _restorable(t)]
            await preferences.get(chat_id)
            state = queues.get(chat_id)
            state.queue = tracks
            state.needs_resume = bool(tracks)
            state.is_paused = bool(tracks)
        except (ValueError, TypeError, KeyError):
            calls.logger.warning('Ignored malformed snapshot for %d', chat_id)


def _restorable(track):
    try:
        validate_track(track)
        return True
    except ValueError:
        return False


def deserialize(payload, user=None) -> list[Track]:
    if not isinstance(payload, list) or len(payload) > settings.max_queue_tracks:
        raise ValueError('Invalid or oversized track list.')
    tracks = [_track_from_dict(item) for item in payload]
    for track in tracks:
        validate_track(track)
        if user is not None:
            track.requested_by = user.id
            track.requested_by_name = user.first_name or 'listener'
    return tracks
