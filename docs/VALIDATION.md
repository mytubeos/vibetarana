# Expanded blueprint validation

27 October 2026, Linux, Python 3.12, locked `requirements.txt` dependencies.

- **147 automated tests passed**, one real-Mongo integration test skipped locally
  because `VIBETARANA_MONGO_TEST_URI` is not configured.
- Dependency consistency (`pip check`), Python syntax compilation, JavaScript syntax
  and patch whitespace checks passed.
- Chromium browser smoke passed at 390px mobile and 1100px desktop: rendered queue,
  playlist buttons, play payload with request ID/revision, literal rendering of an
  HTML-injection title, no horizontal mobile overflow, no JavaScript page errors.
  Screenshots were visually inspected. Browser API/Telegram responses were fixtures.
- Real FFmpeg/ffprobe media-validation tests remain in the passing regression suite.
- One upstream Pyrogram import-time event-loop deprecation warning remains.

New regressions cover fair FIFO interleaving and priority overrides, duplicate votes,
revoked/outsider control denial, signed/expired/duplicate-field Mini App data,
owner-only metrics, stale web actions checked inside the lock, bounded provider
usage, canonical playlist URLs, timezone/DST windows, missed/cancelled schedules,
lease conflicts/expiry/ownership-scoped release, native-call shutdown and mocked
Docker promotion/rollback. The prior 105 regression tests still pass. The autoplay
fixture now uses distinct valid YouTube IDs to exercise history exclusion correctly.

CI adds a real MongoDB service integration for exclusive lease acquisition, stale
owner release and chat-scoped schedule cancellation, plus the Chromium smoke test.
Check the commit's GitHub Actions result for hosted verification status; local results
are not a claim that those hosted jobs already passed.

No real Telegram, Mongo production database, YouTube catalog, OpenAI or AudD
credentials were used. No production messages or deployments were performed. Docker
is not available in this development environment; deployment subprocesses are tested
with mocks. Live provider requests, private-group playback, actual HTTPS/BotFather
configuration, Docker-host rollout and load/soak measurements remain acceptance work.
See `BLUEPRINT.md` and `OPERATIONS.md` for exact gates and supported feature limits.
