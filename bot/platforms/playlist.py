"""Bounded YouTube playlist metadata import through yt-dlp, without stream downloads."""
import asyncio
import json
import re
import sys
from urllib.parse import urlsplit, parse_qs
from bot.core.queue import Track
from bot.core.media import validate_track, youtube_id
from bot.utils.formatting import format_seconds

_slots = asyncio.Semaphore(1)


def canonical_playlist(url):
    p = urlsplit(url)
    value = parse_qs(p.query).get('list', [''])[0]
    if p.scheme != 'https' or p.hostname not in ('youtube.com', 'www.youtube.com', 'music.youtube.com') or p.username or p.password or p.port not in (None, 443) or not re.fullmatch(r'[A-Za-z0-9_-]{10,100}', value):
        raise ValueError('Use a public YouTube playlist URL.')
    return f'https://www.youtube.com/playlist?list={value}'


async def resolve_playlist(url, user, limit=50):
    url = canonical_playlist(url)
    limit = max(1, min(limit, 50))
    async with asyncio.timeout(60), _slots:
        process = await asyncio.create_subprocess_exec(sys.executable, '-m', 'yt_dlp',
            '--ignore-config', '--flat-playlist', '--skip-download', '--dump-json',
            '--playlist-end', str(limit), '--socket-timeout', '10', '--retries', '1', url,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
        tracks, skipped = [], 0
        try:
            # Stream bounded lines instead of buffering arbitrary playlist JSON.
            total = 0
            while line := await process.stdout.readline():
                total += len(line)
                if total > 2_000_000:
                    raise ValueError('Playlist metadata is too large.')
                item = json.loads(line)
                link = f"https://www.youtube.com/watch?v={item.get('id', '')}"
                if not youtube_id(link) or item.get('is_live') or not isinstance(item.get('duration'), (int, float)):
                    skipped += 1
                    continue
                track = Track(str(item.get('title', 'Music'))[:250], format_seconds(int(item['duration'])), link, None, user.id, user.first_name or 'listener')
                try:
                    validate_track(track)
                    tracks.append(track)
                except ValueError:
                    skipped += 1
                if len(tracks) + skipped >= limit:
                    break
            if not tracks:
                raise ValueError('No playable tracks found in the playlist.')
            return tracks, skipped
        finally:
            if process.returncode is None:
                process.kill()
            await process.wait()
