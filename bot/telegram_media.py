"""Resolver обложки из публичного поста Telegram (второй источник cover).

Тот же принцип, что и bot/behance.py: для кейса, уже опубликованного
дизайнером в публичном Telegram-канале, обложку можно не хранить у себя —
веб-превью t.me отдаёт в HTML тег og:image со ссылкой на CDN Telegram
(cdn*.telesco.pe), и Mini App подставляет её напрямую в <img src>. Байты
картинки идут из CDN Telegram прямо в браузер посетителя, минуя Render.

Почему НЕ через Bot API: Bot API не предоставляет чтения произвольного
сообщения по id (нет getMessage/getMessages), а единственный download-URL
(api.telegram.org/file/bot<TOKEN>/...) содержит токен и потому непригоден
для браузера. Здесь токен не используется вообще — все запросы анонимные,
ровно те же, что делает любой обычный веб-превью-краулер.

Что этот модуль СОЗНАТЕЛЬНО не делает (границы, как и у behance.py):
- не скачивает изображение ни в память, ни на диск — только HEAD для
  проверки доступности; байты картинки никогда не проходят через Render;
- не является веб-краулером: работает ровно с одним URL, который явно ввёл
  дизайнер, и только если это host t.me;
- не ходит ни за какими credentials и не читает bot token;
- не публикует ничего в Telegram (см. "Media Archive" ниже).

Media Archive (СЛЕДУЮЩИЙ ЭТАП, здесь НЕ реализован) — этот же resolver
рассчитан на повторное использование в цепочке прямой загрузки:

    фото от дизайнера -> sendPhoto(в публичный archive-канал)
    -> message_id -> https://t.me/<archive>/<message_id>
    -> resolve_cover_url() (этот модуль, без изменений)
    -> tokenless cdn*.telesco.pe URL

То есть archive-этап добавит только шаг публикации, а извлечение обложки
останется этим кодом. Ни конфига канала, ни sendPhoto, ни env-переменных
здесь намеренно нет: round-trip через реальный канал ещё не проверен живым
тестом, а класть в код непроверенный путь — значит выдать гипотезу за
рабочую функциональность.

Транспорт — синхронный urllib в asyncio.to_thread: тот же паттерн, что в
behance.py, content_store._upstash_command и r2_storage._http_request
(единый стиль внешних HTTP-вызовов, без новых зависимостей).
"""

import asyncio
import gzip
import html as html_module
import logging
import re
import urllib.error
import urllib.parse
import urllib.request
import zlib

logger = logging.getLogger(__name__)

# Только сам Telegram. telegram.me — исторический алиас t.me, отвечает тем
# же превью. Произвольный внешний URL не должен приводить к тому, что наш
# сервер пойдёт его загружать (та же граница, что и _ALLOWED_HOSTS в
# behance.py).
_ALLOWED_HOSTS = ("t.me", "www.t.me", "telegram.me", "www.telegram.me")

# Первый сегмент пути у публичного поста — username канала. Правила
# Telegram: 5-32 символа, начинается с буквы, дальше буквы/цифры/подчёркивание.
_POST_PATH_PATTERN = re.compile(r"^/([A-Za-z][A-Za-z0-9_]{3,31})/(\d+)/?$")

# Служебные первые сегменты t.me, которые НЕ являются username канала.
# Критичен здесь "c": t.me/c/<internal_id>/<msg> — ссылка на ПРИВАТНЫЙ
# канал. Она синтаксически неотличима от обычной (c/1234567890/5 подходит
# под шаблон выше), но публичного превью у неё нет — проверено: страница
# отдаёт HTTP 200 с ПУСТЫМ og:image. Молча пропустив её дальше, мы бы
# сходили в сеть впустую и показали дизайнеру невнятную ошибку вместо
# понятной "это приватный пост".
_RESERVED_FIRST_SEGMENTS = frozenset({
    "c", "s", "joinchat", "addstickers", "addemoji", "addtheme", "proxy",
    "socks", "share", "iv", "login", "bg", "setlanguage", "confirmphone",
    "contact", "invoice", "giftcode", "boost", "m", "k", "a",
})

# CDN веб-превью Telegram: cdn1..cdnN.telesco.pe. Это ЕДИНСТВЕННЫЙ host,
# который мы готовы записать в cover — og:image не должен уводить нас на
# произвольный домен, даже если Telegram когда-нибудь начнёт его отдавать.
_CDN_HOST_PATTERN = re.compile(r"^cdn\d*\.telesco\.pe$", re.IGNORECASE)

