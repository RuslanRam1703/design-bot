"""Resolver обложки Behance-проекта через Open Graph (E2E-эксперимент).

ЭКСПЕРИМЕНТ, а не production-путь. Проверяем гипотезу: для кейсов, уже
опубликованных дизайнером на Behance, обложку можно не хранить у себя
вообще — Behance отдаёт в HTML тег og:image со ссылкой на свой CDN, и
Mini App может подставить её напрямую в <img src>. Тогда для таких кейсов
не нужен ни object storage, ни proxy: байты картинки идут из CDN Behance
прямо в браузер посетителя, минуя Render.

Что этот модуль СОЗНАТЕЛЬНО не делает (границы эксперимента):
- не скачивает само изображение ни в память, ни на диск — только HEAD для
  проверки доступности; байты картинки никогда не проходят через Render;
- не парсит modules проекта и не импортирует галерею — только обложка;
- не является веб-краулером: работает ровно с одним URL, который явно
  ввёл дизайнер, и только если это host behance.net;
- не ходит ни за какими credentials — запросы полностью анонимные.

Транспорт — синхронный urllib, обёрнутый в asyncio.to_thread на границе с
async-кодом: тот же паттерн, что уже применён в
content_store._upstash_command и r2_storage._http_request (единый стиль
внешних HTTP-вызовов, без новых зависимостей в requirements.txt).

Существующий storage backend (bot/r2_storage.py и вся четвёрка
is_configured/generate_object_key/upload_image/delete_image) этим модулем
не затрагивается вообще: он ничего не загружает и ничего не удаляет.
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

# Только сам Behance. Не "любой сайт с og:image" — это не общий scraper
# (см. докстринг модуля): произвольный внешний URL не должен приводить к
# тому, что наш сервер пойдёт его загружать.
_ALLOWED_HOSTS = ("behance.net", "www.behance.net")

# Нейтральный UA: некоторые CDN/фронты отвечают 400 на пустой User-Agent
# (проверено на самом behance.net — /robots.txt без UA отдаёт 400).
_USER_AGENT = "Mozilla/5.0 (compatible; DesignAssistantBot/1.0; portfolio-cover-resolver)"

# Fix A. Edge Adobe (Fastly/Varnish) отдал Render HTTP 403 на ту же самую
# страницу, которая локально отдаётся с HTTP 200 тем же кодом и тем же UA
# (см. production-логи: "Behance вернул HTTP 403", Server: Varnish против
# server: adobe у успешного ответа). Голый urllib-запрос уходил всего с
# тремя заголовками (Host / User-Agent / Accept-Encoding: identity) — это
# заметный bot-сигнал для edge-фильтра. Здесь набор заголовков приводится
# к тому, что реально шлёт браузер.
#
# Accept-Encoding НАМЕРЕННО без "br": brotli нет в stdlib, и попросив br,
# мы получили бы тело, которое нечем распаковать (og:image перестал бы
# находиться). gzip/deflate распаковываются вручную в _decode_body ниже —
# urllib, в отличие от requests, САМ этого не делает.
_BASE_HEADERS = {
    "User-Agent": _USER_AGENT,
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate",
}

# Sec-Fetch-* осмысленны только применительно к типу запроса: страница
# кейса запрашивается как навигация по документу, обложка на CDN — как
# изображение с чужого origin.
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

# Оба порядка атрибутов: content может стоять как после property, так и до.
# Закрывающая кавычка сразу после og:image обязательна — иначе паттерн
# поймал бы и og:image:width/og:image:height, которые тоже есть на странице.
_OG_IMAGE_PATTERNS = (
    re.compile(r'<meta[^>]+property=["\']og:image["\'][^>]*?content=["\']([^"\']+)["\']', re.IGNORECASE),
    re.compile(r'<meta[^>]+content=["\']([^"\']+)["\'][^>]*?property=["\']og:image["\']', re.IGNORECASE),
)

# Путь конкретного проекта: /gallery/<числовой id>/<slug>. Профиль
# (/username), главная (/) и прочие страницы Behance сюда не подходят —
# см. is_behance_project_url. Группа — сам id проекта, он же понадобится
# для отбора нужной картинки (см. extract_project_cdn_url).
_PROJECT_PATH_PATTERN = re.compile(r"^/gallery/(\d+)(?:/|$)")

# Читалка r.jina.ai. Прямой запрос к www.behance.net с Render получает 403
# на edge Adobe/Fastly (воспроизведено дважды в production, в том числе с
# полным набором browser-заголовков) — при этом Render → r.jina.ai →
# Behance измерен из того же egress и работает: HTTP 200, ~32 КБ, ~0.35 с.
# Обычный режим (markdown) выбран намеренно: он на порядок легче, чем
# x-respond-with: html (~1.4 МБ), и уже содержит нужные CDN-ссылки.
_JINA_ENDPOINT = "https://r.jina.ai/"

# Картинки самого проекта лежат под /project_modules/<size>/<file>.
# ВАЖНО: в ответе есть и /projects/... — это превью ЧУЖИХ проектов из
# блоков рекомендаций, и они идут РАНЬШЕ нужных нам (проверено на реальном
# ответе: первые 6 ссылок — чужие работы). Поэтому брать "первую попавшуюся"
# CDN-ссылку нельзя: обложкой кейса стала бы чужая картинка.
_CDN_MODULE_PATTERN = re.compile(
    r"https://mir-s3-cdn-cf\.behance\.net/project_modules/[^/\s\"'()\[\]]+/[^\s\"'()\[\]]+",
    re.IGNORECASE,
)

# CDN-адрес вида .../project_modules/<size>/<file> — единственная форма, в
# которой безопасно подменять size-вариант (см. _disp_variant).
_CDN_SIZE_PATTERN = re.compile(
    r"^(https://mir-s3-cdn-cf\.behance\.net/project_modules/)([^/]+)(/.+)$", re.IGNORECASE
)


class BehanceResolveError(Exception):
    """Обложку получить не удалось: не тот host, страница недоступна, нет
    og:image, или найденный URL картинки не отвечает. Вызывающий код должен
    показать дизайнеру понятную ошибку и НЕ записывать cover — пустая
    обложка уже корректно отрабатывается фронтендом (см. renderPortfolio:
    c.cover ? <img> : card-cover-empty), поэтому отдельного UI не нужно."""


def is_behance_project_url(url: str) -> bool:
    """Строгая проверка host + пути: только behance.net/www.behance.net,
    только http(s), и только конкретный ПРОЕКТ (/gallery/<id>/...).

    Проверка пути — не придирка: без неё https://www.behance.net/ (профиль
    или главная) тоже успешно "резолвится", потому что og:image там есть —
    но это generic SEO-картинка Behance, а не обложка кейса. Дизайнер,
    вставивший ссылку на свой профиль вместо проекта, молча получил бы
    логотип Behance в качестве обложки (воспроизведено при live-проверке
    error-путей). Всё отсекается ДО первого сетевого запроса."""
    if not isinstance(url, str) or not url.strip():
        return False
    try:
        parsed = urllib.parse.urlparse(url.strip())
    except ValueError:
        return False
    if parsed.scheme not in ("http", "https"):
        return False
    if (parsed.hostname or "").lower() not in _ALLOWED_HOSTS:
        return False
    return bool(_PROJECT_PATH_PATTERN.match(parsed.path))


def project_id_from_url(url: str) -> str | None:
    """id проекта из /gallery/<id>/... — им отбирается нужная картинка среди
    множества CDN-ссылок в ответе r.jina.ai (см. extract_project_cdn_url)."""
    try:
        parsed = urllib.parse.urlparse((url or "").strip())
    except ValueError:
        return None
    match = _PROJECT_PATH_PATTERN.match(parsed.path)
    return match.group(1) if match else None


def extract_project_cdn_url(text: str, project_id: str) -> str | None:
    """Первая /project_modules/-ссылка, у которой ИМЯ ФАЙЛА содержит id
    проекта.

    Отбор по id обязателен, а не для красоты: в ответе r.jina.ai рядом
    лежат превью чужих проектов из рекомендаций, и они встречаются раньше
    (проверено на реальном ответе — первые шесть ссылок чужие). Имя файла
    модуля выглядит как "<префикс><project_id>.<hash>.<ext>", поэтому
    вхождение id в basename однозначно привязывает картинку к нужному
    проекту."""
    if not text or not project_id:
        return None
    for url in _CDN_MODULE_PATTERN.findall(text):
        basename = url.rsplit("/", 1)[-1]
        if project_id in basename.split(".", 1)[0]:
            return url
    return None


def extract_og_image(html_text: str) -> str | None:
    """Достаёт content из <meta property="og:image">. html.unescape —
    обязателен: в атрибуте URL приходит с HTML-энтити (&amp; вместо &),
    и без раскодирования такая ссылка ведёт на 404."""
    for pattern in _OG_IMAGE_PATTERNS:
        match = pattern.search(html_text)
        if match:
            return html_module.unescape(match.group(1)).strip()
    return None


def _disp_variant(image_url: str) -> str | None:
    """Behance отдаёт один и тот же ассет в нескольких размерах, меняется
    только один сегмент пути (disp/max_1200/1400/hd/fs/source). "disp"
    заметно легче (проверено: 71 КБ против 267 КБ у 1400) и его достаточно
    для карточки портфолио.

    Возвращает None, если URL не соответствует ТОЧНО ожидаемой форме — то
    есть подменять сегмент небезопасно. Искусственно ломать URL ради
    оптимизации нельзя: корректность важнее экономии трафика, поэтому
    вызывающий код в этом случае просто оставит оригинальный og:image."""
    match = _CDN_SIZE_PATTERN.match(image_url)
    if not match or match.group(2).lower() == "disp":
        return None
    return f"{match.group(1)}disp{match.group(3)}"


def _decode_body(raw: bytes, content_encoding: str | None) -> bytes:
    """urllib (в отличие от requests) НЕ распаковывает ответ сам — раз мы
    просим gzip/deflate в Accept-Encoding, распаковать обязаны здесь, иначе
    в extract_og_image придут сжатые байты и обложка перестанет находиться.
    Неизвестная/пустая кодировка — возвращаем как есть."""
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


def _http_head(url: str, timeout: int = 10) -> int:
    request = urllib.request.Request(url, headers=_IMAGE_HEADERS, method="HEAD")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status
    except urllib.error.HTTPError as e:
        return e.code


async def _is_accessible(image_url: str) -> bool:
    """HEAD, а не GET — доступность проверяем, байты не забираем."""
    try:
        status = await asyncio.to_thread(_http_head, image_url)
    except urllib.error.URLError as e:
        logger.warning("Behance: обложка недоступна (%s): %s", image_url, e.reason)
        return False
    return status == 200


async def resolve_cover_url(project_url: str, *, prefer_disp: bool = True) -> str:
    """Behance project URL -> публичный URL обложки на CDN Behance.

    Бросает BehanceResolveError на любой контролируемой неудаче — молчаливо
    вернуть "почти правильный" результат здесь нельзя: cover попадёт в
    portfolio.json и станет битой картинкой у клиента."""
    if not is_behance_project_url(project_url):
        raise BehanceResolveError(f"не ссылка на Behance: {project_url!r}")
    project_id = project_id_from_url(project_url)
    if not project_id:
        raise BehanceResolveError(f"не удалось определить id проекта: {project_url!r}")

    # Читаем страницу ЧЕРЕЗ r.jina.ai, а не напрямую: прямой запрос к
    # www.behance.net с Render блокируется на edge Adobe (HTTP 403).
    try:
        status, body = await asyncio.to_thread(_http_get, _JINA_ENDPOINT + project_url)
    except urllib.error.URLError as e:
        raise BehanceResolveError(f"r.jina.ai недоступен: {e.reason}") from e
    if status != 200:
        raise BehanceResolveError(f"r.jina.ai вернул HTTP {status}")

    text = body.decode("utf-8", errors="replace")
    image_url = extract_project_cdn_url(text, project_id)
    if not image_url:
        # Сюда же попадает случай, когда r.jina.ai сменит формат ответа:
        # подходящей ссылки просто не найдётся, и мы честно сообщим об
        # ошибке вместо того, чтобы записать чужую или битую картинку.
        raise BehanceResolveError(f"в ответе r.jina.ai нет изображения проекта {project_id}")

    if prefer_disp:
        candidate = _disp_variant(image_url)
        if candidate and await _is_accessible(candidate):
            return candidate

    if not await _is_accessible(image_url):
        raise BehanceResolveError(f"изображение недоступно: {image_url}")
    return image_url
