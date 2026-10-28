import dataclasses
import json
import logging
from pathlib import Path

from aiogram import Bot
from aiohttp import web

from bot import config, content_store, texts
from bot.calculator import calculate
from bot.data import load_pricing
from bot.lead import format_lead_message, format_lead_supplement_message
from bot.notifications import notify_designer_lead, notify_designer_supplement
from bot.telegram_auth import diagnose_init_data, validate_init_data

SUPPLEMENT_FIELD_KEYS = ("comment", "additional_requirements", "references", "contact")


async def _pricing_for_calculation() -> dict:
    # /admin writes to Upstash when configured. Never calculate against
    # stale deployment-time pricing.json.
    if content_store.is_redis_backed():
        return await content_store.read_async("pricing.json")
    return load_pricing()


def _valid_new_lead_payload(payload: dict) -> bool:
    """Reject malformed external input before persisting or formatting it."""
    if payload.get("mode", "new") != "new":
        return False
    text_fields = (
        "service_id", "service_name", "task_description", "deadline",
        "budget", "contact", "source", "source_case_id",
        "source_case_title", "draft_id",
    )
    for key in text_fields:
        value = payload.get(key)
        if value is not None and (not isinstance(value, str) or len(value) > 5000):
            return False
    if "attach_tz" in payload and not isinstance(payload["attach_tz"], bool):
        return False
    have = payload.get("have", [])
    if not isinstance(have, list) or len(have) > 30 or any(
        not isinstance(value, str) or len(value) > 200 for value in have
    ):
        return False
    tz = payload.get("tz_details")
    if tz is not None and (
        not isinstance(tz, dict) or
        any(not isinstance(value, str) or len(value) > 5000 for value in tz.values())
    ):
        return False
    calc = payload.get("calc")
    if calc is None:
        return True
    if not isinstance(calc, dict) or not isinstance(calc.get("service_id"), str):
        return False
    if len(calc["service_id"]) > 150:
        return False
    selected = calc.get("options", [])
    if not isinstance(selected, list) or len(selected) > 100:
        return False
    for item in selected:
        if not isinstance(item, dict) or not isinstance(item.get("id"), str):
            return False
        qty = item.get("qty", 1)
        if not isinstance(qty, int) or isinstance(qty, bool) or not 1 <= qty <= 100:
            return False
    for name in ("urgent", "complex"):
        if name in calc and not isinstance(calc[name], bool):
            return False
    return True


WEBAPP_DIR = Path(__file__).resolve().parent.parent / "webapp"
logger = logging.getLogger(__name__)

# Whitelist, не blacklist: только эти файлы Mini App реально запрашивает
# (см. webapp/js/app.js::init() — ровно эти 4 fetch()). leads.json (реальные
# контакты клиентов при выключенном Upstash — см. content_store.py) и
# faq.json (Mini App его вообще не читает, FAQ — только бот) никогда не
# становятся доступны просто потому, что лежат в той же директории — раньше
# add_static("/data/", DATA_DIR) отдавал ВСЮ директорию целиком (см.
# production-аудит, P0-1). Любой новый файл, который когда-нибудь появится
# в data/, по умолчанию НЕ публичен, пока явно не добавлен сюда.
PUBLIC_DATA_FILES = ("pricing.json", "portfolio.json", "about.json", "ui_config.json")


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


async def handle_public_data(request: web.Request) -> web.Response:
    """Отдаёт ТОЛЬКО файлы из PUBLIC_DATA_FILES (см. её докстринг выше) —
    любое другое имя, включая leads.json, получает обычный 404, как если
    бы файла не существовало вовсе (не 403 — не подтверждаем и не
    опровергаем существование чего-либо за пределами whitelist).

    Читает через content_store.read_async — тот же backend, что и /admin
    (Upstash, если включён, иначе локальный файл): раньше эта ручка отдавала
    файл напрямую с локального диска (web.FileResponse), а /admin в
    Upstash-режиме пишет ТОЛЬКО в Redis — Mini App продолжал показывать
    устаревший deploy-time снапшот после любой правки в /admin (production
    freshness bug). Второго source of truth для этих 4 файлов больше нет."""
    filename = request.match_info["filename"]
    if filename not in PUBLIC_DATA_FILES:
        raise web.HTTPNotFound()
    try:
        data = await content_store.read_async(filename)
    except FileNotFoundError:
        raise web.HTTPNotFound()
    except Exception:
        logger.exception("Не удалось прочитать публичный /data/%s", filename)
        return web.json_response({"error": "internal"}, status=500)
    return web.json_response(data, dumps=lambda d: json.dumps(d, ensure_ascii=False))


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
    leads = await content_store.list_leads_by_user(user["id"])
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
    except (json.JSONDecodeError, web.HTTPBadRequest, UnicodeDecodeError):
        return web.json_response({"error": "invalid_json"}, status=400)
    if not isinstance(payload, dict):
        return web.json_response({"error": "invalid_payload"}, status=400)
    if payload.get("mode") != "supplement" and not _valid_new_lead_payload(payload):
        return web.json_response({"error": "invalid_payload"}, status=400)

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
    bot: Bot = request.app["bot"]

    if payload.get("mode") == "supplement":
        if not isinstance(payload.get("fields"), dict) or any(not isinstance(v, str) or len(v) > 5000 for v in payload["fields"].values()):
            return web.json_response({"error": "invalid_fields"}, status=400)
        if "wants_file" in payload and not isinstance(payload["wants_file"], bool):
            return web.json_response({"error": "invalid_payload"}, status=400)
        return await _handle_lead_supplement(payload, telegram_identity, bot)
    return await _handle_lead_create(payload, telegram_identity, bot)


