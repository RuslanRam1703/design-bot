import json
import logging
from pathlib import Path

from aiohttp import web

from bot import config, content_store
from bot.telegram_auth import validate_init_data

WEBAPP_DIR = Path(__file__).resolve().parent.parent / "webapp"
DATA_DIR = Path(__file__).resolve().parent.parent / "data"
logger = logging.getLogger(__name__)


async def handle_index(request: web.Request) -> web.Response:
    response = web.FileResponse(WEBAPP_DIR / "index.html")
    # Telegram-клиенты иногда переиспользуют уже открытый WebView для того же
    # бота и не подгружают HTML заново при повторном нажатии кнопки меню —
    # без запрета кэша экран мог оставаться прежним (баг с "не переключается
    # на калькулятор"). Путь тоже меняем на уникальный для каждого экрана —
    # см. keyboards.py — это надёжнее, чем один путь с разным ?screen=.
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate"
    return response


async def handle_health(request: web.Request) -> web.Response:
    return web.Response(text="ok")


async def handle_my_leads(request: web.Request) -> web.Response:
    """"Мои заявки" — клиент видит ТОЛЬКО свои заявки. user_id никогда не
    берётся из query/body напрямую (это позволило бы запросить чужие
    заявки, подставив произвольный id) — только из initData, подпись
    которой проверяется здесь через validate_init_data (см. bot/telegram_auth.py)."""
    init_data = request.headers.get("X-Telegram-Init-Data", "")
    user = validate_init_data(init_data, config.BOT_TOKEN)
    if user is None:
        # Диагностика реальной причины пустого/невалидного initData у
        # настоящих клиентов — раньше клиент вообще не долетал до сервера в
        # этом случае (см. app.js), поэтому в логах не было ни следа. Ничего
        # секретного не пишем (длина строки, не содержимое), проверка
        # подписи этим не ослабляется — на решение "пустить/не пустить" эти
        # поля не влияют.
        logger.warning(
            "my-leads unauthorized: initData_len=%d platform=%r version=%r has_hash=%r hash_has_tgwebappdata=%r ua=%r",
            len(init_data),
            request.headers.get("X-Debug-Platform", ""),
            request.headers.get("X-Debug-Version", ""),
            request.headers.get("X-Debug-Has-Hash", ""),
            request.headers.get("X-Debug-Hash-Has-TgWebAppData", ""),
            request.headers.get("User-Agent", "")[:120],
        )
        return web.json_response({"error": "unauthorized"}, status=401)
    leads = content_store.list_leads_by_user(user["id"])
    return web.json_response(leads, dumps=lambda d: json.dumps(d, ensure_ascii=False))


async def _no_cache(request: web.Request, response: web.StreamResponse) -> None:
    # index.html уже запрещал кэш (см. handle_index) — но /js/, /css/, /data/
    # отдавались через add_static БЕЗ Cache-Control вовсе, а не только без
    # no-store: aiohttp.web_fileresponse только проставляет Last-Modified и
    # обрабатывает If-Modified-Since, ничего не говоря клиенту "не кэшируй".
    # WebView в Telegram-клиенте мог годами отдавать app.js/style.css/данные
    # из собственного диск-кэша вообще без обращения к серверу — то есть
    # заново открытая (не переиспользованная) страница всё равно показывала
    # старый JS. Не трогаем /img/ — картинки кейсов можно кэшировать, старая
    # картинка не ломает функциональность, в отличие от старого app.js.
    if request.path.startswith(("/js/", "/css/", "/data/")):
        response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate"


def create_app() -> web.Application:
    app = web.Application()
    app.on_response_prepare.append(_no_cache)
    for path in ("/", "/portfolio", "/about", "/calculator", "/brief", "/myleads"):
        app.router.add_get(path, handle_index)
    app.router.add_get("/health", handle_health)
    app.router.add_get("/api/my-leads", handle_my_leads)
    app.router.add_static("/css/", WEBAPP_DIR / "css")
    app.router.add_static("/js/", WEBAPP_DIR / "js")
    app.router.add_static("/img/", WEBAPP_DIR / "img")
    app.router.add_static("/data/", DATA_DIR)
    return app
