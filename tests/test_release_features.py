import asyncio
import time
from dataclasses import asdict
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock
import pytest

from bot.core import calls, preferences, session, service
from bot.core.queue import QueueManager, Track
from bot.core.decorators import can_request, can_control
from bot.plugins import library, play
from bot.plugins.controls import playback_callback
from pyrogram.enums import ChatType, ChatMemberStatus


def track(title='song'):
    return Track(title, '1:00', 'https://youtube.com/watch?v=abcdefghijk', None, 7, 'user')


@pytest.fixture(autouse=True)
def reset(monkeypatch):
    monkeypatch.setattr(session, 'sessions', {})
    monkeypatch.setattr(calls, '_audio_url_cache', {})
    monkeypatch.setattr(preferences, '_cache', {})
    monkeypatch.setattr(preferences, '_locks', {})
    monkeypatch.setattr(play, '_choices', {})
    monkeypatch.setattr(play, 'allow_request', lambda *a: True)
    monkeypatch.setattr(library, 'allow_request', lambda *a: True)


def test_same_song_shares_extraction(monkeypatch):
    async def scenario():
        monkeypatch.setattr(calls, '_inflight', {})
        monkeypatch.setattr(calls, '_extract_slots', asyncio.Semaphore(2))
        async def extract(*a):
            await asyncio.sleep(0.01)
            return ('video', 'audio')
        mock = AsyncMock(side_effect=extract)
        monkeypatch.setattr(calls.YtDlp, 'extract', mock)
        result = await asyncio.gather(calls.extract_urls(track().link, False), calls.extract_urls(track().link, False))
        assert result == [('video', 'audio'), ('video', 'audio')]
        mock.assert_awaited_once()
    asyncio.run(scenario())


def test_cancelled_waiter_does_not_cancel_shared_extraction(monkeypatch):
    async def scenario():
        monkeypatch.setattr(calls, '_inflight', {})
        monkeypatch.setattr(calls, '_extract_slots', asyncio.Semaphore(2))
        entered, release = asyncio.Event(), asyncio.Event()
        async def extract(*a):
            entered.set(); await release.wait(); return ('v', 'a')
        monkeypatch.setattr(calls.YtDlp, 'extract', extract)
        first = asyncio.create_task(calls.extract_urls(track().link, False))
        await entered.wait()
        second = asyncio.create_task(calls.extract_urls(track().link, False))
        first.cancel()
        with pytest.raises(asyncio.CancelledError): await first
        release.set()
        assert await second == ('v', 'a')
    asyncio.run(scenario())


def test_stale_cached_url_retries_fresh_once(monkeypatch):
    async def scenario():
        q = QueueManager(); t = track(); t.video = True; q.add(1, t)
        monkeypatch.setattr(calls, 'queues', q)
        monkeypatch.setattr(calls, '_prefetch_cache', {})
        monkeypatch.setattr(calls, '_cached_urls_get', lambda _: ('stale-v', 'stale-a'))
        monkeypatch.setattr(calls, 'MediaStream', lambda *a, **kw: a[0])
        monkeypatch.setattr(calls, '_video_flags', lambda _: None)
        monkeypatch.setattr(calls, '_cached_urls_put', AsyncMock())
        monkeypatch.setattr(calls, '_record_play', AsyncMock())
        monkeypatch.setattr(calls, 'schedule_prefetch', lambda _: None)
        fresh = AsyncMock(return_value=('fresh-v', 'fresh-a')); monkeypatch.setattr(calls, 'extract_urls', fresh)
        a = NS(call_py=NS(play=AsyncMock(side_effect=[RuntimeError('expired'), None])))
        assert await calls._play_track(a, 1, t)
        assert a.call_py.play.await_count == 2
        fresh.assert_awaited_once()
        await asyncio.sleep(0)
    asyncio.run(scenario())


def test_expired_prefetch_is_not_used(monkeypatch):
    async def scenario():
        t = track(); q = QueueManager(); q.add(1, t)
        monkeypatch.setattr(calls, 'queues', q)
        monkeypatch.setattr(calls, '_prefetch_cache', {(t.link, False): (('stale', 'stale'), time.monotonic() - 601)})
        monkeypatch.setattr(calls, 'MediaStream', lambda *a, **kw: a[0])
        monkeypatch.setattr(calls, '_video_flags', lambda _: None)
        monkeypatch.setattr(calls, 'schedule_prefetch', lambda _: None)
        monkeypatch.setattr(calls, '_record_play', AsyncMock())
        fresh = AsyncMock(return_value=('fresh', 'fresh')); monkeypatch.setattr(calls, 'extract_urls', fresh)
        a = NS(call_py=NS(play=AsyncMock()))
        assert await calls._play_track(a, 1, t)
        assert a.call_py.play.call_args.args[1] == 'fresh'
        fresh.assert_awaited_once()
        await asyncio.sleep(0)
    asyncio.run(scenario())


