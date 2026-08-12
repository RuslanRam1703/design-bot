from pathlib import Path

from aiohttp import web

WEBAPP_DIR = Path(__file__).resolve().parent.parent / "webapp"
DATA_DIR = Path(__file__).resolve().parent.parent / "data"


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
    for path in ("/", "/portfolio", "/about", "/calculator", "/brief"):
        app.router.add_get(path, handle_index)
    app.router.add_get("/health", handle_health)
    app.router.add_static("/css/", WEBAPP_DIR / "css")
    app.router.add_static("/js/", WEBAPP_DIR / "js")
    app.router.add_static("/img/", WEBAPP_DIR / "img")
    app.router.add_static("/data/", DATA_DIR)
    return app
