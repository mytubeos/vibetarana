# Vibetarana implementation blueprint

Decision date: 24 October 2026. Expanded implementation: 27 October 2026. Baseline: `5e7658a`.

## Product goal

Dependable group listening: correct songs, predictable controls, fair permissions,
saved music and understandable recovery. Optimize actual play success and user
experience before adding AI or rebuilding the stack.

## Release boundaries

| Release | Scope | Status |
| --- | --- | --- |
| R1 (this implementation) | Stable playback coordination, limits, durable settings/queue, safe file playback, search choices, playlists/favourites, queue tools, group roles, player panel, diagnostics, core localization, CI | Implemented; offline regression gate required, live acceptance pending |
| R2 | Vote skip, fair queue, history/language recommendations, YouTube playlist import, one-shot schedules, configured YouTube radio, video output quality | Implemented with bounded provider scope; live acceptance pending |
| R3 | Operator dashboard, process metrics, exclusive worker leases, capacity planning, startup rollback | Code and runbook implemented; production measurement and active/active sharding pending |
| R4 | Telegram Mini App, AI mood requests, optional recognition/lyrics providers, premium service design | Code and design implemented; credentials/hosting/provider acceptance pending, no billing |

## R1 architecture

- `session.py`: one operation lock per chat, stop revision and cancellable active task.
  A request started before stop must not play after stop. Separate groups proceed independently.
- `service.py`: validate and transact queue changes; restore a previous queue when
  force-play fails; recover the previous stream or leave and mark it for manual resume.
- `calls.py`: bounded extraction, shared in-flight lookups, separate audio/video
  formats, timestamped prefetch, fresh retry on invalid cached URLs, stream lifecycle.
- `preferences.py`: durable settings independent of transient playback state.
- `db.py`: sudo users, settings, named playlists, queue snapshots, URL cache, play counts.
- `media.py`: Telegram file IDs and safe, opt-in public HTTP media downloads.
- `player.py`: one editable text control panel, refreshed on actions and every
  30 seconds during playback; stale control panels cannot change playback.
- Plugins: play/search, controls, queue, scoped library, group settings and diagnostics.

### Command decisions

| Area | Commands / behaviour |
| --- | --- |
| Play | `/play`, `/vplay`, reply-to-audio/video/voice; `/search` returns up to five choices |
| Priority | `/playnext`, `/playforce`, `/vplayforce` for admins/DJs |
| Control | `/player`, `/pause`, `/resume`, `/skip`, `/stop`, `/seek` |
| Queue | `/queue page`, `/remove position`, `/move from to`, `/shuffle`, `/clearqueue`, `/history` |
| Persistence | `/export`, `/import`; active queue snapshots restore without auto-start |
| Library | `/playlist list/save/load/delete`, `/gplaylist`, `/favourite` |
| Group policy | `/settings mode admins/members`, `/settings language en/hi/hinglish`, `/dj add/remove user_id` |
| Operations | `/doctor`, `/ping`, owner `/stopall`, `/addsudo`, `/delsudo` |

Positions are 1-based; position 1 is current and cannot be removed/moved by queue edits.
`/clearqueue` keeps the current track. Search choices expire after two minutes and
are bound to the requesting user, chat and stop revision.

### Permissions and storage

- Default: admins and designated DJs can request and control playback.
- Member mode allows group members to request songs, not stop/skip/force other users' music.
- Only admins change member mode, language and the DJ list.
- Personal playlists are scoped to Telegram user ID; group playlists to chat ID.
  Group playlist writes require control permission. Imports never trust embedded requester IDs.
- Up to 30 named playlists per scope, 100 favourites, default queue limit 100 tracks.
- MongoDB snapshots are best effort when the database is unavailable; failures are logged.
  This is not a claim of zero data loss under database outages.
- Restart recovery restores track IDs and queue order, then waits for `/resume`.
  Playback starts the current track from the beginning; exact position recovery is deferred.
- Queue history is session-local, bounded to 20 entries; `/history` displays the last 10.

### Media and capacity defaults

- One simultaneous group per assistant account; more groups require more healthy assistants.
- 60-minute track limit, 50 MB media limit, two extractor slots, three-second request cooldown.
- Enforce known duration at admission and cap ffmpeg playback duration independently.
- Uploaded files are downloaded only when needed, probed and removed on replacement/stop.
- Generic direct URLs are disabled by default. Opt-in supports bounded media files only.
  DNS validation is performed by the actual HTTP connector; redirects are validated again.
  Private/reserved addresses, credentials in URLs and nonstandard ports are rejected.
- Untrusted downloads use restricted local ffmpeg protocols and media formats.
  HLS/M3U8/live radio through generic direct links is deferred until a safe streaming design exists.
- Spotify/Apple Music/SoundCloud resolve catalog metadata to YouTube matches; they are
  not independent audio backends. R1 did not support playlist URLs; the expanded release imports public YouTube playlists.
