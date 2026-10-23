"""Personal/group playlists; favourites use the same scoped durable store."""
import asyncio
import re
from dataclasses import asdict
from pyrogram import Client, filters
from pyrogram.enums import ParseMode
from bot.core import db, service, player
from bot.core.queue import queues
from bot.core.decorators import can_control, can_request
from bot.core.session import get_session
from bot.plugins.play import allow_request


@Client.on_message(filters.command(['playlist', 'gplaylist', 'favourite']) & filters.group)
async def playlist_cmd(client, message):
    user = message.from_user
    if user is None:
        return
    command = message.command[0].lower()
    group = command == 'gplaylist'
    scope = f'group:{message.chat.id}' if group else f'user:{user.id}'
    args = message.command[1:]
    if command == 'favourite':
        args = ['favourite', 'favourites']
    action = args[0].lower() if args else 'list'
    name = args[1].lower() if len(args) > 1 else ''
    if action not in ('list', 'save', 'load', 'delete', 'favourite'):
        await message.reply_text('Usage: /playlist list|save name|load name|delete name. /gplaylist for group playlists; /favourite saves the current song.')
        return
    if action != 'list' and not re.fullmatch(r'[a-z0-9_-]{1,32}', name):
        await message.reply_text('Use a playlist name with 1–32 letters, numbers, _ or -.')
        return
    if group and action in ('save', 'delete') and not await can_control(client, user.id, message.chat):
        await message.reply_text('Only admins/DJs may edit group playlists.')
        return
    if action == 'load' and not await can_request(client, user.id, message.chat):
        await message.reply_text('Music requests are restricted in this group.')
        return
    if not allow_request(message.chat.id, user.id):
        await message.reply_text('Please wait a few seconds before another request.')
        return
    revision = get_session(message.chat.id).revision
    try:
        if action == 'list':
            names = await db.list_playlists(scope)
            result = '\n'.join(names) or 'No playlists. Use /playlist save name while songs are queued.'
        elif action == 'save':
            tracks = [asdict(t) for t in queues.get(message.chat.id).queue]
            if not tracks:
                raise ValueError('Queue is empty.')
            await db.save_playlist(scope, name, tracks)
            result = f'Saved {len(tracks)} tracks as {name}.'
        elif action == 'delete':
            result = 'Deleted.' if await db.delete_playlist(scope, name) else 'Playlist not found.'
        elif action == 'favourite':
            current = queues.get(message.chat.id).current
            if current is None:
                raise ValueError('Nothing is playing.')
            tracks = await db.get_playlist(scope, name) or []
            if not any(t['link'] == current.link for t in tracks):
                if len(tracks) >= 100:
                    raise ValueError('Favourites limit: 100 songs.')
                tracks.append(asdict(current))
                await db.save_playlist(scope, name, tracks)
            result = 'Added to favourites. Use /playlist load favourites.'
        else:
            payload = await db.get_playlist(scope, name)
            if not payload:
                raise ValueError('Playlist not found or empty.')
            tracks = service.deserialize(payload, user)
            await service.enqueue(message.chat.id, tracks, revision=revision)
            await player.refresh(message.chat.id, create=True)
            result = f'Loaded {len(tracks)} tracks.'
        await message.reply_text(result, parse_mode=ParseMode.DISABLED)
    except asyncio.CancelledError:
        await message.reply_text('Playlist request cancelled.')
    except ValueError as exc:
        await message.reply_text(str(exc), parse_mode=ParseMode.DISABLED)
    except Exception:
        await message.reply_text('Playlist storage is unavailable. Please try again.')
