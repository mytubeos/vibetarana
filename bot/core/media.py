"""Bounded media preparation. Untrusted URLs never go directly to ffmpeg."""
import asyncio
import ipaddress
import json
import math
import re
import socket
from pathlib import Path
from urllib.parse import urlsplit

import aiohttp
from aiohttp.abc import AbstractResolver

from bot.core.client import bot
from bot.core.queue import Track
from config import settings

ROOT = Path('downloads').resolve()
_LIMIT = settings.max_media_mb * 1024 * 1024
_download_slots = asyncio.Semaphore(2)


def youtube_id(link: str) -> str | None:
    parsed = urlsplit(link)
    host = (parsed.hostname or '').lower()
    if parsed.scheme not in ('http', 'https') or parsed.username or parsed.password:
        return None
    if host in ('youtu.be', 'www.youtu.be'):
        candidate = parsed.path.strip('/').split('/')[0]
    elif host in ('youtube.com', 'www.youtube.com', 'm.youtube.com', 'music.youtube.com'):
        from urllib.parse import parse_qs
        candidate = parse_qs(parsed.query).get('v', [''])[0]
        if parsed.path.startswith(('/shorts/', '/live/', '/embed/')):
            candidate = parsed.path.split('/')[2]
    else:
        return None
    return candidate if re.fullmatch(r'[A-Za-z0-9_-]{11}', candidate) else None


def validate_url(link: str) -> str:
    try:
        p = urlsplit(link)
        if p.scheme not in ('https', 'http') or not p.hostname or p.username or p.password or p.port not in (None, 80, 443):
            raise ValueError
        host = p.hostname.lower().rstrip('.')
        if host == 'localhost' or host.endswith(('.localhost', '.local', '.internal')):
            raise ValueError
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            address = None
        if address is not None and not address.is_global:
            raise ValueError
    except ValueError:
        raise ValueError('Only public HTTP(S) media URLs on standard ports are allowed.') from None
    return link


class PublicResolver(AbstractResolver):
    async def resolve(self, host, port=0, family=socket.AF_INET):
        rows = await asyncio.get_running_loop().getaddrinfo(host, port, type=socket.SOCK_STREAM, family=family)
        result = []
        for fam, _, _, _, addr in rows:
            if not ipaddress.ip_address(addr[0]).is_global:
                raise ValueError('Private/reserved destination blocked.')
            result.append({'hostname': host, 'host': addr[0], 'port': port, 'family': fam, 'proto': 0, 'flags': 0})
        if not result:
            raise ValueError('No public address found.')
        return result

    async def close(self):
        pass


def validate_track(track: Track) -> None:
    from bot.utils.formatting import parse_duration_to_seconds
    if parse_duration_to_seconds(track.duration) > settings.duration_limit_min * 60:
        raise ValueError(f'Track exceeds the {settings.duration_limit_min}-minute limit.')
    if track.link.startswith('tgfile:'):
        if not re.fullmatch(r'[A-Za-z0-9_-]{10,1024}', track.link[7:]):
            raise ValueError('Invalid Telegram file ID.')
    elif youtube_id(track.link):
        return
    else:
        validate_url(track.link)
        if not settings.allow_direct_links:
            raise ValueError('Direct links are disabled. Use a supported music link or a Telegram audio/video file.')


async def _download_http(link: str, path: Path) -> None:
    # DNS resolution is validated in the connector used for the actual request,
    # preventing check-then-resolve rebinding. Every redirect is revalidated.
    connector = aiohttp.TCPConnector(resolver=PublicResolver(), use_dns_cache=False)
    async with aiohttp.ClientSession(connector=connector, timeout=aiohttp.ClientTimeout(total=120), trust_env=False) as client:
        for _ in range(6):
            validate_url(link)
            async with client.get(link, allow_redirects=False) as response:
                if response.status in (301, 302, 303, 307, 308):
                    from urllib.parse import urljoin
                    link = urljoin(link, response.headers.get('Location', ''))
                    continue
                response.raise_for_status()
                if response.content_length and response.content_length > _LIMIT:
                    raise ValueError('Media file is too large.')
                count = 0
                with path.open('wb') as target:
                    async for chunk in response.content.iter_chunked(65536):
                        count += len(chunk)
                        if count > _LIMIT:
                            raise ValueError('Media file is too large.')
                        target.write(chunk)
                return
        raise ValueError('Too many redirects.')


async def _probe(path: Path) -> None:
    process = await asyncio.create_subprocess_exec(
        'ffprobe', '-v', 'error', '-protocol_whitelist', 'file,pipe',
        '-format_whitelist', 'mov,mp3,wav,flac,ogg,matroska,webm,aac',
        '-show_entries', 'format=duration', '-of', 'json', str(path),
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
    try:
        output, _ = await asyncio.wait_for(process.communicate(), 15)
        duration = float(json.loads(output).get('format', {}).get('duration', 0))
        if process.returncode or not math.isfinite(duration) or duration <= 0 or duration > settings.duration_limit_min * 60:
            raise ValueError('Media has no valid duration or exceeds the duration limit.')
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()


async def local_media(track: Track) -> Path:
    validate_track(track)
    ROOT.mkdir(exist_ok=True)
    # Unique downloads are caller-owned and always removed after playback.
    import uuid
    path = ROOT / f'{uuid.uuid4().hex}.media'
    async with _download_slots:
        try:
            if track.link.startswith('tgfile:'):
                async def progress(current, total):
                    if max(current, total) > _LIMIT:
                        bot.stop_transmission()
                async with asyncio.timeout(120):
                    result = await bot.download_media(track.link[7:], file_name=str(path), progress=progress)
                if result is None or not path.exists() or path.stat().st_size > _LIMIT:
                    raise ValueError('Telegram download failed or exceeded the size limit.')
            else:
                await _download_http(track.link, path)
            await _probe(path)
            return path
        except BaseException:
            path.unlink(missing_ok=True)
            Path(str(path) + '.temp').unlink(missing_ok=True)
            raise
