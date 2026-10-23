"""Optional, rate-limited operator alerts. Never include exception bodies/secrets."""
import time
from collections import OrderedDict
from config import settings

_last = OrderedDict()


async def notify(message: str, *, key: str):
    if settings.log_group_id is None:
        return
    now = time.monotonic()
    if now - _last.get(key, -999999) < 300:
        return
    _last[key] = now
    _last.move_to_end(key)
    while len(_last) > 1000:
        _last.popitem(last=False)
    try:
        from bot.core.client import bot
        from pyrogram.enums import ParseMode
        await bot.send_message(settings.log_group_id, message, parse_mode=ParseMode.DISABLED)
    except Exception:
        pass
