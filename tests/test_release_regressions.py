"""Release regressions: real control flow with external IO mocked."""
import asyncio
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, Mock

import pytest
from bot.core import calls, preferences, service
from bot.core.queue import QueueManager, Track
from bot.core import session
from bot.core.media import youtube_id, validate_url, validate_track, PublicResolver
from bot.utils.formatting import edit_track_card, reply_track_card
from bot.plugins.controls import seek_cmd


def track(name='track'):
    return Track(name, '3:00', 'https://www.youtube.com/watch?v=abcdefghijk', None, 7, 'listener')


@pytest.fixture(autouse=True)
def isolated(monkeypatch):
    q = QueueManager()
    monkeypatch.setattr(calls, 'queues', q)
    monkeypatch.setattr(service, 'queues', q)
    monkeypatch.setattr(preferences, 'queues', q)
    monkeypatch.setattr(preferences, '_cache', {})
    monkeypatch.setattr(preferences, '_locks', {})
    monkeypatch.setattr(session, 'sessions', {})
    monkeypatch.setattr(calls, '_audio_url_cache', {})
    monkeypatch.setattr(calls, '_prefetch_cache', {})
    monkeypatch.setattr(calls, '_prefetch_tasks', {})
    monkeypatch.setattr(calls, '_pending_leave_tasks', {})
    monkeypatch.setattr(calls, '_local_paths', {})
    monkeypatch.setattr(calls, 'persist_queue', AsyncMock())
    monkeypatch.setattr(calls, '_record_play', AsyncMock())
    monkeypatch.setattr(calls, '_cached_urls_put', AsyncMock())
    monkeypatch.setattr(calls, 'schedule_prefetch', Mock())
    return q


def test_stop_cancels_loading_before_play(monkeypatch, isolated):
    async def scenario():
        started, released = asyncio.Event(), asyncio.Event()
        a = NS(healthy=True, call_py=NS(play=AsyncMock(), leave_call=AsyncMock()))
        assignments = {1: a}
        monkeypatch.setattr(calls.pool, 'get_assigned', lambda c: assignments.get(c))
        monkeypatch.setattr(calls.pool, 'release', lambda c: assignments.pop(c, None))
        async def extraction(*args):
            started.set()
            await released.wait()
            return ('https://example.com/video', 'https://example.com/audio')
        monkeypatch.setattr(calls, 'extract_urls', extraction)
        async def loading():
            async with session.operation(1):
                await calls._play_track(a, 1, track())
        isolated.add(1, track())
        task = asyncio.create_task(loading())
        await started.wait()
        await calls.stop(1)
        released.set()
        assert task.cancelled()
        a.call_py.play.assert_not_awaited()
        a.call_py.leave_call.assert_awaited_once()
        assert isolated.get(1).current is None
        assert not assignments
    asyncio.run(scenario())


def test_waiting_operation_invalidated_by_stop():
    async def scenario():
        s = session.get_session(1)
        await s.lock.acquire()
        entered = []
        async def mutate():
            async with session.operation(1):
                entered.append(True)
        pending = asyncio.create_task(mutate())
        await asyncio.sleep(0)
        await session.cancel_pending(1)
        s.lock.release()
        with pytest.raises(asyncio.CancelledError):
            await pending
        assert not entered
    asyncio.run(scenario())


def test_stop_revision_rejects_search_result(monkeypatch):
    async def scenario():
        revision = session.get_session(1).revision
        await session.cancel_pending(1)
        with pytest.raises(asyncio.CancelledError):
            await service.enqueue(1, [track()], revision=revision)
    asyncio.run(scenario())


def test_autoplay_never_overwrites_manual_queue(monkeypatch, isolated):
    state = isolated.get(1)
    state.autoplay = True
    state.last_track = track('old')
    a = NS(healthy=True)
    monkeypatch.setattr(calls, '_get_healthy_assistant', lambda c: a)
    monkeypatch.setattr(calls.pool, 'get_assigned', lambda c: a)
    async def related(*args):
        isolated.add(1, track('manual'))
        return track('auto')
    monkeypatch.setattr(calls.youtube, 'get_related', related)
    play = AsyncMock()
    monkeypatch.setattr(calls, '_play_track', play)
    assert asyncio.run(calls._try_autoplay(1)) is False
    play.assert_not_awaited()
    assert state.current.title == 'manual'


def test_autoplay_waits_for_pending_request(monkeypatch, isolated):
    state = isolated.get(1)
    state.autoplay = True
    state.last_track = track()
    session.get_session(1).pending_requests = 1
    related = AsyncMock()
    monkeypatch.setattr(calls.youtube, 'get_related', related)
    assert asyncio.run(calls._try_autoplay(1)) is False
    related.assert_not_awaited()


def test_force_failure_preserves_queue_and_releases_failed_assistant(monkeypatch, isolated):
    old, upcoming = track('old'), track('next')
    isolated.add(1, old); isolated.add(1, upcoming)
    a = NS(healthy=True, call_py=NS(leave_call=AsyncMock()))
    monkeypatch.setattr(calls.pool, 'get_assigned', lambda c: a)
    release = Mock(); monkeypatch.setattr(calls.pool, 'release', release)
    monkeypatch.setattr(calls, 'force_play', AsyncMock(return_value=(None, 'failed')))
    monkeypatch.setattr(calls, '_play_track', AsyncMock(return_value=False))
    with pytest.raises(ValueError, match='previous queue'):
        asyncio.run(service.enqueue(1, [track('broken')], force=True))
    assert isolated.get(1).queue == [old, upcoming]
    assert isolated.get(1).needs_resume
    a.call_py.leave_call.assert_awaited_once()
    release.assert_called_once_with(1)


