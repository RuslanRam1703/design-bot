"""Проверка Telegram Mini App initData — официальный документированный
алгоритм (https://core.telegram.org/bots/webapps#validating-data-received-via-the-mini-app),
не самодеятельность: без этого Mini App не может доказать боту, что
конкретный запрос действительно пришёл от аутентифицированного
Telegram-пользователя, а не просто от кого угодно, подставившего
произвольный user_id в запрос (см. "Мои заявки" — нельзя отдавать чужие
заявки по непроверенному id)."""

import hashlib
import hmac
import json
import time
import urllib.parse
from typing import Any


def validate_init_data(init_data: str, bot_token: str, *, max_age_seconds: int = 86400) -> dict[str, Any] | None:
    """Возвращает распарсенный dict пользователя ({"id": ..., "username": ...,
    "first_name": ..., "last_name": ...}), если подпись верна и данные не
    протухли, иначе None. max_age_seconds — защита от повторного
    использования старой, ранее перехваченной initData (Telegram сам
    рекомендует проверять auth_date)."""
    if not init_data or not bot_token:
        return None

    try:
        pairs = urllib.parse.parse_qsl(init_data, strict_parsing=True)
    except ValueError:
        return None

    data = dict(pairs)
    received_hash = data.pop("hash", None)
    data.pop("signature", None)  # ed25519-подпись, отдельное поле — не участвует в data-check-string
    if not received_hash:
        return None

    data_check_string = "\n".join(f"{k}={v}" for k, v in sorted(data.items()))
    secret_key = hmac.new(b"WebAppData", bot_token.encode("utf-8"), hashlib.sha256).digest()
    computed_hash = hmac.new(secret_key, data_check_string.encode("utf-8"), hashlib.sha256).hexdigest()

    if not hmac.compare_digest(computed_hash, received_hash):
        return None

    auth_date = data.get("auth_date")
    if auth_date is not None:
        try:
            if time.time() - int(auth_date) > max_age_seconds:
                return None
        except ValueError:
            return None

    user_raw = data.get("user")
    if not user_raw:
        return None
    try:
        user = json.loads(user_raw)
    except json.JSONDecodeError:
        return None
    if not isinstance(user, dict) or "id" not in user:
        return None
    return user
