"""Bounded queues with numbered pagination and transactional editing."""
import asyncio
import json
from io import BytesIO
from pyrogram import Client, filters
from pyrogram.enums import ParseMode
from bot.core import calls, preferences, service, player
from bot.core.decorators import control_filter
from bot.core.queue import queues
from bot.core.session import operation, get_session

MAX_IMPORT_FILE_BYTES = 512 * 1024


@Client.on_message(filters.command('queue') & filters.group)
async def queue_cmd(_, message):
    state = queues.get(message.chat.id)
    try:
        page = int(message.command[1]) if len(message.command) > 1 else 1
        if page < 1:
            raise ValueError
    except ValueError:
        await message.reply_text('Usage: /queue page (1 or greater)')
        return
    if not state.queue:
        await message.reply_text('Queue is empty.')
        return
    pages = (len(state.queue) + 9) // 10
    if page > pages:
        await message.reply_text(f'Choose a page from 1 to {pages}.')
        return
    start = (page - 1) * 10
    lines = [f'Vibetarana queue · {page}/{pages}', 'Position 1 is the current track.']
    for i, track in enumerate(state.queue[start:start + 10], start + 1):
        lines.append(f'{i}. {track.title[:150]} · {track.duration[:20]} · {track.requested_by_name[:50]}')
    await message.reply_text('\n'.join(lines), parse_mode=ParseMode.DISABLED)


@Client.on_message(filters.command(['loop', 'autoplay']) & filters.group & control_filter)
async def settings_cmd(_, message):
    name = message.command[0].lower()
    allowed = ('off', 'one', 'all') if name == 'loop' else ('on', 'off')
    if len(message.command) != 2 or message.command[1].lower() not in allowed:
        await message.reply_text(f'Usage: /{name} ' + '|'.join(allowed), parse_mode=ParseMode.DISABLED)
        return
    value = message.command[1].lower()
    async with operation(message.chat.id):
        await preferences.set(message.chat.id, **({'loop_mode': value} if name == 'loop' else {'autoplay': value == 'on'}))
    await player.refresh(message.chat.id)
    await message.reply_text(f'{name}: {value} (saved)')


@Client.on_message(filters.command(['shuffle', 'remove', 'move', 'clearqueue']) & filters.group & control_filter)
async def edit_queue(_, message):
    chat_id = message.chat.id
    try:
        async with operation(chat_id):
            name = message.command[0].lower()
            if name == 'remove':
                queues.remove(chat_id, int(message.command[1]))
            elif name == 'move':
                queues.move(chat_id, int(message.command[1]), int(message.command[2]))
            elif name == 'shuffle':
                if not queues.shuffle(chat_id):
                    raise ValueError('Add at least two upcoming songs to shuffle.')
            else:
                queues.get(chat_id).queue[1:] = []
            calls.schedule_prefetch(chat_id)
            await calls.persist_queue(chat_id)
        await player.refresh(chat_id)
        await message.reply_text('Queue updated.')
    except (ValueError, IndexError) as exc:
        await message.reply_text(str(exc) if isinstance(exc, ValueError) else 'Usage: /remove 2 or /move 3 2. Position 1 is the current track.', parse_mode=ParseMode.DISABLED)
    except asyncio.CancelledError:
        await message.reply_text('Queue edit cancelled.')


@Client.on_message(filters.command('history') & filters.group)
async def history_cmd(_, message):
    history = queues.get(message.chat.id).history[-10:]
    await message.reply_text('\n'.join(t.title[:150] for t in reversed(history)) or 'No recent tracks in this session.', parse_mode=ParseMode.DISABLED)


@Client.on_message(filters.command('export') & filters.group)
async def export_cmd(_, message):
    file = BytesIO(json.dumps(queues.export(message.chat.id)).encode())
    file.name = 'vibetarana-queue.json'
    await message.reply_document(file, caption='Queue export. Reply with /import to restore.')


@Client.on_message(filters.command('import') & filters.group & control_filter)
async def import_cmd(client, message):
    reply = message.reply_to_message
    doc = reply.document if reply else None
    if not doc or not doc.file_name or not doc.file_name.endswith('.json') or not doc.file_size or doc.file_size > MAX_IMPORT_FILE_BYTES:
        await message.reply_text('Reply to a JSON queue file, maximum 512 KB.')
        return
    revision = get_session(message.chat.id).revision
    try:
        async def progress(current, total):
            if max(current, total) > MAX_IMPORT_FILE_BYTES:
                client.stop_transmission()
        async with asyncio.timeout(30):
            buffer = await client.download_media(reply, in_memory=True, progress=progress)
        if buffer is None or len(buffer.getvalue()) > MAX_IMPORT_FILE_BYTES:
            raise ValueError('File exceeded the size limit.')
        payload = json.loads(buffer.getvalue())
        if not isinstance(payload, dict):
            raise ValueError('Invalid queue file.')
        tracks = service.deserialize(payload.get('tracks'), message.from_user)
        await service.enqueue(message.chat.id, tracks, revision=revision)
        await message.reply_text(f'Imported {len(tracks)} tracks.')
        await player.refresh(message.chat.id, create=True)
    except asyncio.CancelledError:
        await message.reply_text('Import cancelled.')
    except Exception as exc:
        await message.reply_text(f'Import failed: {str(exc)[:180]}', parse_mode=ParseMode.DISABLED)