async def _notify_client(bot: Bot, telegram_identity: dict, text: str, *, lead_id: int) -> None:
    """Подтверждение КЛИЕНТУ в его чат с ботом — best-effort.

    Зачем вообще: Mini App отправляет заявку через POST /api/leads, и до
    этого места по боевому пути клиенту не уходило НИЧЕГО — уведомление
    получал только дизайнер. Пользователь закрывал окно (в том числе
    кнопкой «Отправить материалы») и оказывался в чате, где не было ни
    подтверждения, ни инструкции прислать файл, хотя заявка уже ждала его
    (awaiting_tz_file). Текст с инструкцией в texts.py существовал, но
    вызывался только из legacy-обработчика sendData(), который Mini App
    больше не использует.

    chat_id берётся из telegram_identity — а он собран из подписи initData
    (см. handle_create_lead выше), не из тела запроса. Отправить
    подтверждение в чужой чат подставленным id поэтому невозможно.

    Ошибка отправки НЕ проваливает запрос: заявка уже сохранена и
    дизайнер уже уведомлён — тот же принцип best-effort, что и у
    уведомления дизайнера чуть ниже. Клиент в худшем случае не увидит
    подтверждения, но заявка не потеряется."""
    user_id = telegram_identity.get("user_id")
    if not user_id:
        return
    try:
        await bot.send_message(chat_id=user_id, text=text)
    except Exception:
        logger.exception("Не удалось отправить подтверждение клиенту (lead #%s)", lead_id)


def _price_range_text(calc_summary: dict | None) -> str | None:
    """Та же форма строки, что и в legacy-пути (bot/handlers/webapp.py):
    тексты подтверждений общие, значит и цена в них должна выглядеть
    одинаково независимо от того, каким путём пришла заявка."""
    if not calc_summary:
        return None
    return f"{calc_summary['price_from']:,} – {calc_summary['price_to']:,} ₽".replace(",", " ")


async def _handle_lead_create(payload: dict, telegram_identity: dict, bot: Bot) -> web.Response:
    calc_result = None
    calc_payload = payload.get("calc")
    if isinstance(calc_payload, dict) and calc_payload.get("service_id"):
        pricing = await _pricing_for_calculation()
        calc_result = calculate(
            pricing=pricing,
            service_id=calc_payload["service_id"],
            selected=calc_payload.get("options", []),
            urgent=bool(calc_payload.get("urgent")),
            complex_=bool(calc_payload.get("complex")),
        )
    calc_summary = dataclasses.asdict(calc_result) if calc_result and calc_result.valid else None

    try:
        lead = await content_store.add_lead(payload, telegram_identity, calc_summary, draft_id=payload.get("draft_id"))
    except content_store.NotLeadOwnerError:
        # draft_id совпал с чужой заявкой (см. content_store.add_lead,
        # production-аудит P1-2) — ничего не создано и не изменено.
        return web.json_response({"error": "forbidden"}, status=403)
    except content_store.LeadClosedError:
        # Batch 2 — устаревший draft_id клиента совпал с уже закрытой
        # (DONE/CANCELLED) заявкой: апсерт в неё не делаем, ничего не
        # создано и не изменено (см. content_store.add_lead).
        return web.json_response({"error": "lead_closed"}, status=409)
    created = lead["created"]

    # Уведомляем владельца ТОЛЬКО при реальном создании — повторный/
    # дублирующийся POST того же draft_id (см. аудит: кнопка "Отправить
    # заявку" могла до фикса срабатывать несколько раз на один клик)
    # обновляет ту же заявку через add_lead(), но не должен слать вторую
    # копию "Новая заявка" в чат дизайнеру.
    if created or lead.get("changed") or (lead.get("designer_notification") or {}).get("state") != "sent":
        if not config.DESIGNER_CHAT_ID:
            logger.warning("DESIGNER_CHAT_ID не задан — уведомление о заявке #%s ожидает отправки", lead["id"])
        await notify_designer_lead(bot, lead)

    if created:
        # Подтверждение клиенту — под тем же `if created`, что и уведомление
        # дизайнера: повторный POST того же draft_id обновляет ту же заявку
        # и не должен слать вторую копию подтверждения (клиент его уже
        # получил при первой отправке).
        attach_tz = bool(payload.get("attach_tz"))
        ack = texts.lead_ack_ask_file if attach_tz else texts.lead_received_ack
        await _notify_client(
            bot, telegram_identity,
            ack(lead["id"], payload.get("service_name"), _price_range_text(calc_summary)),
            lead_id=lead["id"],
        )

    price_range = None
    if calc_summary:
        price_range = {"from": calc_summary["price_from"], "to": calc_summary["price_to"]}

    return web.json_response(
        {"lead_id": lead["id"], "created": created, "attach_tz": bool(payload.get("attach_tz")), "price_range": price_range},
        dumps=lambda d: json.dumps(d, ensure_ascii=False),
        status=200,
    )


