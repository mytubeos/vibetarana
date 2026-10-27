"""Saved sessions, bounded public playlist imports and operator-selected radio."""
import asyncio
from dataclasses import asdict
from pyrogram import Client, filters
from pyrogram.enums import ParseMode
from bot.core import db, scheduler, service, player
from bot.core.decorators import admin_filter, control_filter, request_filter
from bot.core.queue import Track
from bot.core.session import get_session, operation
from bot.core.media import youtube_id
from bot.plugins.play import allow_request
from config import settings

@Client.on_message(filters.command('schedule') & filters.group & admin_filter)
async def schedule_cmd(_, message):
    args = message.command[1:]
    try:
        if not args or args[0] == 'list':
            rows = [f"{j['_id']} · {j['due'].isoformat()} UTC · {j['status']}" async for j in
                    db.db.scheduled_sessions.find({'chat_id': message.chat.id}).sort('due', -1).limit(20)]
            response = '\n'.join(rows) or 'No scheduled sessions.'
        elif args[0] == 'cancel' and len(args) == 2:
            response = 'Cancelled pending work; /stop ends any already-started audio.' if await scheduler.cancel(message.chat.id, args[1]) else 'Job not found or already finished.'
        elif args[0] == 'add' and len(args) == 4:
            when = scheduler.parse_time(args[1], args[2])
            tracks = await db.get_playlist(f'group:{message.chat.id}', args[3])
            if not tracks:
                raise ValueError('Save a group playlist with /gplaylist save name first.')
            tracks = [asdict(t) for t in service.deserialize(tracks, message.from_user)]
            async with operation(message.chat.id):
                job = await scheduler.create(message.chat.id, message.from_user.id, tracks, when, args[2])
            response = f'Scheduled {job}: {when.isoformat()} UTC. It will queue the saved tracks; admin permission is checked again at execution.'
        else:
            raise ValueError('Usage: /schedule add 2026-12-01T20:00 Asia/Kolkata playlist_name · /schedule list · /schedule cancel id')
        await message.reply_text(response, parse_mode=ParseMode.DISABLED)
    except (ValueError, OverflowError) as exc:
        await message.reply_text(str(exc), parse_mode=ParseMode.DISABLED)
    except Exception:
        await message.reply_text('Scheduling storage is unavailable. Try again.')

@Client.on_message(filters.command('importplaylist') & filters.group & request_filter)
async def import_playlist(_, message):
    if len(message.command) != 2:
        await message.reply_text('Usage: /importplaylist public_YouTube_playlist_URL (up to 50 entries).')
        return
    if not allow_request(message.chat.id, message.from_user.id):
        return
    session = get_session(message.chat.id)
    revision = session.revision
    session.pending_requests += 1
    try:
        from bot.platforms.playlist import resolve_playlist
        tracks, skipped = await resolve_playlist(message.command[1], message.from_user, min(50, settings.max_queue_tracks))
        await service.enqueue(message.chat.id, tracks, revision=revision)
        await player.refresh(message.chat.id, create=True)
        await message.reply_text(f'Queued {len(tracks)} tracks; skipped {skipped} unavailable/unsupported entries. Import is limited to the first 50 entries.')
    except asyncio.CancelledError:
        await message.reply_text('Import cancelled.')
    except ValueError as exc:
        await message.reply_text(str(exc), parse_mode=ParseMode.DISABLED)
    except Exception:
        await message.reply_text('Playlist lookup failed. Public YouTube playlists are supported; try again later.')
    finally:
        session.pending_requests -= 1

@Client.on_message(filters.command('radio') & filters.group & control_filter)
async def radio_cmd(_, message):
    names = sorted(settings.radio_stations)
    if len(message.command) != 2 or message.command[1] not in settings.radio_stations:
        await message.reply_text('Usage: /radio station. Configured stations: ' + (', '.join(names) or 'none') + '. Streams are limited to the configured song-duration cap; /stop exits.', parse_mode=ParseMode.DISABLED)
        return
    name = message.command[1]
    link = settings.radio_stations[name]
    if not youtube_id(link):
        await message.reply_text('Station must be an operator-configured YouTube video/live URL.')
        return
    try:
        await service.enqueue(message.chat.id, [Track(name, 'Unknown', link, None, message.from_user.id, message.from_user.first_name or 'listener', 'Radio')], force=True)
        await player.refresh(message.chat.id, create=True)
    except asyncio.CancelledError:
        pass
    except Exception:
        await message.reply_text('Radio could not start. Check /doctor.')
