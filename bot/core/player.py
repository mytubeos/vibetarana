"""One editable panel per group, refreshed on actions and natural advances."""
import asyncio
import time
from pyrogram.enums import ParseMode
from bot.core.client import bot
from bot.core.queue import queues
from bot.core.session import get_session, sessions
from bot.utils.formatting import playback_keyboard, format_seconds
from bot.core import preferences
from bot.core.i18n import text


def panel(chat_id, language='en'):
    state = queues.get(chat_id)
    track = state.current
    if track is None:
        return text('empty', language)
    elapsed = state.elapsed
    if state.started_at is not None and not state.is_paused:
        elapsed += time.monotonic() - state.started_at
    status = '⏸ /resume' if state.needs_resume else ('⏸' if state.is_paused else '▶️')
    upcoming = state.queue[1].title[:100] if len(state.queue) > 1 else '—'
    return (f'🎵 Vibetarana · {text("playing", language)}\n\n'
            f'{track.title[:250]}\n{status} {format_seconds(int(elapsed))} / {track.duration}\n'
            f'🙋 {track.requested_by_name[:100]} · {track.source[:30]}\n'
            f'Next: {upcoming}\nQueue: {len(state.queue)} · Loop: {state.loop_mode} · Autoplay: {"on" if state.autoplay else "off"}')


async def refresh(chat_id: int, *, create=False):
    session = get_session(chat_id)
    if session.card is None and not create:
        return
    pref = await preferences.get(chat_id)
    state = queues.get(chat_id)
    markup = playback_keyboard(paused=state.is_paused or state.needs_resume) if state.current else None
    content = panel(chat_id, pref['language'])
    if session.card is not None:
        try:
            await session.card.edit_text(content, reply_markup=markup, parse_mode=ParseMode.DISABLED)
            return
        except Exception as exc:
            if 'MESSAGE_NOT_MODIFIED' in str(exc):
                return
            if not create:
                return
            try:
                await session.card.edit_reply_markup(None)
            except Exception:
                pass
    session.card = await bot.send_message(chat_id, content, reply_markup=markup, parse_mode=ParseMode.DISABLED)


async def refresh_loop():
    while True:
        await asyncio.sleep(30)
        for chat_id in list(sessions):
            state = queues.get(chat_id)
            if state.current and not state.is_paused and not state.needs_resume:
                try:
                    await refresh(chat_id)
                except Exception:
                    pass
                await asyncio.sleep(0.1)
