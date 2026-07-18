# TG2 Music Bot

A multi-group Telegram voice-chat music bot — built as a from-scratch "best of the
top 5" take on the genre (feature ideas pulled from YukkiMusicBot, AnonXMusic,
AsmSafone/MusicPlayer, RadioPlayerV3, and Zaid-Vc-Player), on the same
Pyrogram + py-tgcalls assistant-pool architecture as the sibling `TG` project.
Independent deployment — own bot token, own assistant accounts, own Mongo
cluster.

## Status

**Built so far (Phase 1 — MVP):**
`/start` `/help` `/ping` `/play <query or link>` `/pause` `/resume`
`/skip` `/stop` `/seek <seconds>`, a formalized multi-source platform
registry (see Phase 2a below), in-memory per-chat queue with
`/queue` `/loop <off|one|all>` `/shuffle` `/export` `/import`, assistant pool
with least-loaded assignment, auto-leave on empty queue, health-check/
reconnect handling — and, unlike `TG`, MongoDB is actually connected and used
(`sudo_users` allow-list managed via `/addsudo`/`/delsudo`, a `chat_settings`
scaffold for later phases).

**Built so far (Phase 2a — multi-source):**
`/play` now also accepts **Spotify**, **Apple Music**, and **SoundCloud**
links, plugged into the Phase 1 platform registry (`bot/plugins/play.py`
itself didn't change). None of those three services let a third-party bot
pull raw audio via API, so each resolver looks up public track metadata
(title/artist/duration/art) and then searches YouTube for a matching stream
— same pattern YukkiMusicBot/AnonXMusic use, same category of legal-gray-area
risk already noted below, not a new one:
- **Spotify** (`bot/platforms/spotify.py`) — Spotify Web API, Client
  Credentials flow. Requires `SPOTIFY_CLIENT_ID`/`SPOTIFY_CLIENT_SECRET` (see
  Setup below); without them, Spotify links just don't resolve — everything
  else keeps working.
- **Apple Music** (`bot/platforms/apple_music.py`) — Apple's public iTunes
  Lookup API, no credentials needed. Only resolves links to a specific track
  (a `?i=` query param); an album/playlist-only link won't match.
- **SoundCloud** (`bot/platforms/soundcloud.py`) — SoundCloud's public oEmbed
  endpoint, no credentials needed. Doesn't attempt yt-dlp's direct SoundCloud
  extractor — that had open client_id/format-lookup reliability issues as of
  mid-2026, so this goes straight to the metadata+YouTube-fallback path for
  consistency.

**Built so far (Phase 2b-i — video chat + direct links):**
- **`/vplay <query or link>`** — same resolution as `/play`, but streams
  video too (`bot/core/calls.py` switches `MediaStream`'s `video_flags` from
  `IGNORE` to `AUTO_DETECT` per-track — a queue can freely mix `/play` and
  `/vplay` tracks). `/seek` preserves whichever mode the current track used.
- **Direct links** (`bot/platforms/direct_link.py`) — any raw `http(s)` URL
  not claimed by YouTube/Spotify/Apple Music/SoundCloud (e.g. a direct
  `.mp3`/`.mp4` link) streams straight through, no metadata lookup possible
  so the filename is used as the title. Registered last in the resolver
  order on purpose — see the module docstring.

**Built so far (Phase 2b-ii — autoplay):**
- **`/autoplay <on|off>`** (per-chat, off by default) — when the queue empties
  and this is on, `bot/core/calls.py` looks up a YouTube-related video to the
  last track played (`bot/platforms/youtube.py`'s `get_related()`, via
  `py_yt`'s `Recommendations.getRelated()`) and keeps playing instead of
  starting the auto-leave timer, announcing what it picked in the chat. Only
  seeds from YouTube-sourced links today (Spotify/Apple Music/SoundCloud
  tracks resolve *to* a YouTube link already, so this still works for them
  too — it just doesn't know their original source's own "radio" feature).

**Not built yet (ask to add when ready):**
- **Phase 2b-iii** — a Telegram-uploaded audio/video file replied to with
  `/play` (needs a download-then-stream step, unlike everything above which
  streams from a URL directly), lyrics lookup, custom now-playing
  thumbnails, gapless/pre-buffer polish, full restart-persistence of active
  queue state, an actually-wired-up `LOG_GROUP_ID` (currently a dead config
  field, same as it was in `TG`).
- **Phase 3** — light moderation (anti-spam/anti-raid), multi-language,
  stats, blacklist, broadcast.

## Setup

1. **Get credentials** (see `.env.example` for the full list):
   - `API_ID` / `API_HASH` from [my.telegram.org](https://my.telegram.org)
   - `BOT_TOKEN` from [@BotFather](https://t.me/BotFather) (`/newbot`)
   - `OWNER_ID` — your numeric Telegram user ID (ask [@userinfobot](https://t.me/userinfobot))
   - `MONGO_URI` — free cluster at [MongoDB Atlas](https://cloud.mongodb.com) (**actually used** here — sudo users and per-chat settings persist to it)
2. Copy `.env.example` to `.env` and fill in the four values above (leave `ASSISTANT_SESSIONS` blank for now).
3. Install dependencies: `pip install -r requirements.txt`
4. Install ffmpeg (Linux/VPS): `sudo apt install ffmpeg -y`
5. Generate at least one assistant session (use a spare phone number, not your personal one):
   ```
   python scripts/generate_session.py
   ```
   Paste the printed string into `ASSISTANT_SESSIONS` in `.env`. Add more comma-separated sessions later to raise how many groups can play concurrently (`max concurrent groups = assistants × MAX_VC_PER_ASSISTANT`).
6. Add the bot **and** every assistant account to your test group. Have a human start the group's voice chat first — a plain member-level assistant can join an existing voice chat but typically can't create one from scratch.
7. Message the bot as the owner and run `/addsudo <user_id>` to promote any additional trusted users (beyond `OWNER_ID`) to sudo — persisted in Mongo, not an env var.
8. **Optional** — for Spotify link support, create a free app at the [Spotify Developer Dashboard](https://developer.spotify.com/dashboard) and put its Client ID/Secret in `SPOTIFY_CLIENT_ID`/`SPOTIFY_CLIENT_SECRET`. Apple Music and SoundCloud links work with no extra setup.

## Running

```
python main.py
```

A misconfigured `.env` fails immediately with a list of every missing/invalid variable — fix those and rerun.

**Real voice-chat audio needs Linux** (the native call-streaming engine doesn't run on bare Windows). Develop on Windows if you like, but do real playback testing on the VPS or under WSL2.

### Tests

```
python -m pytest
```

Covers the pure-logic pieces (queue loop/shuffle/export/import edge cases,
platform-registry dispatch ordering, the autoplay race guard) — no real
`.env`, Mongo, or network connection needed (`tests/conftest.py` sets
harmless dummy config values, since a couple of modules need *some* config
present at import time even for their pure logic). Anything that streams
into a live voice chat still needs a real run on Linux/WSL2 (see above).

### Deploying on a VPS (systemd)

```
cp deploy/musicbot.service /etc/systemd/system/musicbot.service
# edit the REPLACE_WITH_YOUR_USERNAME placeholders in that file first
sudo systemctl daemon-reload
sudo systemctl enable --now musicbot
journalctl -u musicbot -f   # view logs
```

## Known operational risks

- **YouTube cookies**: YouTube throttles datacenter IPs, which is what every VPS is. If playback works locally but fails on the VPS, this is the most likely cause — export a `cookies.txt` from a logged-in browser session into `cookies/`.
- **Assistant accounts**: prefer an aged number over a brand-new SIM; don't add many assistants to freshly-created accounts at once, or Telegram's abuse systems may flag them.
- **Legal/ToS**: streaming YouTube-sourced audio via a public multi-group bot sits in a legal gray area in most jurisdictions — a common category of open-source project (all 5 bots this project drew ideas from do the same), but worth being aware of at scale. Phase 2's Spotify/Apple Music/SoundCloud "support" will be metadata-lookup-then-YouTube-stream, not native third-party streaming — same category of risk, not a new one.

## Project layout

```
main.py              entrypoint
config.py             env loading/validation
bot/core/             client bootstrap, assistant pool, Mongo wiring, call wiring, queue, admin filter
bot/platforms/        source resolver registry + resolvers (youtube.py so far)
bot/plugins/          command handlers
bot/utils/             logging
tests/                 pytest — pure-logic coverage (queue, platform dispatcher)
scripts/               generate_session.py — assistant session string generator
deploy/                systemd unit template
```