_USER_AGENT = "Mozilla/5.0 (compatible; DesignAssistantBot/1.0; portfolio-cover-resolver)"

# Accept-Encoding НАМЕРЕННО без "br" — brotli нет в stdlib (та же причина,
# что подробно расписана в behance.py): попросив br, мы получили бы тело,
# которое нечем распаковать, и og:image перестал бы находиться.
_BASE_HEADERS = {
    "User-Agent": _USER_AGENT,
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate",
}

_PAGE_HEADERS = {
    **_BASE_HEADERS,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Upgrade-Insecure-Requests": "1",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
}

_IMAGE_HEADERS = {
    **_BASE_HEADERS,
    "Accept": "image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8",
    "Sec-Fetch-Dest": "image",
    "Sec-Fetch-Mode": "no-cors",
    "Sec-Fetch-Site": "cross-site",
}

# Оба порядка атрибутов (content до или после property). Закрывающая
# кавычка сразу после og:image обязательна — иначе паттерн поймал бы и
# og:image:width/og:image:height. Тот же приём, что в behance.py.
_OG_IMAGE_PATTERNS = (
    re.compile(r'<meta[^>]+property=["\']og:image["\'][^>]*?content=["\']([^"\']*)["\']', re.IGNORECASE),
    re.compile(r'<meta[^>]+content=["\']([^"\']*)["\'][^>]*?property=["\']og:image["\']', re.IGNORECASE),
)

# ---- ?embed=1: полноразмерный вариант ----
#
# og:image обычного поста отдаёт НЕ оригинал, а превью ~320px по ширине
# (замерено вживую: отправили 800x300 -> og:image вернул 320x120, 6514 Б,
# тогда как Telegram хранил и вариант 800x300, 23807 Б). Для обложки кейса
# это заметная потеря качества.
#
# Страница ?embed=1 того же поста отдаёт САМЫЙ КРУПНЫЙ вариант: у того же
# поста оттуда приходит ровно 800x300 / 23807 Б — побайтово тот самый
# largest PhotoSize, который вернул sendPhoto.
#
# Структура (проверена на трёх разных каналах, включая каналы С аватаркой):
#   медиа   -> style="...background-image:url('https://cdnN.telesco.pe/...')"
#   аватар  -> <i class="...user_photo..."><img src="https://cdnN.telesco.pe/...">
# Пересечение множеств пустое во всех проверенных случаях, то есть аватарка
# в background-image не появляется в принципе. Тем не менее аватарка
# вычитается явно (см. _extract_embed_media_urls) — структурная гарантия
# подтверждена на выборке из трёх каналов, а не документирована Telegram,
# и цена ошибки здесь — чужая картинка в портфолио.
_EMBED_SUFFIX = "?embed=1"

_EMBED_MEDIA_PATTERN = re.compile(
    r"background-image:url\('(https://cdn\d*\.telesco\.pe/file/[^']+)'\)", re.IGNORECASE
)

_EMBED_AVATAR_PATTERN = re.compile(
    r'user_photo[^>]*>\s*<img[^>]+src="(https://cdn\d*\.telesco\.pe/file/[^"]+)"', re.IGNORECASE
)


class TelegramMediaResolveError(Exception):
    """Обложку получить не удалось: не тот host/формат ссылки, страница
    недоступна, нет og:image, в посте нет своей картинки (вернулась аватарка
    канала), или найденный CDN-URL не отвечает. Вызывающий код должен
    показать дизайнеру понятную ошибку и НЕ записывать cover."""


def parse_post_url(url: str) -> tuple[str, str] | None:
    """(username, message_id) для публичного поста, иначе None.

    Отсекается ДО первого сетевого запроса: приватные t.me/c/..., профили
    без message id, корни каналов, служебные пути и чужие домены."""
    if not isinstance(url, str) or not url.strip():
        return None
    try:
        parsed = urllib.parse.urlparse(url.strip())
    except ValueError:
        return None
    if parsed.scheme not in ("http", "https"):
        return None
    if (parsed.hostname or "").lower() not in _ALLOWED_HOSTS:
        return None
    match = _POST_PATH_PATTERN.match(parsed.path)
    if not match:
        return None
    username, message_id = match.group(1), match.group(2)
    if username.lower() in _RESERVED_FIRST_SEGMENTS:
        return None
    return username, message_id


def is_telegram_post_url(url: str) -> bool:
    return parse_post_url(url) is not None