- Core onboarding, play-status and player messages have English/Hindi/Hinglish text.
  Detailed command help, diagnostic and validation strings remain English in R1.
- Video uses the existing stream defaults; R2 now provides user-adjustable output quality through /policy video.

## Acceptance gates

### Automated (no credentials)

1. Stop during extraction cancels the active operation and no later play starts.
2. Requests waiting behind a stopped operation and stale search results are invalidated.
3. Autoplay cannot replace a manual queue item or pending manual play request.
4. Failed force-play preserves the previous queue and restores/releases the assistant.
5. Skip at queue end immediately silences the current stream.
6. Preferences survive clear/reload; queue recovery never initiates a call automatically.
7. Private-IP URLs, host spoofing, DNS-private answers and oversized media are rejected.
8. Expired prefetch and bad cached URLs trigger fresh extraction; concurrent identical
   requests share extraction without one cancelled waiter killing another's request.
9. Members cannot control playback, removed DJs cannot use callbacks, stale buttons
   and another user's search choices are rejected.
10. Personal playlist scope and group-edit permissions are enforced.
11. Caption transitions, invalid seek input, queue edits and media-probe restrictions work.
12. Python compile, dependency consistency and all existing tests pass in CI.

### Live acceptance (pending; requires deployment and Telegram credentials)

Use a private test group and at least two assistants before broad rollout:

- Cold/warm YouTube song, supported catalog link and Telegram audio/video.
- Pause/resume/seek/skip; rapid stop while loading; force an invalid track.
- Two groups simultaneously; fill assistant capacity and check the busy response.
- Natural audio/video end advances exactly once; empty queues leave after the grace period.
- Revoke an assistant; drop database/network; inspect `/doctor` and optional log alerts.
- Restart with queued tracks; verify saved settings, playlists and explicit resume.
- Try member/DJ/admin permissions from different accounts and languages.
- Observe a two-hour session, CPU/RAM, FFmpeg processes, temporary-file cleanup and FloodWait.

Record play success rate, p50/p95 request-to-audio latency, skip latency, wrong-result
rate and recovery time. These metrics have no measured production baseline yet.
Do not advertise instant playback, zero downtime or unlimited concurrent groups.

## Deployment and rollback

R1 adds collections and an index without deleting existing data. Keep a database
backup and the previous deployment image. Build against pinned `requirements.txt`.
Deploy to the test instance, execute the live gate, then promote the same image.
Rollback means restoring the previous image/commit; new collections can remain.
Never overwrite tokens, cookies or assistant sessions from version control.
GitHub commits are source delivery; they are not proof of successful deployment.

## Expanded implementation map

The user authorized the remaining blueprint implementation on 27 October 2026.
Detailed configuration, semantics and limits are in [OPERATIONS.md](OPERATIONS.md).

- `community.py` / `queue.py`: round-robin requests, unique-member votes, group policies.
- `recommendations.py`: skip session history and bias real searches by music language.
- `platforms/playlist.py`: canonical public YouTube playlist metadata, max 50 entries.
- `scheduler.py` / `plugins/sessions.py`: durable single-fire snapshots, timezone validation,
  status/cancellation, admin recheck, missed/interrupted outcomes and approved radio.
- `lease.py`: exclusive deployment ownership, fail-closed renewal, guarded mutations.
- `metrics.py`: bounded real playback observations, resource/capacity snapshots.
- `web/`: signed Telegram authentication, live membership checks, protected controls,
  private playlist loading, responsive listening UI and owner dashboard.
- `discovery.py`: optional OpenAI mood queries, AudD clips and LRCLIB links, bounded
  responses/time/concurrency and per-provider hourly limits.
- `scripts/deploy.py`: managed single-host promotion and failed-readiness rollback.
- CI: Python regressions, real Mongo lease/job integration and browser smoke tests.

### Remaining acceptance and scope limits

This does not mark the live acceptance gates complete. HTTPS deployment/BotFather,
provider credentials, real Telegram streaming, radio-provider compatibility,
Docker-host promotion and production load/soak measurements still require the
operator's environment. Multiworker active/active sharding is not enabled; the lease
prevents overlapping use of one bot/session pool. Raw HLS radio, non-YouTube playlist
pagination and recurring schedules are not implemented. Premium is a service design,
not a payment integration. These limits are explicit rather than placeholder handlers.

## Research references

Feature inspiration, not live-performance guarantees:

- https://github.com/TeamYukki/YukkiMusicBot
- https://notreallyshikhar.gitbook.io/yukkimusicbot/about/getting-started/features
- https://github.com/AnonymousX1025/AnonXMusic
- https://github.com/AsmSafone/MusicPlayer
- https://github.com/AsmSafone/RadioPlayerV3
- https://github.com/subinps/VCPlayerBot
- https://github.com/ITZ-ZAID/Zaid-Vc-Player
- https://github.com/TheTeamVivek/YukkiMusic
- https://core.telegram.org/bots/webapps
