"""Environment configuration for the bot. Import `settings` to access validated config.

Note: scripts/generate_session.py deliberately does NOT use this module — it only
needs API_ID/API_HASH and must work before ASSISTANT_SESSIONS exists at all.
"""
from __future__ import annotations

import sys
from typing import Annotated

from pydantic import ValidationError, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    api_id: int
    api_hash: str
    bot_token: str
    owner_id: int
    # NoDecode: pydantic-settings otherwise tries to json.loads() this field
    # (it's a list[str]) before our validator ever runs, which throws an
    # unhandled SettingsError on any plain comma-separated session string —
    # exactly the format .env.example documents. NoDecode skips that and
    # hands the raw string to _split_sessions below instead.
    assistant_sessions: Annotated[list[str], NoDecode]
    mongo_uri: str

    log_group_id: int | None = None
    duration_limit_min: int = 60
    max_vc_per_assistant: int = 1
    default_lang: str = "en"

    # Optional — Spotify link resolution (bot/platforms/spotify.py) degrades
    # gracefully to "unsupported" without these, rather than failing to start.
    spotify_client_id: str | None = None
    spotify_client_secret: str | None = None

    @field_validator("log_group_id", "spotify_client_id", "spotify_client_secret", mode="before")
    @classmethod
    def _blank_to_none(cls, value: object) -> object:
        return None if value == "" else value

    @field_validator("assistant_sessions", mode="before")
    @classmethod
    def _split_sessions(cls, value: object) -> list[str]:
        if isinstance(value, str):
            return [s.strip() for s in value.split(",") if s.strip()]
        if isinstance(value, list):
            return [str(s).strip() for s in value if str(s).strip()]
        return []

    @field_validator("assistant_sessions")
    @classmethod
    def _require_at_least_one_session(cls, value: list[str]) -> list[str]:
        if not value:
            raise ValueError(
                "no assistant session configured — generate one with "
                "'python scripts/generate_session.py' and set ASSISTANT_SESSIONS in .env"
            )
        return value


def load_settings() -> Settings:
    """Load and validate settings, exiting with a readable summary on any error."""
    try:
        return Settings()  # type: ignore[call-arg]
    except ValidationError as exc:
        print("Configuration error — fix the following in your .env file:\n", file=sys.stderr)
        for error in exc.errors():
            field = ".".join(str(loc) for loc in error["loc"])
            print(f"  - {field.upper()}: {error['msg']}", file=sys.stderr)
        print("\nSee .env.example for the full list of required variables.", file=sys.stderr)
        sys.exit(1)


settings = load_settings()