def test_force_failure_recovers_previous_stream(monkeypatch, isolated):
    isolated.add(1, track('old'))
    a = NS(healthy=True, call_py=NS(leave_call=AsyncMock()))
    monkeypatch.setattr(calls.pool, 'get_assigned', lambda c: a)
    monkeypatch.setattr(calls, 'force_play', AsyncMock(return_value=(None, 'failed')))
    recovery = AsyncMock(return_value=True); monkeypatch.setattr(calls, '_play_track', recovery)
    with pytest.raises(ValueError, match='restored'):
        asyncio.run(service.enqueue(1, [track('broken')], force=True))
    assert isolated.get(1).current.title == 'old'
    assert not isolated.get(1).needs_resume
    a.call_py.leave_call.assert_not_awaited()


def test_skip_last_silences_current_stream(monkeypatch, isolated):
    isolated.add(1, track())
    a = NS(call_py=NS(pause=AsyncMock()))
    monkeypatch.setattr(calls, '_get_healthy_assistant', lambda c: a)
    monkeypatch.setattr(calls, '_try_autoplay', AsyncMock(return_value=False))
    monkeypatch.setattr(calls, '_schedule_auto_leave', AsyncMock())
    assert asyncio.run(calls.skip(1)) is None
    a.call_py.pause.assert_awaited_once_with(1)


def test_preferences_survive_clear_and_reload(monkeypatch, isolated):
    monkeypatch.setattr(preferences.db, 'get_chat_settings', AsyncMock(return_value={'autoplay': True, 'loop_mode': 'all'}))
    async def scenario():
        await preferences.get(1)
        isolated.clear(1)
        assert isolated.get(1).autoplay
        assert isolated.get(1).loop_mode == 'all'
        preferences._cache.clear()
        await preferences.get(1)
        assert isolated.get(1).autoplay
    asyncio.run(scenario())


def test_restore_does_not_start_calls(monkeypatch, isolated):
    from dataclasses import asdict
    monkeypatch.setattr(service.db, 'get_queues', AsyncMock(return_value=[{'_id': 1, 'tracks': [asdict(track())]}]))
    play = AsyncMock(); monkeypatch.setattr(calls, 'join_and_play', play)
    asyncio.run(service.restore())
    assert isolated.get(1).needs_resume
    assert isolated.get(1).is_paused
    play.assert_not_awaited()


@pytest.mark.parametrize('url', ['http://127.0.0.1/a', 'http://[::1]/a', 'http://10.0.0.1/a', 'http://169.254.169.254/a', 'http://localhost/a', 'https://x.local/a', 'file:///etc/passwd', 'http://u:p@example.com/a', 'http://example.com:8080/a'])
def test_unsafe_urls_rejected(url):
    with pytest.raises(ValueError): validate_url(url)


@pytest.mark.parametrize('url', ['https://youtube.com.evil.test/watch?v=abcdefghijk', 'https://evil.test/youtube.com/watch?v=abcdefghijk', 'http://youtube.com@evil.test/watch?v=abcdefghijk'])
def test_youtube_host_spoof_rejected(url):
    assert youtube_id(url) is None


def test_dns_private_answer_rejected(monkeypatch):
    async def scenario():
        loop = asyncio.get_running_loop()
        monkeypatch.setattr(loop, 'getaddrinfo', AsyncMock(return_value=[(2, 1, 6, '', ('10.0.0.1', 443))]))
        with pytest.raises(ValueError):
            await PublicResolver().resolve('public-looking.example', 443)
    asyncio.run(scenario())


def test_duration_and_direct_link_policy():
    t = track(); t.duration = '5:00:00'
    with pytest.raises(ValueError): validate_track(t)
    t = track(); t.link = 'https://example.com/a.mp3'
    with pytest.raises(ValueError, match='disabled'): validate_track(t)


def test_capacity_checked_before_mutation(monkeypatch, isolated):
    monkeypatch.setattr(service.settings, 'max_queue_tracks', 1)
    isolated.add(1, track('old'))
    with pytest.raises(ValueError, match='Queue limit'):
        asyncio.run(service.enqueue(1, [track('extra')]))
    assert len(isolated.get(1).queue) == 1


def test_photo_card_to_track_without_art_edits_caption():
    target = NS(photo=True, edit_caption=AsyncMock(), edit_text=AsyncMock())
    asyncio.run(edit_track_card(target, track(), heading='Playing'))
    target.edit_caption.assert_awaited_once()
    target.edit_text.assert_not_awaited()


def test_broken_art_falls_back_to_plain_text():
    t = track(); t.thumbnail = 'https://example.com/broken.jpg'
    message = NS(reply_photo=AsyncMock(side_effect=RuntimeError), reply_text=AsyncMock())
    asyncio.run(reply_track_card(message, t, heading='Playing'))
    message.reply_text.assert_awaited_once()


def test_seek_bad_number_returns_usage():
    message = NS(command=['seek', '--1'], reply_text=AsyncMock())
    asyncio.run(seek_cmd(None, message))
    assert 'Usage' in message.reply_text.call_args.args[0]


def test_upcoming_queue_edits_protect_current(isolated):
    for name in ('a', 'b', 'c'): isolated.add(1, track(name))
    isolated.move(1, 3, 2)
    assert [t.title for t in isolated.get(1).queue] == ['a', 'c', 'b']
    with pytest.raises(ValueError): isolated.remove(1, 1)
    isolated.remove(1, 2)
    assert [t.title for t in isolated.get(1).queue] == ['a', 'b']
