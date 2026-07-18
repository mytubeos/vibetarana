"""In-memory per-chat playback queue and state. Not persisted — losing this on
restart is acceptable (deliberate; restart-persistence is a later phase). Only
`chat_settings` (Mongo, see bot/core/db.py) holds deliberately-persisted config.
"""
from __future__ import annotations

import json
import random
from dataclasses import asdict, dataclass, field
from typing import Literal

LoopMode = Literal["off", "one", "all"]

MAX_IMPORT_TRACKS = 100
_REQUIRED_TRACK_FIELDS = {"title", "duration", "link", "requested_by", "requested_by_name"}


@dataclass
class Track:
    title: str
    duration: str
    link: str
    thumbnail: str | None
    requested_by: int
    requested_by_name: str
    source: str = "YouTube"
    # Set by /vplay (vs. /play) after resolution — bot/core/calls.py checks
    # this per-track, not per-chat, so a queue can mix audio-only and video
    # tracks depending on which command queued each one.
    video: bool = False


@dataclass
class ChatPlaybackState:
    queue: list[Track] = field(default_factory=list)
    is_paused: bool = False
    loop_mode: LoopMode = "off"

    @property
    def current(self) -> Track | None:
        return self.queue[0] if self.queue else None


def _track_from_dict(data: object) -> Track:
    if not isinstance(data, dict) or not _REQUIRED_TRACK_FIELDS <= data.keys():
        raise ValueError("track is missing required fields")
    link = str(data["link"])
    if not link.startswith(("http://", "https://")):
        raise ValueError("track link must be an http(s) URL")
    try:
        requested_by = int(data["requested_by"])
    except (TypeError, ValueError) as exc:
        raise ValueError("track requested_by must be an integer") from exc
    return Track(
        title=str(data["title"])[:300],
        duration=str(data["duration"])[:50],
        link=link,
        thumbnail=(str(data["thumbnail"]) if data.get("thumbnail") else None),
        requested_by=requested_by,
        requested_by_name=str(data["requested_by_name"])[:200],
        source=str(data.get("source", "YouTube"))[:50],
        video=bool(data.get("video", False)),
    )


class QueueManager:
    """Holds one ChatPlaybackState per chat_id."""

    def __init__(self) -> None:
        self._chats: dict[int, ChatPlaybackState] = {}

    def get(self, chat_id: int) -> ChatPlaybackState:
        if chat_id not in self._chats:
            self._chats[chat_id] = ChatPlaybackState()
        return self._chats[chat_id]

    def add(self, chat_id: int, track: Track) -> int:
        """Append a track, returning its 1-based position in the queue."""
        state = self.get(chat_id)
        state.queue.append(track)
        return len(state.queue)

    def advance(self, chat_id: int, *, force: bool = False) -> Track | None:
        """Drop the finished/skipped track, return the next one to play (or
        None if empty). Under loop_mode "one", a natural stream-end (force=
        False, the default) replays the same track; an explicit /skip
        (force=True) always moves on regardless of loop_mode — otherwise
        loop="one" would make /skip a permanent no-op."""
        state = self.get(chat_id)
        if state.queue:
            if state.loop_mode == "one" and not force:
                return state.current
            finished = state.queue.pop(0)
            if state.loop_mode == "all":
                state.queue.append(finished)
        return state.current

    def clear(self, chat_id: int) -> None:
        self._chats.pop(chat_id, None)

    def shuffle(self, chat_id: int) -> bool:
        """Randomize queued (not currently-playing) tracks in place. Returns
        False if there are fewer than 3 tracks (nothing meaningful to shuffle)."""
        state = self.get(chat_id)
        if len(state.queue) < 3:
            return False
        rest = state.queue[1:]
        random.shuffle(rest)
        state.queue[1:] = rest
        return True

    def export(self, chat_id: int) -> dict:
        state = self.get(chat_id)
        return {
            "version": 1,
            "chat_id": chat_id,
            "loop_mode": state.loop_mode,
            "tracks": [asdict(t) for t in state.queue],
        }

    def import_queue(self, chat_id: int, payload: bytes) -> int:
        """Append tracks parsed from an exported-queue JSON payload. Always
        appends (never replaces — index 0 may be actively streaming; run
        /stop first for a clean slate). Raises ValueError on any malformed or
        hostile input, never lets a bad file crash the bot."""
        try:
            data = json.loads(payload)
        except json.JSONDecodeError as exc:
            raise ValueError(f"not valid JSON: {exc}") from exc
        if not isinstance(data, dict) or not isinstance(data.get("tracks"), list):
            raise ValueError("missing or invalid 'tracks' array")
        if len(data["tracks"]) > MAX_IMPORT_TRACKS:
            raise ValueError(f"too many tracks (max {MAX_IMPORT_TRACKS})")
        tracks = [_track_from_dict(t) for t in data["tracks"]]
        self.get(chat_id).queue.extend(tracks)
        return len(tracks)


queues = QueueManager()
