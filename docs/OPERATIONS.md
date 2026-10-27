# Operating the expanded Vibetarana bot

## Enable and verify

Use the pinned Python dependencies, FFmpeg and Deno. Existing R1 environment values
remain valid. New functionality is additive; no collection is deleted. Back up
MongoDB before promotion. `docs/BLUEPRINT.md` retains the live Telegram checklist.

The bot is one active process per bot token/assistant pool. All replicas must use
the same Mongo database and synchronized clocks. Startup atomically acquires an
exclusive lease for the bot and configured session strings. Overlapping workers
fail startup. Renewal failure shuts down playback; an expired lease permits a
replacement. Do not share the same assistant account between different bots or
regenerate another session for the same account to evade the guard. Telegram does
not offer our application a fencing token: this is fail-closed deployment
protection, not a guarantee against indefinitely paused processes/partitions and
not active/active sharding. Schedule stop-before-start deployments.

## Group policy

- `/settings mode members` lets current group members request songs.
- `/policy fair on` interleaves upcoming requesters, keeping the current song and
  each person's request order. Admin `/playnext`, `/move`, `/shuffle` are explicit
  overrides; a later normal enqueue rebalances when fair mode is enabled.
- `/policy votes 3` requires three unique current group members to `/voteskip`;
  zero disables voting. This counts group members, not a claimed live VC audience.
  Votes clear when playback starts a new stream, including seek/restart.
- `/policy music Hindi` biases recommendation searches; `any` uses related tracks.
  History excludes the last 20 session tracks. This does not guarantee detected
  language or a perfect catalog match. Supported names are listed by `/policy`.
- `/policy video 360|480|720|1080` controls outgoing video size/fps on the next
  stream. The extraction cache may still fetch a higher-quality source; this is
  an output quality/encoding setting, not a source bandwidth guarantee.

## Playlists, scheduled sessions and radio

`/importplaylist https://www.youtube.com/playlist?list=...` imports at most the
first 50 public YouTube entries (and never exceeds queue capacity). Entries without
usable metadata, live entries and overlong songs are skipped. Private playlists,
Spotify/Apple/SoundCloud playlist pagination are not implemented; existing single
track links remain supported. Playlist extraction is one-at-a-time, time bounded,
uses a canonical YouTube URL and does not download the playlist's media.

Save `/gplaylist save evening`, then an admin can use:

```
/schedule add 2026-11-01T20:00 Asia/Kolkata evening
/schedule list
/schedule cancel job_id
```

Use an explicit IANA timezone and local ISO date/time, 30 seconds to 30 days ahead.
Ambiguous/nonexistent DST times are rejected. Each group has at most 20 pending
jobs. A job stores a playlist snapshot and rechecks the scheduling user's admin
permission at execution. It queues tracks rather than interrupting an existing
song. The group must already have a voice chat and assistant capacity available.
Jobs delayed more than five minutes are `missed`. Claimed jobs interrupted by a
crash become `interrupted` and never automatically replay: this favors avoiding
duplicate playback over guaranteed delivery. `/schedule list` shows terminal
outcomes; records expire after 60 days. Cancellation stops pending work; use
`/stop` for audio that already started. Jobs are one-shot, not recurring.

`RADIO_STATIONS` is an operator-provided JSON mapping, for example
`{"lofi":"https://www.youtube.com/watch?v=YOUR_VIDEO_ID"}` with a real 11-character
video ID. `/radio lofi` force-plays that approved YouTube live/video source using
the ordinary assistant pool and group loop policies. Each stream is capped by
`DURATION_LIMIT_MIN`; `/stop` leaves. No arbitrary HLS/M3U8/IP radio is passed to
FFmpeg. Catalog links can disappear and need a live provider compatibility test.

## Mini App and operator dashboard

Set `PORT=8080` on a VPS; Render supplies PORT. Serve this HTTP service through an
HTTPS reverse proxy. Set `WEBAPP_URL=https://your-host/app` and configure the same
URL in BotFather's menu button. `/app` in a group sends a link to the bot's private
chat, where the Web App button opens with the group's ID prefilled.

The app supports group queue viewing, play requests, personal playlist loading,
pause/resume/skip/stop, and an owner-only metrics tab. `/playlist save` still creates
playlists from Telegram. It is a remote control: audio stays in the group VC.

