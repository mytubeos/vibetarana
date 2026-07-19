"""Assistant userbot pool — the MTProto sessions that actually join and stream
into voice chats, load-balanced across chats. Each assistant is a distinct
session, so two assistants in two different chats' voice chats share no
protocol-level state.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

from pyrogram import Client
from pytgcalls import PyTgCalls

from bot.utils.logger import get_logger
from config import settings

logger = get_logger(__name__)


@dataclass
class Assistant:
    index: int
    client: Client
    call_py: PyTgCalls
    chats: set[int] = field(default_factory=set)
    healthy: bool = True


class AssistantPool:
    def __init__(self) -> None:
        self.assistants: list[Assistant] = []
        self._chat_assignment: dict[int, int] = {}
        # One lock for the whole pool, not per-chat: the thing being
        # protected (an assistant's free capacity) is shared across every
        # chat, so two different chats' get_or_assign() calls racing each
        # other could both see the same assistant as having room and both
        # assign to it — one of them then never actually joins the voice
        # chat at the Telegram level (a single account can only be in one
        # call at a time), later surfacing as NoActiveGroupCall on /stop.
        # Confirmed live: two /play commands in two different chats close
        # together with only one assistant configured.
        self._assign_lock = asyncio.Lock()

    async def start(self) -> None:
        for i, session_string in enumerate(settings.assistant_sessions):
            client = Client(
                name=f"assistant{i}",
                api_id=settings.api_id,
                api_hash=settings.api_hash,
                session_string=session_string,
            )
            call_py = PyTgCalls(client)
            try:
                await call_py.start()  # also connects `client` if not already connected
                me = await client.get_me()
            except Exception:
                # One invalid/revoked/rate-limited session shouldn't take the
                # whole bot down — skip it and keep starting the rest.
                logger.warning("Assistant session #%d failed to start — skipping it", i, exc_info=True)
                try:
                    await client.stop()
                except Exception:
                    pass
                continue
            # `index` must equal this assistant's position in self.assistants
            # (not its position in the config list) — get_assigned() and
            # _chat_assignment look it up via self.assistants[index], so a
            # gap from a skipped session would otherwise corrupt that lookup.
            index = len(self.assistants)
            self.assistants.append(Assistant(index=index, client=client, call_py=call_py))
            logger.info(f"Assistant {index} (config slot {i}) started as @{me.username or me.id}")
        if not self.assistants:
            raise RuntimeError("No assistants started — check ASSISTANT_SESSIONS in .env")

    async def stop(self) -> None:
        for assistant in self.assistants:
            try:
                await assistant.client.stop()
            except Exception:
                logger.warning("Assistant %d did not stop cleanly", assistant.index, exc_info=True)

    async def get_or_assign(self, chat_id: int) -> Assistant | None:
        """Return the assistant handling this chat. If none is assigned yet,
        pick the least-loaded healthy assistant with free capacity. Returns
        None if every assistant is at MAX_VC_PER_ASSISTANT capacity.
        """
        async with self._assign_lock:
            existing_index = self._chat_assignment.get(chat_id)
            if existing_index is not None:
                assistant = self.assistants[existing_index]
                if assistant.healthy:
                    return assistant
                assistant.chats.discard(chat_id)
                del self._chat_assignment[chat_id]

            candidates = [
                a for a in self.assistants
                if a.healthy and len(a.chats) < settings.max_vc_per_assistant
            ]
            if not candidates:
                return None
            chosen = min(candidates, key=lambda a: len(a.chats))
            chosen.chats.add(chat_id)
            self._chat_assignment[chat_id] = chosen.index
            return chosen

    def get_assigned(self, chat_id: int) -> Assistant | None:
        index = self._chat_assignment.get(chat_id)
        return self.assistants[index] if index is not None else None

    def release(self, chat_id: int) -> None:
        index = self._chat_assignment.pop(chat_id, None)
        if index is not None:
            self.assistants[index].chats.discard(chat_id)

    async def health_check_loop(self, interval_seconds: int = 60) -> None:
        """Periodically confirm each assistant is still connected, catching
        silent disconnects between user actions."""
        while True:
            await asyncio.sleep(interval_seconds)
            for assistant in self.assistants:
                try:
                    await assistant.client.get_me()
                    assistant.healthy = True
                except Exception:
                    logger.warning("Assistant %d failed health check", assistant.index, exc_info=True)
                    assistant.healthy = False


pool = AssistantPool()
