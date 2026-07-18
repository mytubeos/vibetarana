"""Standalone assistant session generator.

Run this once per assistant account you want to add:
    python scripts/generate_session.py

It logs in interactively (phone number + OTP, and your 2FA password if you
have one set) using ONLY API_ID/API_HASH from .env, then prints a session
string to paste into ASSISTANT_SESSIONS. Deliberately does not import
config.py's Settings — that class requires ASSISTANT_SESSIONS to already be
set, which would be circular here.

Use a spare/secondary phone number, not your personal one — this account will
join a voice chat as a visible member in every group the bot plays music in.
"""
from __future__ import annotations

import asyncio
import os
import sys

from dotenv import load_dotenv
from pyrogram import Client

load_dotenv()

API_ID = os.environ.get("API_ID")
API_HASH = os.environ.get("API_HASH")

if not API_ID or not API_HASH:
    sys.exit(
        "Set API_ID and API_HASH in your .env file first (copy .env.example to "
        ".env and fill those two in — get them from my.telegram.org)."
    )


async def main() -> None:
    print(
        "Logging in as the ASSISTANT account (use a spare number, not your "
        "personal one) — enter its phone number, then the OTP code Telegram "
        "sends you.\n"
    )
    async with Client(
        "assistant_session_gen",
        api_id=int(API_ID),
        api_hash=API_HASH,
        in_memory=True,
    ) as app:
        session_string = await app.export_session_string()

    print(
        "\nDone. Add this to ASSISTANT_SESSIONS in .env "
        "(comma-separate if you add more assistants later):\n"
    )
    print(session_string)
    print("\nKeep it secret — anyone with this string can log in as this account.")


if __name__ == "__main__":
    asyncio.run(main())
