import dataclasses
import json
import logging

from aiogram import F, Router
from aiogram.types import Message

from bot import config, content_store, texts
from bot.calculator import calculate
from bot.data import load_pricing
from bot.lead import format_lead_message, format_material_message

router = Router(name="webapp")
logger = logging.getLogger(__name__)


@router.message(F.web_app_data)
async def handle_webapp_data(message: Message) -> None:
    """Legacy-путь: Telegram.WebApp.sendData() официально работает только
    для Mini App, запущенного через KeyboardButton.web_app
    (https://core.telegram.org/bots/webapps) — мы ушли от этой кнопки ради
    initData (см. bot/keyboards.py), поэтому submitBrief() теперь шлёт
    POST /api/leads (bot/webserver.py::handle_create_lead), а не sendData().
    Хендлер оставлен нетронутым как fallback — на случай, если что-то в
    будущем всё же вызовет sendData() (например, старый закэшированный
    app.js у части клиентов до следующего обращения к серверу)."""
    try:
        payload = json.loads(message.web_app_data.data)
    except (json.JSONDecodeError, TypeError):
        logger.warning("Не удалось разобрать web_app_data: %r", message.web_app_data.data)
        await message.answer("Не получилось обработать данные из мини-приложения.")
        return

    action = payload.get("action")
    if action == "submit_brief":
        await _handle_brief_submission(message, payload)
    else:
        logger.warning("Неизвестное action из webapp: %r", action)


async def _handle_brief_submission(message: Message, payload: dict) -> None:
    calc_result = None
    calc_payload = payload.get("calc")
    if calc_payload and calc_payload.get("service_id"):
        pricing = load_pricing()
        calc_result = calculate(
            pricing=pricing,
            service_id=calc_payload["service_id"],
            selected=calc_payload.get("options", []),
            urgent=bool(calc_payload.get("urgent")),
            complex_=bool(calc_payload.get("complex")),
        )

    user = message.from_user
    # Не требуем username — Telegram user_id уже достаточен как identity и
    # как основной канал ответа через бота (см. /admin -> Заявки).
    telegram_identity = {
        "user_id": user.id,
        "username": user.username,
        "first_name": user.first_name,
        "last_name": user.last_name,
    }
    calc_summary = dataclasses.asdict(calc_result) if calc_result and calc_result.valid else None
    # draft_id — сгенерирован Mini App вместе с черновиком (localStorage) и
    # используется ТОЛЬКО для upsert повторного/дублирующегося submit ОДНОГО
    # И ТОГО ЖЕ ещё не изменённого черновика (не для "Дополнить информацию" —
    # это отдельный supplement-режим, см. bot/webserver.py::
    # _handle_lead_supplement и аудит). add_lead() сама выставляет
    # awaiting_tz_file из payload["attach_tz"] — см. bot/content_store.py,
    # тот же persistent-механизм, что и для основного HTTP-пути
    # (handle_tz_file ниже не различает, откуда взялась заявка).
    lead = content_store.add_lead(payload, telegram_identity, calc_summary, draft_id=payload.get("draft_id"))

    # Уведомляем владельца только при реальном создании — та же логика
    # idempotency, что и в bot/webserver.py::_handle_lead_create (см. аудит).
    if lead["created"]:
        text = format_lead_message(payload, calc_result, lead_id=lead["id"], from_user_id=user.id, username=user.username)
        if config.DESIGNER_CHAT_ID:
            try:
                await message.bot.send_message(chat_id=config.DESIGNER_CHAT_ID, text=text, parse_mode="HTML")
            except Exception:
                logger.exception("Не удалось отправить заявку дизайнеру (DESIGNER_CHAT_ID=%s)", config.DESIGNER_CHAT_ID)
        else:
            logger.warning("DESIGNER_CHAT_ID не задан — заявка не отправлена дизайнеру:\n%s", text)

    price_range = None
    if calc_summary:
        price_range = f"{calc_summary['price_from']:,} – {calc_summary['price_to']:,} ₽".replace(",", " ")

    if payload.get("attach_tz"):
        await message.answer(texts.lead_ack_ask_file(lead["id"], payload.get("service_name"), price_range))
    else:
        await message.answer(texts.lead_received_ack(lead["id"], payload.get("service_name"), price_range))


@router.message(F.document | F.photo)
async def handle_tz_file(message: Message) -> None:
    """Не привязано к FSM-состоянию (раньше — BriefStates.awaiting_tz_file,
    терялось при рестарте бота) — вместо этого ищем заявку САМОГО этого
    отправителя (message.from_user.id — всегда надёжный Telegram-identity,
    как и для создания заявки), которая ждёт файл — см.
    content_store.find_lead_awaiting_file. Чужую заявку так найти
    невозможно: фильтр по user_id, не по произвольному id из сообщения.
    add_lead/add_lead_supplement поддерживают инвариант "максимум одна
    ожидающая заявка на клиента" (см. content_store._clear_other_awaiting и
    аудит) — при нескольких заявках одного клиента файл не может уйти не
    туда. Если ждущей заявки нет — просто ничего не делаем, документ/фото
    не наш случай (например, админ шлёт архив бэкапа — это отдельный,
    FSM-scoped хендлер в admin.py, который стоит раньше в порядке
    роутеров и перехватит его первым)."""
    lead = content_store.find_lead_awaiting_file(message.from_user.id)
    if lead is None:
        return

    if message.document:
        file_id, file_unique_id, kind = message.document.file_id, message.document.file_unique_id, "document"
    else:
        photo = message.photo[-1]
        file_id, file_unique_id, kind = photo.file_id, photo.file_unique_id, "photo"
    source = lead.get("awaiting_tz_file_source") or "new"
    # Материал сохраняем ДО пересылки — если пересылка упадёт (Telegram API
    # недоступен и т.п.), связь "файл ↔ заявка" всё равно не теряется, её
    # можно будет восстановить вручную через file_id из /admin.
    content_store.record_lead_material(lead["id"], file_id, file_unique_id, kind, source)

    if config.DESIGNER_CHAT_ID:
        try:
            # message.forward() не умеет добавлять caption (ограничение Bot
            # API) — номер заявки уходит отдельным сообщением перед форвардом,
            # иначе пересланный файл в чате владельца ничем не отличался бы
            # от материала любой другой заявки (см. аудит).
            await message.bot.send_message(
                chat_id=config.DESIGNER_CHAT_ID, text=format_material_message(lead["id"]), parse_mode="HTML",
            )
            await message.forward(chat_id=config.DESIGNER_CHAT_ID)
        except Exception:
            logger.exception("Не удалось переслать материал дизайнеру (lead #%s)", lead["id"])
    await message.answer(texts.TZ_FILE_FORWARDED)
