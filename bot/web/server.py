"""All API reads/actions validate Telegram identity and live group membership."""
import asyncio
import time
from collections import OrderedDict
from dataclasses import asdict
from pathlib import Path
from types import SimpleNamespace
from aiohttp import web
from pyrogram.enums import ChatType
from bot.web.auth import validate_init_data
from bot.core.client import bot
from bot.core import service, preferences, player, db
from bot.core.queue import queues
from bot.core.session import get_session
from bot.core.decorators import can_request, can_control
from bot.core.metrics import metrics
from bot.core.assistants import pool
from bot.plugins.community import member
from bot.plugins.play import allow_request
from config import settings

ROOT = Path(__file__).parent
_seen = OrderedDict()
_rates = OrderedDict()


def rate_limit(user_id):
    now = time.monotonic()
    count, until = _rates.get(user_id, (0, now + 60))
    if now >= until:
        count, until = 0, now + 60
    if count >= 60:
        raise web.HTTPTooManyRequests(text='Too many requests. Wait a minute.')
    _rates[user_id] = (count + 1, until)
    _rates.move_to_end(user_id)
    while len(_rates) > 5000:
        _rates.popitem(last=False)


@web.middleware
async def security(request, handler):
    try:
        response = await handler(request)
    except web.HTTPException as exc:
        response = web.json_response({'error': exc.text}, status=exc.status)
    except (ValueError, KeyError, TypeError):
        response = web.json_response({'error': 'Invalid request or unavailable track.'}, status=400)
    except asyncio.CancelledError:
        raise
    except Exception:
        from bot.utils.logger import get_logger
        get_logger(__name__).exception('Web request failed')
        response = web.json_response({'error': 'Service temporarily unavailable.'}, status=503)
    response.headers.update({'Cache-Control': 'no-store', 'X-Content-Type-Options': 'nosniff',
        'Referrer-Policy': 'no-referrer',
        'Content-Security-Policy': "default-src 'self'; script-src 'self' https://telegram.org; style-src 'self'; connect-src 'self'; img-src 'self' data:; base-uri 'none'; form-action 'self'"})
    return response


async def api(request):
    try:
        user = validate_init_data(request.headers.get('X-Telegram-Init-Data', ''), settings.bot_token)
    except ValueError as exc:
        raise web.HTTPUnauthorized(text=str(exc)) from None
    rate_limit(user['id'])
    payload = await request.json()
    if not isinstance(payload, dict):
        raise web.HTTPBadRequest(text='Expected a JSON object.')
    action = payload.get('action', 'state')
    if action == 'ops':
        if user['id'] != settings.owner_id:
            raise web.HTTPForbidden(text='Owner only.')
        return web.json_response({**metrics.snapshot(), 'assistants': len(pool.assistants),
            'healthy': sum(a.healthy for a in pool.assistants), 'active_groups': sum(len(a.chats) for a in pool.assistants),
            'free_slots': sum(a.healthy and not a.chats for a in pool.assistants),
            'queue_tracks': sum(len(s.queue) for s in queues._chats.values())})
    chat_id = int(payload['chat_id'])
    chat = await bot.get_chat(chat_id)
    if chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP) or not await member(bot, chat_id, user['id']):
        raise web.HTTPForbidden(text='Current group membership is required.')
    if action == 'state':
        state = queues.get(chat_id)
        return web.json_response({'chat': chat.title, 'paused': state.is_paused, 'needs_resume': state.needs_resume,
            'playback_id': state.playback_id, 'revision': get_session(chat_id).revision,
            'queue': [{'title': t.title, 'duration': t.duration, 'requester': t.requested_by_name} for t in state.queue],
            'can_control': await can_control(bot, user['id'], chat),
            'can_request': await can_request(bot, user['id'], chat),
            'playlists': await db.list_playlists(f"user:{user['id']}")})
    if action in ('pause', 'resume', 'skip', 'stop'):
        if not await can_control(bot, user['id'], chat):
            raise web.HTTPForbidden(text='Admins and DJs only.')
    elif action in ('play', 'load'):
        if not await can_request(bot, user['id'], chat):
            raise web.HTTPForbidden(text='Music requests are restricted.')
    else:
        raise web.HTTPBadRequest(text='Unknown action.')
    if not allow_request(chat_id, user['id']):
        raise web.HTTPTooManyRequests(text='Please wait a few seconds.')
    request_id = payload.get('request_id', '')
    if not isinstance(request_id, str) or not 16 <= len(request_id) <= 64:
        raise web.HTTPBadRequest(text='A unique request_id is required.')
    key = (user['id'], request_id)
    if key in _seen:
        raise web.HTTPConflict(text='Request already processed; refresh the player.')
    _seen[key] = time.monotonic()
    while len(_seen) > 5000:
        _seen.popitem(last=False)
    session = get_session(chat_id)
    revision = session.revision
    if payload.get('revision') != revision:
        raise web.HTTPConflict(text='Player changed. Refresh before acting.')
    if action in ('pause', 'resume', 'skip', 'stop'):
        from bot.plugins.controls import action as control
        # Skip/pause cannot act on a different song than the rendered one.
        if action != 'stop' and payload.get('playback_id') != queues.get(chat_id).playback_id:
            raise web.HTTPConflict(text='Track changed. Refresh the player.')
        await control(chat_id, action, expected_playback_id=payload.get('playback_id'))
    else:
        session.pending_requests += 1
        try:
            if action == 'play':
                from bot.platforms import resolve
                query = payload.get('query')
                if not isinstance(query, str) or not 1 <= len(query.strip()) <= 500:
                    raise ValueError('Invalid query')
                async with asyncio.timeout(30):
                    track = await resolve(query, user['id'], str(user.get('first_name', 'listener'))[:100])
                tracks = [track] if track else []
            else:
                name = payload.get('name')
                if not isinstance(name, str) or len(name) > 32:
                    raise ValueError('Invalid playlist')
                saved = await db.get_playlist(f"user:{user['id']}", name)
                tracks = service.deserialize(saved or [], SimpleNamespace(id=user['id'], first_name=user.get('first_name', 'listener')))
            await service.enqueue(chat_id, tracks, revision=revision)
            await player.refresh(chat_id, create=True)
        except asyncio.CancelledError:
            raise web.HTTPConflict(text='Playback request cancelled.') from None
        finally:
            session.pending_requests -= 1
    return web.json_response({'ok': True})


def attach(app):
    app.middlewares.append(security)
    async def index(_):
        return web.FileResponse(ROOT / 'index.html')
    app.router.add_get('/app', index)
    for filename in ('app.js', 'style.css'):
        async def static(_, name=filename):
            return web.FileResponse(ROOT / name)
        app.router.add_get('/app/' + filename, static)
    app.router.add_post('/api', api)