async def _handle_lead_supplement(payload: dict, telegram_identity: dict, bot: Bot) -> web.Response:
    """mode="supplement" — дополнение к уже существующей заявке, адресуется
    строго по lead_id (НЕ draft_id — тот принадлежит другому, уже
    закрытому этапу жизни заявки, см. content_store.add_lead_supplement).
    Не трогает lead["payload"] — то, что было в исходном Order Builder,
    остаётся как есть; дополнение — отдельная append-only запись."""
    lead_id = payload.get("lead_id")
    if not isinstance(lead_id, int) or isinstance(lead_id, bool):
        return web.json_response({"error": "invalid_lead_id"}, status=400)

    raw_fields = payload.get("fields")
    if not isinstance(raw_fields, dict):
        return web.json_response({"error": "invalid_fields"}, status=400)
    fields = {
        key: raw_fields[key].strip()
        for key in SUPPLEMENT_FIELD_KEYS
        if isinstance(raw_fields.get(key), str) and raw_fields[key].strip()
    }
    wants_file = bool(payload.get("wants_file"))
    if not fields and not wants_file:
        return web.json_response({"error": "empty_supplement"}, status=400)

    try:
        lead, supplement_id = await content_store.add_lead_supplement(lead_id, telegram_identity, fields, wants_file=wants_file)
    except (content_store.LeadNotFoundError, content_store.NotLeadOwnerError):
        # Единый внешне наблюдаемый ответ для "такого lead_id нет" и "lead_id
        # чужой" (см. production-аудит, P2-11) — иначе разница 404 vs 403
        # позволяла бы перебором lead_id узнавать, какие ID вообще существуют
        # у других клиентов. Сами exception-типы внутри content_store не
        # менялись — различие полезно для логики/логов, наружу его просто не
        # показываем.
        return web.json_response({"error": "not_found"}, status=404)
    except content_store.LeadClosedError:
        # Batch 2 — в отличие от LeadNotFoundError/NotLeadOwnerError выше,
        # здесь владение УЖЕ подтверждено (проверка статуса в
        # add_lead_supplement идёт после проверки owner) — раскрытие "эта
        # заявка закрыта" не даёт узнать ничего о чужих lead_id, поэтому
        # отдельный, содержательный код ответа безопасен (P2-11 сюда не
        # относится).
        return web.json_response({"error": "lead_closed"}, status=409)

    await notify_designer_supplement(bot, lead, supplement_id)

    # Тот же принцип, что и при создании заявки: клиент, закрывший Mini App,
    # должен увидеть в чате и подтверждение, и — если отметил файл —
    # инструкцию его прислать (add_lead_supplement уже выставил
    # awaiting_tz_file, см. wants_file выше).
    await _notify_client(
        bot, telegram_identity,
        texts.supplement_ack_ask_file(lead_id) if wants_file else texts.supplement_ack(lead_id),
        lead_id=lead_id,
    )

    return web.json_response(
        {"lead_id": lead_id, "supplement_id": supplement_id},
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
    app = web.Application(client_max_size=64 * 1024)
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
    app.router.add_get("/data/{filename}", handle_public_data)
    return app