def normalize_post_url(url: str) -> str | None:
    """Канонический https://t.me/<username>/<id> без query и fragment.

    Query отбрасывается намеренно: ?single / ?comment=... / ?embed=1
    относятся к способу ОТОБРАЖЕНИЯ поста, а не к его идентичности, и в
    external_url кейса им делать нечего — в Mini App ссылка должна
    открывать сам пост."""
    parsed = parse_post_url(url)
    if parsed is None:
        return None
    username, message_id = parsed
    return f"https://t.me/{username}/{message_id}"


def channel_root_url(url: str) -> str | None:
    """Корень канала для того же поста — источник эталонной аватарки
    (см. resolve_cover_url про discriminator)."""
    parsed = parse_post_url(url)
    if parsed is None:
        return None
    return f"https://t.me/{parsed[0]}"


def extract_og_image(html_text: str) -> str | None:
    """content из <meta property="og:image">. html.unescape обязателен:
    в атрибуте URL приходит с HTML-энтити (&amp; вместо &).

    Пустая строка возвращается как None: приватный пост (t.me/c/...) отдаёт
    именно `content=""` при HTTP 200 — проверено вживую. Без этого пустая
    строка уехала бы дальше по цепочке как "успешный" результат."""
    for pattern in _OG_IMAGE_PATTERNS:
        match = pattern.search(html_text or "")
        if match:
            value = html_module.unescape(match.group(1)).strip()
            return value or None
    return None


def is_valid_cdn_url(image_url: str) -> bool:
    """https + host строго cdn*.telesco.pe + отсутствие следов bot token.

    Проверка на токен избыточна при уже жёстком whitelist host'а, но стоит
    здесь намеренно: cover уходит в portfolio.json, который отдаётся
    публично (см. webserver.PUBLIC_DATA_FILES), и цена ошибки — утечка
    токена бота, а не битая картинка."""
    if not isinstance(image_url, str) or not image_url.strip():
        return False
    try:
        parsed = urllib.parse.urlparse(image_url.strip())
    except ValueError:
        return False
    if parsed.scheme != "https":
        return False
    if not _CDN_HOST_PATTERN.match(parsed.hostname or ""):
        return False
    lowered = image_url.lower()
    if "/bot" in lowered or "api.telegram.org" in lowered:
        return False
    return True


def _decode_body(raw: bytes, content_encoding: str | None) -> bytes:
    """urllib (в отличие от requests) НЕ распаковывает ответ сам — раз мы
    просим gzip/deflate, распаковать обязаны здесь."""
    encoding = (content_encoding or "").strip().lower()
    if "gzip" in encoding:
        return gzip.decompress(raw)
    if "deflate" in encoding:
        try:
            return zlib.decompress(raw)
        except zlib.error:  # raw deflate без zlib-заголовка
            return zlib.decompress(raw, -zlib.MAX_WBITS)
    return raw


