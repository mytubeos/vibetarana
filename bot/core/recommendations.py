"""History-aware recommendations; language is a search preference, not detection."""
import asyncio
from bot.core import preferences
from bot.core.media import validate_track
from bot.platforms.youtube import VideosSearch
from bot.core.queue import Track

async def choose(chat_id, state, related_lookup):
    pref = await preferences.get(chat_id)
    seen = {t.link for t in state.history[-20:]} | {state.last_track.link}
    language = pref.get('music_language', 'any')
    async with asyncio.timeout(20):
        if language == 'any':
            track = await related_lookup(state.last_track.link)
            if track is None:
                return None
            # A manual request that arrived during lookup wins immediately.
            if state.current is not None:
                return None
            if track.link not in seen:
                try:
                    validate_track(track)
                    return track
                except ValueError:
                    pass
        query = f"{state.last_track.title[:120]} {language if language != 'any' else ''} songs"
        result = await VideosSearch(query, limit=10).next()
        for item in result.get('result', []):
            track = Track(str(item.get('title', 'Music'))[:250], item.get('duration') or 'Unknown',
                          f"https://www.youtube.com/watch?v={item.get('id', '')}", None, 0, 'Autoplay')
            if track.link in seen or not item.get('duration'):
                continue
            try:
                validate_track(track)
                return track
            except ValueError:
                continue
    return None
