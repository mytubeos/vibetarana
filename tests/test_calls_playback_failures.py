"""Tests for bot/core/calls.py's playback-failure handling — no real
Pyrogram/py-tgcalls/network. pool/bot/_play_track/_try_autoplay are all
monkeypatched so only the control flow under test is exercised.
"""
import asyncio

import pytest

import bot.core.calls as calls
from bot.core.queue import QueueManager, Track


def make_track(title: str = "a") -> Track:
    return Track(
        title=title, duration="1:00", link=f"https://youtube.com/watch?v={title}12345678",
        thumbnail=None, requested_by=1, requested_by_name="tester",
    )


class _FakeAssistant:
    def __init__(self, healthy: bool = True) -> None:
        self.healthy = healthy


@pytest.fixture
def isolated_queues(monkeypatch):
    qm = QueueManager()
    monkeypatch.setattr(calls, "queues", qm)
    return qm


def _patch_send(monkeypatch):
    async def fake_send_message(*args, **kwargs):
        pass
    monkeypatch.setattr(calls.bot, "send_message", fake_send_message)


def _patch_no_autoplay(monkeypatch):
    async def fake_try_autoplay(chat_id):
        return False
    monkeypatch.setattr(calls, "_try_autoplay", fake_try_autoplay)


# --- _get_healthy_assistant --------------------------------------------

def test_get_healthy_assistant_none_when_unassigned(monkeypatch):
    monkeypatch.setattr(calls.pool, "get_assigned", lambda chat_id: None)
    assert calls._get_healthy_assistant(1) is None


def test_get_healthy_assistant_none_when_unhealthy(monkeypatch):
    unhealthy = _FakeAssistant(healthy=False)
    monkeypatch.setattr(calls.pool, "get_assigned", lambda chat_id: unhealthy)
    assert calls._get_healthy_assistant(1) is None


def test_get_healthy_assistant_returns_it_when_healthy(monkeypatch):
    healthy = _FakeAssistant(healthy=True)
    monkeypatch.setattr(calls.pool, "get_assigned", lambda chat_id: healthy)
    assert calls._get_healthy_assistant(1) is healthy


# --- _advance_and_play cascading skip -----------------------------------

def test_advance_and_play_skips_a_single_broken_track(isolated_queues, monkeypatch):
    qm = isolated_queues
    a, b, c = make_track("a"), make_track("b"), make_track("c")
    qm.add(1, a)
    qm.add(1, b)
    qm.add(1, c)

    fake_assistant = _FakeAssistant()
    monkeypatch.setattr(calls, "_get_healthy_assistant", lambda chat_id: fake_assistant)
    _patch_send(monkeypatch)
    _patch_no_autoplay(monkeypatch)

    attempted = []

    async def fake_play_track(assistant, chat_id, track):
        attempted.append(track.title)
        return track.title != "b"  # b fails, c succeeds

    monkeypatch.setattr(calls, "_play_track", fake_play_track)

    asyncio.run(calls._advance_and_play(1))  # simulates "a" just finished
    assert attempted == ["b", "c"]
    assert qm.get(1).queue == [c]


def test_advance_and_play_clears_queue_after_max_consecutive_failures(isolated_queues, monkeypatch):
    qm = isolated_queues
    for t in [make_track(str(i)) for i in range(5)]:
        qm.add(1, t)

    fake_assistant = _FakeAssistant()
    monkeypatch.setattr(calls, "_get_healthy_assistant", lambda chat_id: fake_assistant)
    _patch_send(monkeypatch)
    _patch_no_autoplay(monkeypatch)

    attempted = []

    async def fake_play_track(assistant, chat_id, track):
        attempted.append(track.title)
        return False  # everything fails

    monkeypatch.setattr(calls, "_play_track", fake_play_track)

    leave_scheduled = []

    async def fake_schedule_auto_leave(chat_id):
        leave_scheduled.append(chat_id)

    monkeypatch.setattr(calls, "_schedule_auto_leave", fake_schedule_auto_leave)

    asyncio.run(calls._advance_and_play(1))
    assert len(attempted) == calls.MAX_CONSECUTIVE_PLAY_FAILURES
    assert qm.get(1).queue == []  # cleared, not left with a phantom "current"
    assert leave_scheduled == [1]


# --- join_and_play releases the pool slot on playback failure -----------

def test_join_and_play_releases_pool_slot_when_playback_fails(monkeypatch):
    fake_assistant = _FakeAssistant()

    async def fake_get_or_assign(chat_id):
        return fake_assistant

    monkeypatch.setattr(calls.pool, "get_or_assign", fake_get_or_assign)
    released = []
    monkeypatch.setattr(calls.pool, "release", lambda chat_id: released.append(chat_id))

    async def fake_play_track(assistant, chat_id, track):
        return False

    monkeypatch.setattr(calls, "_play_track", fake_play_track)

    result = asyncio.run(calls.join_and_play(1, make_track()))
    assert result is None
    assert released == [1]


def test_join_and_play_returns_assistant_on_success(monkeypatch):
    fake_assistant = _FakeAssistant()

    async def fake_get_or_assign(chat_id):
        return fake_assistant

    monkeypatch.setattr(calls.pool, "get_or_assign", fake_get_or_assign)
    released = []
    monkeypatch.setattr(calls.pool, "release", lambda chat_id: released.append(chat_id))

    async def fake_play_track(assistant, chat_id, track):
        return True

    monkeypatch.setattr(calls, "_play_track", fake_play_track)

    result = asyncio.run(calls.join_and_play(1, make_track()))
    assert result is fake_assistant
    assert released == []
