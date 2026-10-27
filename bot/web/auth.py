import hashlib
import hmac
import json
import re
import time
from urllib.parse import parse_qsl


def validate_init_data(raw, token, *, now=None):
    if not raw or len(raw) > 8192:
        raise ValueError('Open this app inside Telegram.')
    pairs = parse_qsl(raw, keep_blank_values=True, strict_parsing=True)
    data = dict(pairs)
    if len(data) != len(pairs):
        raise ValueError('Duplicate authentication fields.')
    received = data.pop('hash', '')
    if not re.fullmatch(r'[a-fA-F0-9]{64}', received):
        raise ValueError('Invalid Telegram signature.')
    check = '\n'.join(f'{key}={value}' for key, value in sorted(data.items()))
    secret = hmac.new(b'WebAppData', token.encode(), hashlib.sha256).digest()
    expected = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(received, expected):
        raise ValueError('Invalid Telegram signature.')
    now = time.time() if now is None else now
    try:
        age = now - int(data['auth_date'])
        user = json.loads(data['user'])
        if age < -30 or age > 600 or not isinstance(user, dict) or type(user.get('id')) is not int or user['id'] <= 0 or user.get('is_bot'):
            raise ValueError
    except (KeyError, TypeError, ValueError):
        raise ValueError('Session expired or invalid. Reopen the Mini App.') from None
    return user
