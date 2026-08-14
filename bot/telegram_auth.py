"""Проверка Telegram Mini App initData — официальный документированный
алгоритм (https://core.telegram.org/bots/webapps#validating-data-received-via-the-mini-app),
не самодеятельность: без этого Mini App не может доказать боту, что
конкретный запрос действительно пришёл от аутентифицированного
Telegram-пользователя, а не просто от кого угодно, подставившего
произвольный user_id в запрос (см. "Мои заявки" — нельзя отдавать чужие
заявки по непроверенному id)."""

import dataclasses
import hashlib
import hmac
import json
import time
import urllib.parse
from typing import Any


def _parse_and_split(init_data: str) -> tuple[dict[str, str], str | None] | None:
    """Общая для validate_init_data и diagnose_init_data часть разбора —
    чтобы диагностика гарантированно смотрела на те же данные, что и
    реальная проверка, а не на отдельно пересчитанные. None — если сама
    строка не парсится как query string."""
    try:
        pairs = urllib.parse.parse_qsl(init_data, strict_parsing=True)
    except ValueError:
        return None
    data = dict(pairs)
    received_hash = data.pop("hash", None)
    data.pop("signature", None)  # ed25519-подпись, отдельное поле — не участвует в data-check-string
    return data, received_hash


def _compute_hash(data: dict[str, str], bot_token: str) -> str:
    data_check_string = "\n".join(f"{k}={v}" for k, v in sorted(data.items()))
    secret_key = hmac.new(b"WebAppData", bot_token.encode("utf-8"), hashlib.sha256).digest()
    return hmac.new(secret_key, data_check_string.encode("utf-8"), hashlib.sha256).hexdigest()


def validate_init_data(init_data: str, bot_token: str, *, max_age_seconds: int = 86400) -> dict[str, Any] | None:
    """Возвращает распарсенный dict пользователя ({"id": ..., "username": ...,
    "first_name": ..., "last_name": ...}), если подпись верна и данные не
    протухли, иначе None. max_age_seconds — защита от повторного
    использования старой, ранее перехваченной initData (Telegram сам
    рекомендует проверять auth_date). Логика 1:1 совпадает с версией до
    вынесения _parse_and_split/_compute_hash — рефакторинг чисто
    структурный, см. diagnose_init_data и тесты, подтверждающие это."""
    if not init_data or not bot_token:
        return None

    parsed = _parse_and_split(init_data)
    if parsed is None:
        return None
    data, received_hash = parsed
    if not received_hash:
        return None

    computed_hash = _compute_hash(data, bot_token)
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


@dataclasses.dataclass
class InitDataDiagnostics:
    """Только для логов при отказе — ни одно из этих полей не используется
    и не может быть использовано для решения "пустить/не пустить" (это
    делает исключительно validate_init_data, независимо от этого класса).
    None у auth_date_valid/hmac_valid значит "эта проверка не применялась",
    а не "провалилась" (например, hmac_valid=None, если hash вообще
    отсутствовал — сравнивать было не с чем)."""

    parse_ok: bool
    hash_present: bool
    hmac_valid: bool | None
    auth_date_present: bool
    auth_date_valid: bool | None
    user_present: bool
    user_json_ok: bool


def diagnose_init_data(init_data: str, bot_token: str, *, max_age_seconds: int = 86400) -> InitDataDiagnostics:
    """Диагностика initData, которая уже была отклонена validate_init_data —
    в отличие от неё, считает все проверки НЕЗАВИСИМО (без остановки на
    первой неудаче), чтобы один реальный failing-запрос сразу показал все
    причины сразу, а не только первую по порядку. Не логирует и не
    возвращает hash/BOT_TOKEN/init_data целиком/user JSON — только булевы
    результаты проверок. Использует те же _parse_and_split/_compute_hash,
    что и validate_init_data, поэтому не может разойтись с ней в том, что
    считается валидным hash/HMAC."""
    if not init_data or not bot_token:
        return InitDataDiagnostics(False, False, None, False, None, False, False)

    parsed = _parse_and_split(init_data)
    if parsed is None:
        return InitDataDiagnostics(False, False, None, False, None, False, False)
    data, received_hash = parsed

    hash_present = bool(received_hash)
    hmac_valid = None
    if hash_present:
        computed_hash = _compute_hash(data, bot_token)
        hmac_valid = hmac.compare_digest(computed_hash, received_hash)

    auth_date = data.get("auth_date")
    auth_date_present = auth_date is not None
    auth_date_valid = None
    if auth_date_present:
        try:
            auth_date_valid = (time.time() - int(auth_date)) <= max_age_seconds
        except ValueError:
            auth_date_valid = False

    user_raw = data.get("user")
    user_present = bool(user_raw)
    user_json_ok = False
    if user_present:
        try:
            parsed_user = json.loads(user_raw)
            user_json_ok = isinstance(parsed_user, dict) and "id" in parsed_user
        except json.JSONDecodeError:
            user_json_ok = False

    return InitDataDiagnostics(
        parse_ok=True,
        hash_present=hash_present,
        hmac_valid=hmac_valid,
        auth_date_present=auth_date_present,
        auth_date_valid=auth_date_valid,
        user_present=user_present,
        user_json_ok=user_json_ok,
    )