def test_member_mode_allows_requests_but_not_controls(monkeypatch):
    import bot.core.decorators as permissions
    monkeypatch.setattr(permissions, 'user_is_admin_or_owner', AsyncMock(return_value=False))
    monkeypatch.setattr(preferences, 'get', AsyncMock(return_value={'play_mode': 'members', 'dj_users': []}))
    client = NS(get_chat_member=AsyncMock(return_value=NS(status=ChatMemberStatus.MEMBER)))
    chat = NS(id=1, type=ChatType.SUPERGROUP)
    assert asyncio.run(can_request(client, 7, chat))
    assert not asyncio.run(can_control(client, 7, chat))


def test_removed_dj_cannot_use_old_callback(monkeypatch):
    import bot.core.decorators as permissions
    monkeypatch.setattr(permissions, 'user_is_admin_or_owner', AsyncMock(return_value=False))
    monkeypatch.setattr(preferences, 'get', AsyncMock(return_value={'play_mode': 'admins', 'dj_users': [7]}))
    client = NS(get_chat_member=AsyncMock(return_value=NS(status=ChatMemberStatus.LEFT)))
    assert not asyncio.run(can_control(client, 7, NS(id=1)))


def test_stale_player_callback_is_rejected():
    session.get_session(1).card = NS(id=20)
    callback = NS(message=NS(id=10, chat=NS(id=1)), answer=AsyncMock())
    asyncio.run(playback_callback(None, callback))
    assert 'old' in callback.answer.call_args.args[0]


def test_search_choice_is_bound_to_requester(monkeypatch):
    play._choices['token'] = (time.monotonic()+120, 1, 7, 0, [track()])
    callback = NS(data='pick:token:0', from_user=NS(id=99), message=NS(chat=NS(id=1)), answer=AsyncMock())
    enqueue = AsyncMock(); monkeypatch.setattr(service, 'enqueue', enqueue)
    asyncio.run(play.pick_song(None, callback))
    enqueue.assert_not_awaited()
    assert 'another listener' in callback.answer.call_args.args[0]


def test_personal_playlist_load_uses_calling_user_scope(monkeypatch):
    monkeypatch.setattr(library, 'can_request', AsyncMock(return_value=True))
    get = AsyncMock(return_value=[asdict(track())]); monkeypatch.setattr(library.db, 'get_playlist', get)
    enqueue = AsyncMock(); monkeypatch.setattr(service, 'enqueue', enqueue)
    monkeypatch.setattr(library.player, 'refresh', AsyncMock())
    msg = NS(command=['playlist','load','favourites'], from_user=NS(id=42, first_name='me'), chat=NS(id=-100), reply_text=AsyncMock())
    asyncio.run(library.playlist_cmd(None, msg))
    get.assert_awaited_once_with('user:42', 'favourites')
    assert enqueue.call_args.args[1][0].requested_by == 42


def test_group_playlist_edit_requires_control_permission(monkeypatch):
    monkeypatch.setattr(library, 'can_control', AsyncMock(return_value=False))
    save = AsyncMock(); monkeypatch.setattr(library.db, 'save_playlist', save)
    msg = NS(command=['gplaylist','save','party'], from_user=NS(id=42), chat=NS(id=-100), reply_text=AsyncMock())
    asyncio.run(library.playlist_cmd(None,msg))
    save.assert_not_awaited()


def test_media_reply_rejects_oversized_attachment():
    media = NS(file_size=300*1024*1024)
    message = NS(reply_to_message=NS(audio=media, video=None, voice=None))
    with pytest.raises(ValueError, match='size'): play.replied_media(message, False)


def test_local_probe_accepts_wav_and_rejects_playlist(tmp_path):
    import wave
    from bot.core.media import _probe
    path = tmp_path / 'sample.wav'
    with wave.open(str(path), 'wb') as f:
        f.setnchannels(1); f.setsampwidth(2); f.setframerate(8000); f.writeframes(b'\0' * 16000)
    asyncio.run(_probe(path))
    playlist = tmp_path / 'bad.m3u8'
    playlist.write_text('#EXTM3U\n#EXT-X-TARGETDURATION:10\n#EXTINF:10,\nhttp://127.0.0.1/private\n#EXT-X-ENDLIST')
    with pytest.raises((ValueError, KeyError)):
        asyncio.run(_probe(playlist))
