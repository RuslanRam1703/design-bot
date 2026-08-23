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
import html as html_module
import logging
import re
import urllib.error
import urllib.parse
import urllib.request

logger = logging.getLogger(__name__)

# Только сам Behance. Не "любой сайт с og:image" — это не общий scraper
# (см. докстринг модуля): произвольный внешний URL не должен приводить к
# тому, что наш сервер пойдёт его загружать.
_ALLOWED_HOSTS = ("behance.net", "www.behance.net")

# Нейтральный UA: некоторые CDN/фронты отвечают 400 на пустой User-Agent
# (проверено на самом behance.net — /robots.txt без UA отдаёт 400).
_USER_AGENT = "Mozilla/5.0 (compatible; DesignAssistantBot/1.0; portfolio-cover-resolver)"

# Оба порядка атрибутов: content может стоять как после property, так и до.
# Закрывающая кавычка сразу после og:image обязательна — иначе паттерн
# поймал бы и og:image:width/og:image:height, которые тоже есть на странице.
_OG_IMAGE_PATTERNS = (
    re.compile(r'<meta[^>]+property=["\']og:image["\'][^>]*?content=["\']([^"\']+)["\']', re.IGNORECASE),
    re.compile(r'<meta[^>]+content=["\']([^"\']+)["\'][^>]*?property=["\']og:image["\']', re.IGNORECASE),
)

# Путь конкретного проекта: /gallery/<числовой id>/<slug>. Профиль
# (/username), главная (/) и прочие страницы Behance сюда не подходят —
# см. is_behance_project_url.
_PROJECT_PATH_PATTERN = re.compile(r"^/gallery/\d+(/|$)")

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


def _http_get(url: str, timeout: int = 15) -> tuple[int, bytes]:
    request = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT}, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as e:
        return e.code, b""


def _http_head(url: str, timeout: int = 10) -> int:
    request = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT}, method="HEAD")
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

    try:
        status, body = await asyncio.to_thread(_http_get, project_url)
    except urllib.error.URLError as e:
        raise BehanceResolveError(f"Behance недоступен: {e.reason}") from e
    if status != 200:
        raise BehanceResolveError(f"Behance вернул HTTP {status}")

    og_image = extract_og_image(body.decode("utf-8", errors="replace"))
    if not og_image:
        raise BehanceResolveError("на странице нет og:image")
    if not og_image.startswith("https://"):
        raise BehanceResolveError(f"og:image не https-ссылка: {og_image!r}")

    if prefer_disp:
        candidate = _disp_variant(og_image)
        if candidate and await _is_accessible(candidate):
            return candidate

    if not await _is_accessible(og_image):
        raise BehanceResolveError(f"изображение недоступно: {og_image}")
    return og_image
