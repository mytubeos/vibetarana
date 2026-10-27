"""Group administration and diagnostics without exposing credentials."""
from pyrogram import Client, filters
from pyrogram.enums import ParseMode
from bot.core import preferences, db
from bot.core.assistants import pool
from bot.core.decorators import admin_filter
from bot.core.session import operation
from bot.core.queue import queues
from config import settings


@Client.on_message(filters.command('settings') & filters.group & admin_filter)
async def settings_cmd(_, message):
    chat_id = message.chat.id
    args = message.command[1:]
    if not args:
        p = await preferences.get(chat_id)
        await message.reply_text(f"Request mode: {p['play_mode']}\nLanguage: {p['language']}\nDJs: {len(p['dj_users'])}\nFair queue: {p['fair_queue']} · Vote threshold: {p['vote_skip']}\nMusic: {p['music_language']} · Video: {p['video_quality']}p\n/policy to change listening rules\n/settings mode admins|members\n/settings language en|hi|hinglish\n/dj add user_id · /dj remove user_id", parse_mode=ParseMode.DISABLED)
        return
    if len(args) != 2 or not ((args[0] == 'mode' and args[1] in ('admins', 'members')) or (args[0] == 'language' and args[1] in ('en', 'hi', 'hinglish'))):
        await message.reply_text('Usage: /settings mode admins|members or /settings language en|hi|hinglish')
        return
    async with operation(chat_id):
        await preferences.set(chat_id, **{('play_mode' if args[0] == 'mode' else 'language'): args[1]})
    await message.reply_text('Settings saved.')


@Client.on_message(filters.command('dj') & filters.group & admin_filter)
async def dj_cmd(client, message):
    args = message.command[1:]
    try:
        if len(args) != 2 or args[0] not in ('add', 'remove'):
            raise ValueError
        user_id = int(args[1])
        if user_id <= 0:
            raise ValueError
        if args[0] == 'add':
            await client.get_chat_member(message.chat.id, user_id)
        async with operation(message.chat.id):
            p = await preferences.get(message.chat.id)
            ids = set(p['dj_users'])
            if args[0] == 'add':
                if len(ids) >= 50 and user_id not in ids:
                    raise ValueError
                ids.add(user_id)
            else:
                ids.discard(user_id)
            await preferences.set(message.chat.id, dj_users=sorted(ids))
        await message.reply_text('DJ list updated.')
    except Exception:
        await message.reply_text('Usage: /dj add user_id or /dj remove user_id. User must be in this group; maximum 50 DJs.')


@Client.on_message(filters.command('doctor') & filters.group & admin_filter)
async def doctor_cmd(client, message):
    rows = ['Vibetarana diagnostics']
    try:
        await db.db.command('ping')
        rows.append('Database: connected')
    except Exception:
        rows.append('Database: unavailable')
    healthy = [a for a in pool.assistants if a.healthy]
    free = [a for a in healthy if not a.chats]
    rows.append(f'Assistants: {len(healthy)} healthy / {len(pool.assistants)} configured; {len(free)} free')
    try:
        chat = await client.get_chat(message.chat.id)
        rows.append('Voice chat: ' + ('active' if getattr(chat, 'is_call_active', False) else 'not active — start one in Telegram'))
    except Exception:
        rows.append('Voice-chat state: could not inspect')
    assigned = pool.get_assigned(message.chat.id)
    rows.append('Group assignment: ' + ('assigned' if assigned else 'none'))
    if assigned:
        try:
            active = await assigned.call_py.calls
            rows.append('Assistant call: ' + ('connected' if message.chat.id in active else 'not connected'))
        except Exception:
            rows.append('Assistant call: unavailable')
    rows.append(f'Limits: {settings.duration_limit_min} minutes/song; {settings.max_queue_tracks} tracks; {settings.max_media_mb} MB/file')
    if queues.get(message.chat.id).needs_resume:
        rows.append('Saved queue is waiting. Use /resume.')
    rows.append('Setup: add bot + assistant to the group, allow bot messages, start voice chat, then /play.')
    await message.reply_text('\n'.join(rows), parse_mode=ParseMode.DISABLED)
