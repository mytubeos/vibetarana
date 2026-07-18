"""Tests for bot/core/calls.py's _try_autoplay() — no real Pyrogram/py-tgcalls/
network. pool/bot/youtube.get_related/_play_track are all monkeypatched so
only _try_autoplay's own control-flow logic (including its race guard) is
under test.
"""
import asyncio

import pytest

import bot.core.calls as calls
from bot.core.queue import QueueManager, Track


def make_track(title: str = "seed") -> Track:
    return Track(
        title=title, duration="1:00", link="https://youtube.com/watch?v=seed12345678",
        thumbnail=None, requested_by=1, requested_by_name="tester",
    )


class _FakeAssistant:
    pass


@pytest.fixture
def isolated_queues(monkeypatch):
    qm = QueueManager()
    monkeypatch.setattr(calls, "queues", qm)
    return qm


def _patch_play_and_send(monkeypatch):
    play_calls = []

    async def fake_play_track(assistant, chat_id, track):
        play_calls.append((assistant, chat_id, track))

    async def fake_send_message(*args, **kwargs):
        pass

    monkeypatch.setattr(calls, "_play_track", fake_play_track)
    monkeypatch.setattr(calls.bot, "send_message", fake_send_message)
    return play_calls


def test_try_autoplay_does_nothing_when_disabled(isolated_queues):
    isolated_queues.get(1).autoplay = False
    isolated_queues.get(1).last_track = make_track()
    assert asyncio.run(calls._try_autoplay(1)) is False


def test_try_autoplay_does_nothing_without_a_last_track(isolated_queues):
    isolated_queues.get(1).autoplay = True
    assert asyncio.run(calls._try_autoplay(1)) is False


def test_try_autoplay_does_nothing_without_an_assigned_assistant(isolated_queues, monkeypatch):
    isolated_queues.get(1).autoplay = True
    isolated_queues.get(1).last_track = make_track()
    monkeypatch.setattr(calls.pool, "get_assigned", lambda chat_id: None)
    assert asyncio.run(calls._try_autoplay(1)) is False


def test_try_autoplay_does_nothing_when_no_related_track_found(isolated_queues, monkeypatch):
    isolated_queues.get(1).autoplay = True
    isolated_queues.get(1).last_track = make_track()
    monkeypatch.setattr(calls.pool, "get_assigned", lambda chat_id: _FakeAssistant())

    async def fake_get_related(link):
        return None
    monkeypatch.setattr(calls.youtube, "get_related", fake_get_related)

    assert asyncio.run(calls._try_autoplay(1)) is False


def test_try_autoplay_starts_related_track_and_announces_it(isolated_queues, monkeypatch):
    qm = isolated_queues
    qm.get(1).autoplay = True
    qm.get(1).last_track = make_track()
    fake_assistant = _FakeAssistant()
    monkeypatch.setattr(calls.pool, "get_assigned", lambda chat_id: fake_assistant)
    related = make_track("related")

    async def fake_get_related(link):
        return related
    monkeypatch.setattr(calls.youtube, "get_related", fake_get_related)
    play_calls = _patch_play_and_send(monkeypatch)

    assert asyncio.run(calls._try_autoplay(1)) is True
    assert play_calls == [(fake_assistant, 1, related)]
    assert qm.get(1).queue == [related]


def test_try_autoplay_aborts_if_stop_ran_during_the_network_lookup(isolated_queues, monkeypatch):
    # Regression test: a real bug — /stop racing in while get_related()'s
    # network call is still in flight used to resurrect playback in a chat
    # the user just told the bot to leave (queues.clear() removes the chat's
    # state entirely; the old, now-orphaned `state` object must not be acted
    # on once that's happened).
    qm = isolated_queues
    qm.get(1).autoplay = True
    qm.get(1).last_track = make_track()
    fake_assistant = _FakeAssistant()
    monkeypatch.setattr(calls.pool, "get_assigned", lambda chat_id: fake_assistant)

    async def fake_get_related_with_concurrent_stop(link):
        qm.clear(1)  # simulates /stop finishing while this await was pending
        return make_track("related")
    monkeypatch.setattr(calls.youtube, "get_related", fake_get_related_with_concurrent_stop)
    play_calls = _patch_play_and_send(monkeypatch)

    assert asyncio.run(calls._try_autoplay(1)) is False
    assert play_calls == []


def test_try_autoplay_aborts_if_assistant_reassigned_during_the_network_lookup(isolated_queues, monkeypatch):
    # Same class of race, triggered by the assistant pool changing instead of
    # the queue: e.g. a concurrent /play winning the race and getting a
    # (possibly different) assistant assigned before this lookup returns.
    qm = isolated_queues
    qm.get(1).autoplay = True
    qm.get(1).last_track = make_track()
    original_assistant = _FakeAssistant()
    other_assistant = _FakeAssistant()
    assigned = {"current": original_assistant}
    monkeypatch.setattr(calls.pool, "get_assigned", lambda chat_id: assigned["current"])

    async def fake_get_related_with_reassignment(link):
        assigned["current"] = other_assistant
        return make_track("related")
    monkeypatch.setattr(calls.youtube, "get_related", fake_get_related_with_reassignment)
    play_calls = _patch_play_and_send(monkeypatch)

    assert asyncio.run(calls._try_autoplay(1)) is False
    assert play_calls == []