Every API request validates Telegram's signed `initData`, its ten-minute freshness,
and the authenticated user's live group membership. Request/control permissions
reuse the bot's policies. Actions include stop revision and track identity checks;
IDs are deduplicated within a bounded process cache. A raw group ID or browser query
parameter is never authorization. No CORS grants, cookies, local-storage credentials
or client-side bot tokens are used. Reopen the app after authentication expires.
Do not put auth headers in reverse-proxy logs. Keep TLS termination on the same
origin and block direct public access to the unencrypted backend port.

`/health` exposes readiness only. The owner tab shows bounded process metrics:
play outcomes, latest 1000 successful pipeline start durations (p50/p95), healthy
assistants, free slots and queued tracks. JSON also includes peak process RSS and
host load. Counters reset on restart. Start latency excludes message delivery and
metadata search. Failed metadata searches are not playback-attempt failures.

## Optional discovery providers

- `/mood rainy evening`: without a key, ordinary mood/language search with an honest
  fallback label. With `OPENAI_API_KEY` and `OPENAI_MODEL`, a model suggests a short
  search query; real YouTube results are offered for explicit selection. No generated
  URL is trusted and no generated title is claimed to be playable without resolution.
- `/identify` replying to an audio/voice clip up to 15 seconds and 1 MB sends that
  clip to AudD only when `AUDD_API_TOKEN` is configured. The download is validated,
  size checked and deleted afterward. The command/help state the external upload.
- `/lyrics artist title` is opt-in via `LYRICS_ENABLED=true`. It returns a matching
  LRCLIB provider link, rather than storing or reposting full lyrics.

Provider requests are time bounded and responses capped at 1 MB. Each provider has
`PROVIDER_REQUESTS_PER_HOUR=60` by default, per process, counting failed attempts.
Set spending limits at your provider too; a restart resets this process quota.
These integrations have no live credential-based acceptance in this implementation.

## Promotion and automatic startup rollback

Build and label a tested image on your Docker host, then run:

```sh
docker build -t vibetarana:YOUR_COMMIT .
python scripts/deploy.py --image vibetarana:YOUR_COMMIT --env-file /secure/vibetarana.env --port 8080
```

This script is for one dedicated bot container. It refuses to replace an unmanaged
existing container. The first migration from another deployment must be manual.
It stops and renames the old managed container, starts the candidate bound to
localhost, and requires three consecutive healthy checks within 180 seconds. On
failure it removes the candidate and restarts the previous container with its original
configuration. On success the previous container remains stopped under the printed
backup name. Do not start that backup concurrently with the new container. Monitor
and remove obsolete stopped containers/images deliberately.

The script does not provision TLS, copy cookies, publish images or manage cloud
credentials. Provide YouTube cookies separately through your hosting platform; this
minimal Docker runner does not mount them. Automatic rollback covers initial
readiness failure, not later audio-quality regressions. A previous *pre-lease* image
needs the same stop-before-start discipline. Retain a DB backup; image rollback does
not restore data. Docker execution is mocked in tests; live deployment is pending.

## Capacity planning and rollout

Hard simultaneous group capacity is the number of healthy assistant accounts (one
VC each). Actual CPU/network capacity can be lower. Increase capacity only after
measuring representative audio/video mixes on the target host:

1. Run the blueprint private-group gate with two assistants.
2. Collect at least 100 successful starts, failure counts, cold/warm start durations,
   FFmpeg process counts, CPU, peak memory and outbound bandwidth.
3. Repeat for audio and each supported video output profile; record host/image/time.
4. Run a two-hour soak, force a database outage, expired URLs and process restart.
5. Add assistants gradually while preserving resource headroom and monitor p95.

No production throughput, success SLA or “unlimited groups” claim is justified yet.
Active/active sharding would require partitioned Telegram update ownership, durable
chat routing and leases per shard; it is intentionally not enabled by this release.

## Premium service design

Keep core controls, fair access and basic recovery available to all groups. A future
paid offering can fund dedicated assistant capacity, managed hosting, longer history,
operator support and advanced customization. Capacity reservations must be enforced
server-side; monetization must not allow a client-supplied premium flag to override
permissions or limits. Before enabling billing, choose actual costs/quotas, supported
regions, cancellation/refund handling and the Telegram-compatible payment flow.
This release does not charge users, process payments or claim purchased entitlements.

## Protocol references

- Telegram Mini App validation: https://core.telegram.org/bots/webapps#validating-data-received-via-the-mini-app
- yt-dlp playlist options: https://github.com/yt-dlp/yt-dlp
- AudD recognition: https://docs.audd.io/
- OpenAI chat completions: https://developers.openai.com/api/reference/resources/chat
- LRCLIB: https://lrclib.net/docs
