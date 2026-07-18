"""Pure-logic tests for bot/core/queue.py — no Telegram/Mongo/network needed."""
import json

import pytest

from bot.core.queue import MAX_IMPORT_TRACKS, QueueManager, Track


def make_track(title: str, link: str = "https://youtube.com/watch?v=abc12345678") -> Track:
    return Track(
        title=title, duration="3:00", link=link, thumbnail=None,
        requested_by=1, requested_by_name="tester",
    )


def track_payload(title: str, link: str = "https://youtube.com/watch?v=abc12345678") -> dict:
    return {
        "title": title, "duration": "1:00", "link": link, "thumbnail": None,
        "requested_by": 1, "requested_by_name": "tester", "source": "YouTube",
    }


# --- state defaults ---------------------------------------------------------

def test_new_chat_state_defaults():
    qm = QueueManager()
    state = qm.get(1)
    assert state.autoplay is False
    assert state.last_track is None


# --- advance() / loop_mode -------------------------------------------------

def test_advance_loop_off_drops_finished_track():
    qm = QueueManager()
    a, b = make_track("a"), make_track("b")
    qm.add(1, a)
    qm.add(1, b)
    assert qm.advance(1) is b
    assert qm.get(1).queue == [b]


def test_advance_records_last_track_for_autoplay():
    qm = QueueManager()
    a, b = make_track("a"), make_track("b")
    qm.add(1, a)
    qm.add(1, b)
    qm.advance(1)  # a finishes, b becomes current
    assert qm.get(1).last_track is a
    qm.advance(1)  # b finishes, queue now empty
    assert qm.get(1).last_track is b
    assert qm.get(1).current is None


def test_advance_does_not_record_last_track_under_loop_one_replay():
    # Non-forced advance under loop="one" doesn't pop anything, so nothing
    # "finished" — last_track should stay whatever it was before.
    qm = QueueManager()
    a = make_track("a")
    qm.add(1, a)
    qm.get(1).loop_mode = "one"
    assert qm.get(1).last_track is None
    qm.advance(1)
    assert qm.get(1).last_track is None


def test_advance_loop_one_replays_same_track():
    qm = QueueManager()
    a, b = make_track("a"), make_track("b")
    qm.add(1, a)
    qm.add(1, b)
    qm.get(1).loop_mode = "one"
    assert qm.advance(1) is a
    assert qm.get(1).queue == [a, b]


def test_advance_loop_all_requeues_finished_track_at_end():
    qm = QueueManager()
    a, b = make_track("a"), make_track("b")
    qm.add(1, a)
    qm.add(1, b)
    qm.get(1).loop_mode = "all"
    assert qm.advance(1) is b
    assert qm.get(1).queue == [b, a]


def test_advance_loop_one_force_still_moves_on():
    # A manual /skip (force=True) must always move to the next track, even
    # under loop_mode="one" — otherwise loop="one" would make /skip a
    # permanent no-op, since the non-forced path deliberately replays.
    qm = QueueManager()
    a, b = make_track("a"), make_track("b")
    qm.add(1, a)
    qm.add(1, b)
    qm.get(1).loop_mode = "one"
    assert qm.advance(1, force=True) is b
    assert qm.get(1).queue == [b]


def test_advance_loop_all_force_still_requeues_at_end():
    qm = QueueManager()
    a, b = make_track("a"), make_track("b")
    qm.add(1, a)
    qm.add(1, b)
    qm.get(1).loop_mode = "all"
    assert qm.advance(1, force=True) is b
    assert qm.get(1).queue == [b, a]


@pytest.mark.parametrize("mode", ["off", "one", "all"])
def test_advance_empty_queue_is_a_noop(mode):
    qm = QueueManager()
    qm.get(1).loop_mode = mode
    assert qm.advance(1) is None
    assert qm.get(1).queue == []


def test_advance_single_track_loop_all_repeats_forever():
    qm = QueueManager()
    a = make_track("a")
    qm.add(1, a)
    qm.get(1).loop_mode = "all"
    assert qm.advance(1) is a
    assert qm.get(1).queue == [a]


# --- shuffle() ---------------------------------------------------------

@pytest.mark.parametrize("count", [0, 1, 2])
def test_shuffle_returns_false_below_three_tracks(count):
    qm = QueueManager()
    for i in range(count):
        qm.add(1, make_track(str(i)))
    assert qm.shuffle(1) is False


def test_shuffle_keeps_current_track_at_index_zero():
    qm = QueueManager()
    tracks = [make_track(str(i)) for i in range(6)]
    for t in tracks:
        qm.add(1, t)
    assert qm.shuffle(1) is True
    assert qm.get(1).queue[0] is tracks[0]
    assert {id(t) for t in qm.get(1).queue} == {id(t) for t in tracks}


# --- export() / import_queue() ------------------------------------------

def test_export_import_round_trip_preserves_tracks():
    qm = QueueManager()
    qm.add(1, make_track("a"))
    qm.add(1, make_track("b"))
    exported = qm.export(1)

    qm2 = QueueManager()
    added = qm2.import_queue(2, json.dumps(exported).encode("utf-8"))
    assert added == 2
    assert [t.title for t in qm2.get(2).queue] == ["a", "b"]


def test_import_appends_rather_than_replacing():
    qm = QueueManager()
    qm.add(1, make_track("existing"))
    payload = json.dumps({"tracks": [track_payload("new")]}).encode("utf-8")
    added = qm.import_queue(1, payload)
    assert added == 1
    assert [t.title for t in qm.get(1).queue] == ["existing", "new"]


def test_import_queue_rejects_invalid_json():
    with pytest.raises(ValueError):
        QueueManager().import_queue(1, b"not valid json")


def test_import_queue_rejects_non_dict_root():
    with pytest.raises(ValueError):
        QueueManager().import_queue(1, b"[]")


def test_import_queue_rejects_missing_tracks_key():
    with pytest.raises(ValueError):
        QueueManager().import_queue(1, json.dumps({}).encode())


def test_import_queue_rejects_non_list_tracks():
    with pytest.raises(ValueError):
        QueueManager().import_queue(1, json.dumps({"tracks": "nope"}).encode())


def test_import_queue_rejects_oversized_track_list():
    payload = json.dumps({"tracks": [track_payload("x")] * (MAX_IMPORT_TRACKS + 1)}).encode()
    with pytest.raises(ValueError):
        QueueManager().import_queue(1, payload)


def test_import_queue_rejects_track_missing_required_fields():
    payload = json.dumps({"tracks": [{"title": "incomplete"}]}).encode()
    with pytest.raises(ValueError):
        QueueManager().import_queue(1, payload)


def test_import_queue_rejects_non_http_link():
    bad = track_payload("x", link="ftp://example.com/song.mp3")
    payload = json.dumps({"tracks": [bad]}).encode()
    with pytest.raises(ValueError):
        QueueManager().import_queue(1, payload)


def test_import_queue_never_raises_anything_but_valueerror():
    for bad_payload in (b"", b"null", b"{}", b'{"tracks":[1,2,3]}'):
        with pytest.raises(ValueError):
            QueueManager().import_queue(1, bad_payload)
