# Vibetarana

Telegram group voice-chat music bot using Kurigram, py-tgcalls, yt-dlp and MongoDB.

See [the final blueprint](docs/BLUEPRINT.md) for release scope, architecture,
acceptance tests and the planned community/Mini App roadmap.

## Included in R1

- Audio/video playback, force-play, play-next, pause, resume, seek and skip.
- Cancellable group operations; stop invalidates pending playback requests.
- Search with user-bound choices; reply to Telegram audio/video/voice to play.
- Saved personal/group playlists, favourites, bounded editable queue and recent history.
- Durable preferences and queue snapshots. Restarts wait for `/resume`.
- Admin/DJ controls, opt-in member requests, English/Hindi/Hinglish core messages.
- One maintained player panel, `/doctor`, readiness health endpoint and optional operator alerts.
- Expiring prefetch, shared extraction, cache-failure retry and audio-specific extraction.
- Locked dependencies and offline regression CI.

Spotify, Apple Music and SoundCloud track links use metadata lookup followed by a
YouTube match; they are not separate audio backends. External playlist links,
scheduled sessions, vote skip, advanced recommendations and Mini Apps are later phases.

## Setup

Use Linux with Python 3.12, FFmpeg and Deno (for yt-dlp's YouTube JS challenges).
Docker includes these tools. Do not use bare Windows for live voice-chat streaming.

```sh
python -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
python scripts/generate_session.py
python main.py
```

Set `API_ID`, `API_HASH`, `BOT_TOKEN`, `OWNER_ID`, `MONGO_URI` and
`ASSISTANT_SESSIONS`. Obtain your Telegram API credentials from
[my.telegram.org](https://my.telegram.org), bot token from BotFather, and generate
assistant sessions locally with the included script. Never share these values.
Optional Spotify catalog support uses `SPOTIFY_CLIENT_ID` and `SPOTIFY_CLIENT_SECRET`.

Add the bot and an assistant to a private test group, start a voice chat, run
`/doctor`, then `/play artist song`. The default is one concurrent group per assistant.
Add more assistant sessions for more group capacity; do not raise `MAX_VC_PER_ASSISTANT`.

## Commands

| Purpose | Commands |
| --- | --- |
| Discover/play | `/search artist song`, `/play query`, `/vplay query`, reply to media with `/play` |
| Player | `/player`, `/pause`, `/resume`, `/skip`, `/stop`, `/seek seconds` |
| Priority | `/playnext query`, `/playforce query`, `/vplayforce query` |
| Queue | `/queue page`, `/remove 2`, `/move 3 2`, `/shuffle`, `/clearqueue`, `/history` |
| Repeat | `/loop off\|one\|all`, `/autoplay on\|off` |
| Export/import | `/export`, reply to a queue JSON file with `/import` |
| Personal library | `/playlist save name`, `/playlist list`, `/playlist load name`, `/playlist delete name` |
| Shared library | Same actions with `/gplaylist` (writes require admin/DJ permission) |
| Favourites | `/favourite`, `/playlist load favourites` |
| Group setup | `/settings`, `/settings mode members`, `/settings language hinglish`, `/dj add user_id` |
| Operations | `/doctor`, `/ping`, owner `/stopall`, `/addsudo`, `/delsudo` |

Queue positions are 1-based: position 1 is current. `/clearqueue` keeps that track.
`/stop` clears playback but preserves preferences and named playlists. Saved queues
restore after restart in a waiting state, beginning from the current track's start
when resumed. Detailed help/diagnostic strings remain English; core messages support
English, Hindi and Hinglish.

## Limits and direct URLs

Defaults: 100 queued tracks, 60 minutes/track, 50 MB/file, two extraction slots,
three-second request cooldown. Tune the corresponding `.env.example` values.

`ALLOW_DIRECT_LINKS=false` is intentional. If enabled, arbitrary public HTTP media
files are downloaded through a bounded client with DNS/redirect protection and
probed locally. Live/HLS/M3U8 generic URLs are not supported in this release.
Telegram media is also size/duration limited. Downloads are temporary, not a library.

## Deployment and observability

- `Dockerfile` and `render.yaml` target a long-running Render background worker.
- `deploy/musicbot.service` is a VPS systemd template; replace its placeholders.
- Set `LOG_GROUP_ID` for sanitized startup/failure alerts (five-minute deduplication).
- If `PORT` is set, `/` reports readiness and returns HTTP 503 when dependencies are unhealthy.
- If YouTube needs cookies, supply `cookies/cookies.txt` or Render's
  `/etc/secrets/cookies.txt`; startup copies the secret to a writable cookie jar.
- Run only one process against an assistant pool. Multi-worker leases are a later phase.
- Follow the live acceptance checklist in the blueprint before public rollout.

## Validation

```sh
pip check
python -m compileall -q bot main.py config.py
python -m pytest -q
```

Tests use dummy credentials and mock Telegram/Mongo/network calls. A local FFmpeg
probe test validates safe media handling. Passing tests does not validate production
cookies, real voice-chat playback, host capacity or deployment health.

`requirements.in` records dependency intent; `requirements.txt` pins the tested
Python 3.12 environment. Update the lock deliberately and rerun regressions.
