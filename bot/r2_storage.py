"""Cloudflare R2 (S3-совместимое) хранилище байтов изображений (Batch 3).

Зачем отдельный модуль, а не ещё один блок в content_store.py: R2 хранит
ТОЛЬКО байты картинок портфолио/about — JSON-метаданные (cover/images/avatar
— сами ссылки на эти байты) остаются в Upstash через content_store.py как и
раньше, ничего в этом разделении не меняется. bot/content_store.py импортирует
эти две функции и решает, когда их вызывать; сам этот модуль ничего не знает
про portfolio.json/about.json.

Почему hand-rolled SigV4 через urllib, а не boto3/aiobotocore: requirements.txt
этого проекта намеренно минимален (aiogram, aiohttp, python-dotenv — тот же
принцип уже применён к Upstash, который тоже доступен только через сырые REST-
запросы, без redis-клиента). boto3/aiobotocore — большие библиотеки с
множеством транзитивных зависимостей ради двух HTTP-методов (PUT/DELETE
одного объекта без листинга/multipart) — непропорционально всему остальному
стеку проекта. AWS Signature Version 4 при этом реализована здесь полностью
(canonical request, signing key derivation, Authorization header) — не
упрощена и не ослаблена ради экономии кода; каждый шаг покрыт отдельным
детерминированным тестом (см. tests/test_regression.py), а верность самого
алгоритма сверена с опубликованным AWS canonical-headers примером и с
независимой реализацией signing key derivation.

Транспорт — синхронный urllib.request (не aiohttp.ClientSession), обёрнутый
в asyncio.to_thread на границе с async-кодом — тот же паттерн, что уже
используется для Upstash (см. content_store.py::_upstash_command, P1-1
design review: "internal helpers... могут остаться sync, если не делают
network I/O напрямую внутри async-функции"). Единый стиль внешних REST-
вызовов на весь проект, а не два разных HTTP-клиента для двух похожих задач."""

import asyncio
import hashlib
import hmac
import logging
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timezone

from bot import config

logger = logging.getLogger(__name__)

_SERVICE = "s3"
_REGION = "auto"
_ALGORITHM = "AWS4-HMAC-SHA256"


class R2UploadError(Exception):
    """Upload в R2 не удался (сеть, авторизация, сам R2 вернул ошибку) — см.
    bot/content_store.py::save_case_photo/save_about_photo: когда R2
    сконфигурирован, это исключение должно долететь до admin-хендлера как
    есть, НЕ должно превращаться в тихий fallback на локальный диск (Batch 3
    product decision — иначе portfolio.json указывал бы на файл, реально
    существующий только на эфемерном диске Render, ложный success)."""


def generate_object_key(prefix: str, stem: str, ext: str) -> str:
    """f"{prefix}/{stem}_{uuid8}{ext}" — уникальный ключ на каждую загрузку
    (Batch 3 product decision: unique object keys предпочтительнее ключей,
    зависящих от case_id+расширения) — повторная загрузка обложки/фото
    никогда не перезаписывает и не осиротошивает предыдущий объект молча:
    старый явно удаляется вызывающим кодом (content_store.py) через
    delete_image() по его прежнему URL, до того как записать новый."""
    return f"{prefix}/{stem}_{uuid.uuid4().hex[:8]}{ext}"


def is_configured() -> bool:
    return bool(
        config.R2_ACCOUNT_ID and config.R2_ACCESS_KEY_ID and config.R2_SECRET_ACCESS_KEY
        and config.R2_BUCKET_NAME and config.R2_PUBLIC_BASE_URL
    )


def _endpoint_host() -> str:
    return f"{config.R2_ACCOUNT_ID}.r2.cloudflarestorage.com"


def _sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _hmac_digest(key: bytes, msg: str) -> bytes:
    return hmac.new(key, msg.encode("utf-8"), hashlib.sha256).digest()


def derive_signing_key(secret_key: str, date_stamp: str) -> bytes:
    """AWS SigV4 key derivation chain — тот же алгоритм, что и в официальной
    AWS-документации по SigV4 (region здесь всегда "auto", service — "s3",
    так специфицирует сам Cloudflare R2 для своего S3-совместимого API)."""
    k_date = _hmac_digest(("AWS4" + secret_key).encode("utf-8"), date_stamp)
    k_region = _hmac_digest(k_date, _REGION)
    k_service = _hmac_digest(k_region, _SERVICE)
    return _hmac_digest(k_service, "aws4_request")


def build_canonical_request(method: str, canonical_uri: str, headers: dict[str, str], payload_hash: str) -> tuple[str, str]:
    """headers — уже только те, что должны быть подписаны, ключи в нижнем
    регистре (canonical header names AWS SigV4 всегда lowercase). Возвращает
    (canonical_request, signed_headers) — signed_headers нужен отдельно и
    для string-to-sign, и для итогового Authorization-заголовка."""
    signed_header_names = sorted(headers.keys())
    canonical_headers = "".join(f"{name}:{headers[name].strip()}\n" for name in signed_header_names)
    signed_headers = ";".join(signed_header_names)
    canonical_request = "\n".join([
        method,
        canonical_uri,
        "",  # canonical query string — не используется (объектные PUT/DELETE без query-параметров)
        canonical_headers,
        signed_headers,
        payload_hash,
    ])
    return canonical_request, signed_headers


