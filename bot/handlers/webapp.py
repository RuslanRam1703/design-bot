import dataclasses
import json
import logging

from aiogram import F, Router
from aiogram.types import Message

from bot import config, content_store, texts
from bot.calculator import calculate
from bot.data import load_pricing
from bot.lead import format_lead_message

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
    text = format_lead_message(payload, calc_result, from_user_id=user.id, username=user.username)

    if config.DESIGNER_CHAT_ID:
        try:
            await message.bot.send_message(chat_id=config.DESIGNER_CHAT_ID, text=text, parse_mode="HTML")
        except Exception:
            logger.exception("Не удалось отправить заявку дизайнеру (DESIGNER_CHAT_ID=%s)", config.DESIGNER_CHAT_ID)
    else:
        logger.warning("DESIGNER_CHAT_ID не задан — заявка не отправлена дизайнеру:\n%s", text)

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
    # переживает "Дополнить информацию": повторная отправка того же
    # черновика ОБНОВЛЯЕТ заявку вместо создания дубликата (см. Part 7 ТЗ).
    # add_lead() сама выставляет awaiting_tz_file из payload["attach_tz"] —
    # см. bot/content_store.py, тот же persistent-механизм, что и для
    # основного HTTP-пути (handle_tz_file ниже не различает, откуда взялась
    # заявка).
    lead = content_store.add_lead(payload, telegram_identity, calc_summary, draft_id=payload.get("draft_id"))

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
    Если ждущей заявки нет — просто ничего не делаем, документ/фото не
    наш случай (например, админ шлёт архив бэкапа — это отдельный,
    FSM-scoped хендлер в admin.py, который стоит раньше в порядке
    роутеров и перехватит его первым)."""
    lead = content_store.find_lead_awaiting_file(message.from_user.id)
    if lead is None:
        return
    if config.DESIGNER_CHAT_ID:
        try:
            await message.forward(chat_id=config.DESIGNER_CHAT_ID)
        except Exception:
            logger.exception("Не удалось переслать файл ТЗ дизайнеру (lead #%s)", lead["id"])
    content_store.mark_tz_file_received(lead["id"])
    await message.answer(texts.TZ_FILE_FORWARDED)
