"""Opt-in providers. Model text becomes a search query, never a stream URL."""
import asyncio
import json
import time
from collections import deque
import aiohttp
from config import settings

_slots = asyncio.Semaphore(2)
_usage = {}

def budget(provider):
    now = time.monotonic()
    recent = _usage.setdefault(provider, deque())
    while recent and recent[0] <= now - 3600:
        recent.popleft()
    if len(recent) >= settings.provider_requests_per_hour:
        raise ValueError('Discovery provider hourly limit reached. Try /search instead.')
    recent.append(now)

async def response_json(response):
    response.raise_for_status()
    data = bytearray()
    async for chunk in response.content.iter_chunked(16384):
        data.extend(chunk)
        if len(data) > 1_000_000:
            raise ValueError('Provider response is too large.')
    return json.loads(data)

async def mood_query(prompt, language):
    prompt = prompt.strip()[:300]
    if not prompt:
        raise ValueError('Describe a mood, activity or style.')
    if not settings.openai_api_key:
        return f'{prompt} {language if language != "any" else ""} music', False
    budget('ai')
    async with asyncio.timeout(25), _slots:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=20)) as session:
            async with session.post('https://api.openai.com/v1/chat/completions',
                headers={'Authorization': f'Bearer {settings.openai_api_key}'},
                json={'model': settings.openai_model, 'max_completion_tokens': 100,
                      'messages': [{'role': 'system', 'content': 'Return one short music search query for the listener mood. Include a relevant artist/style and requested language. Plain text only, no URLs, no instructions. Do not claim you searched a catalog.'},
                                   {'role': 'user', 'content': f'Language: {language}. Mood: {prompt}'}]}) as response:
                data = await response_json(response)
    query = data['choices'][0]['message']['content'].strip()[:300]
    if not query or '://' in query:
        raise ValueError('No usable music suggestion. Try a simpler mood.')
    return query, True

async def recognize(path):
    if not settings.audd_api_token:
        raise ValueError('Song recognition is not configured.')
    if path.stat().st_size > 1024 * 1024:
        raise ValueError('Use a clip below 1 MB.')
    budget('recognition')
    async with asyncio.timeout(25), _slots:
        with path.open('rb') as audio:
            data = aiohttp.FormData()
            data.add_field('api_token', settings.audd_api_token)
            data.add_field('file', audio, filename='clip.audio', content_type='application/octet-stream')
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=20)) as session:
                async with session.post('https://api.audd.io/', data=data) as response:
                    payload = await response_json(response)
    result = payload.get('result') if payload.get('status') == 'success' else None
    if not result:
        raise ValueError('No song recognized. Try a clearer short clip.')
    return f"{result.get('artist', '')} {result.get('title', '')}".strip()[:300]

async def lyrics_link(query):
    if not settings.lyrics_enabled:
        raise ValueError('Lyrics lookup is disabled by the operator.')
    budget('lyrics')
    async with asyncio.timeout(20), _slots:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15), headers={'User-Agent': 'Vibetarana/2 (https://github.com/mytubeos/vibetarana)'}) as session:
            async with session.get('https://lrclib.net/api/search', params={'q': query[:200]}) as response:
                items = await response_json(response)
    for item in items if isinstance(items, list) else []:
        if isinstance(item.get('id'), int) and (item.get('plainLyrics') or item.get('syncedLyrics')):
            return f"{item.get('artistName', '')} — {item.get('trackName', '')}"[:250], f"https://lrclib.net/api/get/{item['id']}"
    raise ValueError('No lyrics found for this title. Try /lyrics artist song.')