def _http_get(url: str, timeout: int = 15) -> tuple[int, bytes]:
    request = urllib.request.Request(url, headers=_PAGE_HEADERS, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, _decode_body(response.read(), response.headers.get("Content-Encoding"))
    except urllib.error.HTTPError as e:
        return e.code, b""


def _http_head(url: str, timeout: int = 10) -> tuple[int, str, int]:
    """(status, content_type, content_length). HEAD, а не GET — доступность
    проверяем, байты не забираем."""
    request = urllib.request.Request(url, headers=_IMAGE_HEADERS, method="HEAD")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            length = response.headers.get("Content-Length") or "0"
            try:
                length_int = int(length)
            except ValueError:
                length_int = 0
            return response.status, (response.headers.get("Content-Type") or "").lower(), length_int
    except urllib.error.HTTPError as e:
        return e.code, "", 0


async def _fetch_og_image(page_url: str, what: str) -> str | None:
    try:
        status, body = await asyncio.to_thread(_http_get, page_url)
    except urllib.error.URLError as e:
        raise TelegramMediaResolveError(f"{what} недоступна: {e.reason}") from e
    if status != 200:
        raise TelegramMediaResolveError(f"{what} вернула HTTP {status}")
    return extract_og_image(body.decode("utf-8", errors="replace"))


async def _is_accessible_image(image_url: str) -> bool:
    """200 + image/* + ненулевая длина. Только 200 недостаточно: превью
    может отдать заглушку нулевого размера, и она стала бы битой картинкой
    в карточке кейса."""
    try:
        status, content_type, length = await asyncio.to_thread(_http_head, image_url)
    except urllib.error.URLError as e:
        logger.warning("Telegram: обложка недоступна (%s): %s", image_url, e.reason)
        return False
    if status != 200:
        logger.warning("Telegram: обложка вернула HTTP %s (%s)", status, image_url)
        return False
    if not content_type.startswith("image/"):
        logger.warning("Telegram: обложка не изображение (Content-Type=%r)", content_type)
        return False
    if length <= 0:
        logger.warning("Telegram: обложка нулевой длины (%s)", image_url)
        return False
    return True


def extract_embed_media_urls(html_text: str) -> list[str]:
    """CDN-ссылки НА МЕДИА со страницы ?embed=1, без аватарок.

    Аватарка вычитается явным множеством, а не только за счёт того, что
    лежит в другом атрибуте (см. комментарий у _EMBED_AVATAR_PATTERN)."""
    if not html_text:
        return []
    avatars = set(_EMBED_AVATAR_PATTERN.findall(html_text))
    return [u for u in _EMBED_MEDIA_PATTERN.findall(html_text) if u not in avatars]


async def _resolve_from_embed(normalized_url: str) -> str | None:
    """Полноразмерный вариант через ?embed=1, либо None — тогда вызывающий
    код честно откатывается на og:image.

    None (а не исключение) на любой проблеме здесь намеренно: embed — это
    УЛУЧШЕНИЕ качества, а не обязательный шаг. Если разметка страницы
    когда-нибудь изменится, кейс должен продолжать получать обложку из
    og:image, пусть и меньшего размера, а не падать с ошибкой."""
    try:
        status, body = await asyncio.to_thread(_http_get, normalized_url + _EMBED_SUFFIX)
    except urllib.error.URLError as e:
        logger.warning("Telegram: embed-страница недоступна (%s): %s", normalized_url, e.reason)
        return None
    if status != 200:
        logger.warning("Telegram: embed-страница вернула HTTP %s (%s)", status, normalized_url)
        return None

    for candidate in extract_embed_media_urls(body.decode("utf-8", errors="replace")):
        if not is_valid_cdn_url(candidate):
            continue
        if await _is_accessible_image(candidate):
            return candidate
    return None


async def resolve_cover_url(post_url: str, *, prefer_full_resolution: bool = True) -> str:
    """Публичный Telegram post URL -> tokenless URL картинки на CDN Telegram.

    Сначала пробуется полноразмерный вариант со страницы ?embed=1 (см.
    _resolve_from_embed), затем — прежний путь через og:image. og:image
    остаётся именно как ЗАПАСНОЙ путь, а не как основной: он отдаёт
    ~320px-превью вместо оригинала.

    Бросает TelegramMediaResolveError на любой контролируемой неудаче:
    молча вернуть "почти правильный" результат нельзя — cover попадёт в
    portfolio.json и станет битой (или ЧУЖОЙ) картинкой у клиента."""
    normalized = normalize_post_url(post_url)
    root_url = channel_root_url(post_url)
    if not normalized or not root_url:
        raise TelegramMediaResolveError(f"не ссылка на публичный пост Telegram: {post_url!r}")

    if prefer_full_resolution:
        full = await _resolve_from_embed(normalized)
        if full:
            return full

    post_image = await _fetch_og_image(normalized, "страница поста")
    if not post_image:
        raise TelegramMediaResolveError(f"в посте нет og:image: {normalized}")

    # ГЛАВНАЯ ловушка t.me, проверенная вживую: пост без своей картинки И
    # НЕСУЩЕСТВУЮЩИЙ пост отдают HTTP 200 с og:image, равным АВАТАРКЕ
    # КАНАЛА (t.me/telegram/999999 вернул ровно тот же URL, что и корень
    # t.me/telegram). Без этого сравнения обложкой кейса молча стала бы
    # аватарка канала, а опечатка в номере поста выглядела бы как успех.
    avatar_image = await _fetch_og_image(root_url, "страница канала")
    if avatar_image and post_image == avatar_image:
        raise TelegramMediaResolveError(
            f"в посте нет своего изображения (og:image совпал с аватаркой канала): {normalized}"
        )

    if not is_valid_cdn_url(post_image):
        raise TelegramMediaResolveError(f"og:image не с CDN Telegram: {post_image!r}")

    if not await _is_accessible_image(post_image):
        raise TelegramMediaResolveError(f"изображение недоступно: {post_image}")
    return post_image