def build_authorization_header(method: str, key: str, payload: bytes, extra_headers: dict[str, str] | None = None) -> tuple[str, dict[str, str]]:
    """Собирает подписанный запрос целиком: (url, headers-для-отправки).
    extra_headers — доп. заголовки, которые тоже должны быть подписаны
    (например, content-type при PUT); ключи должны быть в нижнем регистре."""
    now = datetime.now(timezone.utc)
    amz_date = now.strftime("%Y%m%dT%H%M%SZ")
    date_stamp = now.strftime("%Y%m%d")
    host = _endpoint_host()
    canonical_uri = f"/{config.R2_BUCKET_NAME}/{key}"
    payload_hash = _sha256_hex(payload)

    headers_to_sign = {
        "host": host,
        "x-amz-content-sha256": payload_hash,
        "x-amz-date": amz_date,
        **(extra_headers or {}),
    }
    canonical_request, signed_headers = build_canonical_request(method, canonical_uri, headers_to_sign, payload_hash)

    credential_scope = f"{date_stamp}/{_REGION}/{_SERVICE}/aws4_request"
    string_to_sign = "\n".join([
        _ALGORITHM,
        amz_date,
        credential_scope,
        _sha256_hex(canonical_request.encode("utf-8")),
    ])

    signing_key = derive_signing_key(config.R2_SECRET_ACCESS_KEY, date_stamp)
    signature = hmac.new(signing_key, string_to_sign.encode("utf-8"), hashlib.sha256).hexdigest()

    authorization = (
        f"{_ALGORITHM} Credential={config.R2_ACCESS_KEY_ID}/{credential_scope}, "
        f"SignedHeaders={signed_headers}, Signature={signature}"
    )

    request_headers = {k: v for k, v in headers_to_sign.items()}
    request_headers["Authorization"] = authorization
    url = f"https://{host}{canonical_uri}"
    return url, request_headers


def _http_request(method: str, url: str, headers: dict[str, str], data: bytes | None) -> tuple[int, bytes]:
    """Синхронная транспортная функция (см. докстринг модуля) — единственное
    место, где реально уходит сетевой запрос. Возвращает (status, body) даже
    для НЕ-2xx ответов (urllib поднимает HTTPError вместо возврата — здесь
    он перехватывается и приводится к тому же (status, body) виду, чтобы
    вызывающему async-коду не нужно было различать два разных механизма
    сообщения об ошибке)."""
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


async def upload_image(key: str, content: bytes, content_type: str) -> str:
    """Загружает байты под готовым object key (см. generate_object_key) и
    возвращает публичный URL (R2_PUBLIC_BASE_URL + "/" + key). Бросает
    R2UploadError при любой неудаче — намеренно НЕ проглатывается здесь и НЕ
    подменяется локальным fallback (см. докстринг R2UploadError и Batch 3
    product decision): вызывающий код (content_store.py) уже решил
    воспользоваться R2 именно потому, что он сконфигурирован, а значит успех
    должен быть настоящим, либо ошибка должна быть видна."""
    url, headers = build_authorization_header("PUT", key, content, {"content-type": content_type})
    try:
        status, body = await asyncio.to_thread(_http_request, "PUT", url, headers, content)
    except urllib.error.URLError as e:
        raise R2UploadError(f"R2 PUT {key} network error: {e.reason}") from e
    if status not in (200, 201):
        raise R2UploadError(f"R2 PUT {key} -> HTTP {status}: {body[:300]!r}")
    return f"{config.R2_PUBLIC_BASE_URL}/{key}"


async def delete_image(url: str) -> None:
    """Best-effort удаление по ПУБЛИЧНОМУ URL (тому, что был сохранён в
    portfolio.json/about.json) — если url не начинается с текущего
    R2_PUBLIC_BASE_URL, это либо legacy-локальный относительный путь (демо
    SVG), либо объект из другого окружения; в обоих случаях ничего не
    удаляем и не считаем ошибкой (см. content_store.py::remove_case_image/
    delete_case — вызывают это для КАЖДОГО удаляемого images-значения, не
    только для заведомо R2-путей).

    Неудача самого DELETE (сеть, R2 вернул ошибку) НЕ бросает исключение
    дальше — тем же принципом, что и остальные "best-effort, не блокировать
    основное действие" уведомления в этом проекте (см. bot/handlers/admin.py,
    bot/handlers/start.py: logger.exception + продолжить), но она явно
    логируется как ERROR (Batch 3 product decision: "deletion failures must
    be handled deliberately and reported, not silently hidden") — сирота в
    R2 не должен пройти незамеченным, даже если основное действие дизайнера
    (убрать картинку из portfolio.json) не должно из-за этого блокироваться."""
    if not is_configured():
        return
    base = config.R2_PUBLIC_BASE_URL
    if not url.startswith(base + "/"):
        return
    key = url[len(base) + 1:]
    req_url, headers = build_authorization_header("DELETE", key, b"")
    try:
        status, body = await asyncio.to_thread(_http_request, "DELETE", req_url, headers, None)
    except urllib.error.URLError as e:
        logger.error("R2 DELETE %s network error: %s (orphan object may remain)", key, e.reason)
        return
    if status not in (200, 202, 204, 404):
        logger.error("R2 DELETE %s -> HTTP %s: %s (orphan object may remain)", key, status, body[:300])
