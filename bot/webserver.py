import dataclasses
import json
import logging
from pathlib import Path

from aiogram import Bot
from aiohttp import web

from bot import config, content_store
from bot.calculator import calculate
from bot.data import load_pricing
from bot.lead import format_lead_message
from bot.telegram_auth import diagnose_init_data, validate_init_data

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
        diag = diagnose_init_data(init_data, config.BOT_TOKEN)
        logger.warning(
            "my-leads unauthorized: initData_len=%d platform=%r version=%r has_hash=%r hash_has_tgwebappdata=%r "
            "initdata_ascii_only=%r parse_ok=%r hash_present=%r hmac_valid=%r auth_date_present=%r "
            "auth_date_valid=%r user_present=%r user_json_ok=%r parsed_item_count=%r "
            "data_check_string_length=%r data_check_string_sha256=%r received_hash_length=%r "
            "calculated_hash_length=%r ua=%r",
            len(init_data),
            request.headers.get("X-Debug-Platform", ""),
            request.headers.get("X-Debug-Version", ""),
            request.headers.get("X-Debug-Has-Hash", ""),
            request.headers.get("X-Debug-Hash-Has-TgWebAppData", ""),
            request.headers.get("X-Debug-InitData-Ascii-Only", ""),
            diag.parse_ok,
            diag.hash_present,
            diag.hmac_valid,
            diag.auth_date_present,
            diag.auth_date_valid,
            diag.user_present,
            diag.user_json_ok,
            diag.parsed_item_count,
            diag.data_check_string_length,
            diag.data_check_string_sha256,
            diag.received_hash_length,
            diag.calculated_hash_length,
            request.headers.get("User-Agent", "")[:120],
        )
        return web.json_response({"error": "unauthorized"}, status=401)
    leads = content_store.list_leads_by_user(user["id"])
    return web.json_response(leads, dumps=lambda d: json.dumps(d, ensure_ascii=False))


async def handle_create_lead(request: web.Request) -> web.Response:
    """Основной путь создания заявки — заменяет Telegram.WebApp.sendData()
    для submitBrief() (webapp/js/app.js). sendData() официально работает
    только для Mini App, запущенного через KeyboardButton.web_app
    (https://core.telegram.org/bots/webapps: "This method is only available
    for Mini Apps launched via a Keyboard button") — а мы сознательно ушли
    от этой кнопки ради Telegram.WebApp.initData (см. bot/keyboards.py).
    Тот же принцип identity, что и в handle_my_leads: user_id ТОЛЬКО из
    validate_init_data, из body не принимается и не читается вообще —
    подменить чужой user_id через body физически невозможно."""
    init_data = request.headers.get("X-Telegram-Init-Data", "")
    user = validate_init_data(init_data, config.BOT_TOKEN)
    if user is None:
        diag = diagnose_init_data(init_data, config.BOT_TOKEN)
        logger.warning(
            "create-lead unauthorized: initData_len=%d parse_ok=%r hash_present=%r hmac_valid=%r "
            "auth_date_present=%r auth_date_valid=%r user_present=%r user_json_ok=%r",
            len(init_data), diag.parse_ok, diag.hash_present, diag.hmac_valid,
            diag.auth_date_present, diag.auth_date_valid, diag.user_present, diag.user_json_ok,
        )
        return web.json_response({"error": "unauthorized"}, status=401)

    try:
        payload = await request.json()
    except json.JSONDecodeError:
        return web.json_response({"error": "invalid_json"}, status=400)
    if not isinstance(payload, dict):
        return web.json_response({"error": "invalid_payload"}, status=400)

    calc_result = None
    calc_payload = payload.get("calc")
    if isinstance(calc_payload, dict) and calc_payload.get("service_id"):
        pricing = load_pricing()
        calc_result = calculate(
            pricing=pricing,
            service_id=calc_payload["service_id"],
            selected=calc_payload.get("options", []),
            urgent=bool(calc_payload.get("urgent")),
            complex_=bool(calc_payload.get("complex")),
        )
    calc_summary = dataclasses.asdict(calc_result) if calc_result and calc_result.valid else None

    # user — уже провалидированный dict из validate_init_data ({"id":...,
    # "username":..., "first_name":..., "last_name":...}), а не aiogram
    # message.from_user — та же форма telegram_identity, что и в legacy
    # bot/handlers/webapp.py::_handle_brief_submission, но из другого источника.
    telegram_identity = {
        "user_id": user["id"],
        "username": user.get("username"),
        "first_name": user.get("first_name"),
        "last_name": user.get("last_name"),
    }
    lead = content_store.add_lead(payload, telegram_identity, calc_summary, draft_id=payload.get("draft_id"))

    bot: Bot = request.app["bot"]
    text = format_lead_message(payload, calc_result, from_user_id=user["id"], username=user.get("username"))
    if config.DESIGNER_CHAT_ID:
        try:
            await bot.send_message(chat_id=config.DESIGNER_CHAT_ID, text=text, parse_mode="HTML")
        except Exception:
            logger.exception("Не удалось отправить заявку дизайнеру (lead #%s)", lead["id"])
    else:
        logger.warning("DESIGNER_CHAT_ID не задан — заявка не отправлена дизайнеру (lead #%s)", lead["id"])

    price_range = None
    if calc_summary:
        price_range = {"from": calc_summary["price_from"], "to": calc_summary["price_to"]}

    return web.json_response(
        {"lead_id": lead["id"], "attach_tz": bool(payload.get("attach_tz")), "price_range": price_range},
        dumps=lambda d: json.dumps(d, ensure_ascii=False),
        status=200,
    )


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


def create_app(bot: Bot) -> web.Application:
    """bot — тот же Bot-инстанс, что и polling в bot/main.py::main(),
    минимально необходимый способ дать handle_create_lead возможность
    уведомить DESIGNER_CHAT_ID (aiohttp-приложение раньше не имело доступа
    ни к Bot, ни к Dispatcher вообще — см. аудит перед этим изменением)."""
    app = web.Application()
    app["bot"] = bot
    app.on_response_prepare.append(_no_cache)
    for path in ("/", "/portfolio", "/about", "/calculator", "/brief", "/myleads"):
        app.router.add_get(path, handle_index)
    app.router.add_get("/health", handle_health)
    app.router.add_get("/api/my-leads", handle_my_leads)
    app.router.add_post("/api/leads", handle_create_lead)
    app.router.add_static("/css/", WEBAPP_DIR / "css")
    app.router.add_static("/js/", WEBAPP_DIR / "js")
    app.router.add_static("/img/", WEBAPP_DIR / "img")
    app.router.add_static("/data/", DATA_DIR)
    return app
