"""Playback controls and stale-panel-safe callbacks."""
import asyncio
from pyrogram import Client, filters
from pyrogram.enums import ParseMode
from bot.core import calls, service, player, preferences
from bot.core.decorators import control_filter, owner_filter, can_control
from bot.core.session import operation, get_session, sessions
from bot.core.queue import queues
from bot.core.i18n import text


async def action(chat_id, name):
    if name == 'stop':
        result = await calls.stop(chat_id)
    elif name == 'skip':
        result = await calls.skip(chat_id)
    elif name == 'resume':
        result = await service.resume(chat_id)
    else:
        async with operation(chat_id):
            result = await calls.pause(chat_id)
    await player.refresh(chat_id)
    return result


@Client.on_message(filters.command(['pause', 'resume', 'skip', 'stop']) & filters.group & control_filter)
async def control_cmd(_, message):
    name = message.command[0].lower()
    try:
        result = await action(message.chat.id, name)
        pref = await preferences.get(message.chat.id)
        if name == 'stop':
            response = text('stopped', pref['language'])
        elif name == 'skip':
            response = 'Skipped.'
        else:
            response = name.capitalize() + '.' if result else text('empty', pref['language'])
        await message.reply_text(response)
    except asyncio.CancelledError:
        await message.reply_text('Request cancelled.')
    except Exception:
        await message.reply_text('Action failed. Use /doctor to check the connection.')


@Client.on_message(filters.command('seek') & filters.group & control_filter)
async def seek_cmd(_, message):
    try:
        seconds = int(message.command[1])
        if seconds < 0:
            raise ValueError
    except (ValueError, IndexError):
        await message.reply_text('Usage: /seek seconds (0 or greater).', parse_mode=ParseMode.DISABLED)
        return
    try:
        async with operation(message.chat.id):
            ok = await calls.seek(message.chat.id, seconds)
        await player.refresh(message.chat.id)
        await message.reply_text(f'Seeked to {seconds}s.' if ok else 'Cannot seek there. Check the track duration or /resume first.')
    except asyncio.CancelledError:
        await message.reply_text('Request cancelled.')


@Client.on_message(filters.command('player') & filters.group)
async def player_cmd(_, message):
    await player.refresh(message.chat.id, create=True)


@Client.on_message(filters.command('stopall') & owner_filter)
async def stopall_cmd(_, message):
    chat_ids = set(sessions) | {c for a in calls.pool.assistants for c in a.chats}
    for chat_id in chat_ids:
        await calls.stop(chat_id)
        await player.refresh(chat_id)
    await message.reply_text(f'Stopped {len(chat_ids)} chat sessions.')


@Client.on_callback_query(filters.regex(r'^vt:(pause|resume|skip|stop)$'))
async def playback_callback(client, callback):
    if callback.message is None:
        await callback.answer('Panel unavailable.', show_alert=True)
        return
    chat = callback.message.chat
    card = get_session(chat.id).card
    if card is None or card.id != callback.message.id:
        await callback.answer('This panel is old. Use /player.', show_alert=True)
        return
    if not await can_control(client, callback.from_user.id, chat):
        await callback.answer('Admins and DJs only.', show_alert=True)
        return
    await callback.answer('Working…')  # acknowledge before slow media extraction
    try:
        await action(chat.id, callback.data.split(':')[1])
    except asyncio.CancelledError:
        pass
    except Exception:
        await callback.message.reply_text('Action failed. Try /doctor.')
