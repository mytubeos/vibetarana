"""Serialize chat mutations; stop invalidates pending work before waiting for it."""
import asyncio
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass, field


@dataclass
class Session:
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    revision: int = 0
    task: asyncio.Task | None = None
    card: object | None = None
    pending_requests: int = 0


sessions: dict[int, Session] = {}


def get_session(chat_id: int) -> Session:
    return sessions.setdefault(chat_id, Session())


@asynccontextmanager
async def operation(chat_id: int):
    from bot.core.lease import lease
    lease.check()
    session = get_session(chat_id)
    ticket = session.revision
    async with session.lock:
        lease.check()
        if ticket != session.revision:
            raise asyncio.CancelledError
        previous = session.task
        session.task = asyncio.current_task()
        try:
            yield session
        finally:
            session.task = previous


async def cancel_pending(chat_id: int) -> Session:
    session = get_session(chat_id)
    session.revision += 1
    task = session.task
    if task and task is not asyncio.current_task() and not task.done():
        task.cancel()
        with suppress(asyncio.CancelledError, Exception):
            await task
    return session
