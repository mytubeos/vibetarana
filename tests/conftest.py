"""Pytest collection-time setup.

Some modules genuinely need config at import time (bot.core.assistants reads
settings.max_vc_per_assistant to do its job at all, and bot.core.calls pulls
that in) — unlike the platform resolvers, this isn't something that can be
made lazy, since it's core to what those modules are. Set harmless dummy
values before any test module imports them, so the suite never needs a real
.env to run. setdefault(), not blind assignment, so a real environment's
values (if any, e.g. a developer's shell) aren't clobbered.
"""
import os

os.environ.setdefault("API_ID", "12345678")
os.environ.setdefault("API_HASH", "abcdef1234567890abcdef1234567890")
os.environ.setdefault("BOT_TOKEN", "123456789:AAExampleTokenStringHere")
os.environ.setdefault("OWNER_ID", "123456789")
os.environ.setdefault("ASSISTANT_SESSIONS", "dummy_session_one")
os.environ.setdefault("MONGO_URI", "mongodb+srv://user:pass@cluster.mongodb.net/tg2_musicbot_test")
