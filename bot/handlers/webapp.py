import dataclasses
import json
import logging

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import Message

from bot import config, content_store, texts
from bot.calculator import calculate
from bot.data import load_pricing
from bot.lead import format_lead_message
from bot.states import BriefStates

router = Router(name="webapp")
logger = logging.getLogger(__name__)


@router.message(F.web_app_data)
async def handle_webapp_data(message: Message, state: FSMContext) -> None:
    try:
        payload = json.loads(message.web_app_data.data)
    except (json.JSONDecodeError, TypeError):
        logger.warning("Не удалось разобрать web_app_data: %r", message.web_app_data.data)
        await message.answer("Не получилось обработать данные из мини-приложения.")
        return

    action = payload.get("action")
    if action == "submit_brief":
        await _handle_brief_submission(message, state, payload)
    else:
        logger.warning("Неизвестное action из webapp: %r", action)


async def _handle_brief_submission(message: Message, state: FSMContext, payload: dict) -> None:
    # Сбрасываем любое старое ожидание файла ТЗ до принятия решения по этой
    # заявке — иначе повторная отправка брифа без "пришлю файл" оставляла
    # BriefStates.awaiting_tz_file висеть от предыдущей заявки, и все
    # следующие сообщения клиента перехватывались как файл ТЗ.
    await state.clear()

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
    lead = content_store.add_lead(payload, telegram_identity, calc_summary, draft_id=payload.get("draft_id"))

    price_range = None
    if calc_summary:
        price_range = f"{calc_summary['price_from']:,} – {calc_summary['price_to']:,} ₽".replace(",", " ")

    if payload.get("attach_tz"):
        await message.answer(texts.lead_ack_ask_file(lead["id"], payload.get("service_name"), price_range))
        await state.set_state(BriefStates.awaiting_tz_file)
    else:
        await message.answer(texts.lead_received_ack(lead["id"], payload.get("service_name"), price_range))


@router.message(BriefStates.awaiting_tz_file, F.document | F.photo)
async def handle_tz_file(message: Message, state: FSMContext) -> None:
    if config.DESIGNER_CHAT_ID:
        try:
            await message.forward(chat_id=config.DESIGNER_CHAT_ID)
        except Exception:
            logger.exception("Не удалось переслать файл ТЗ дизайнеру")
    await message.answer(texts.TZ_FILE_FORWARDED)
    await state.clear()


@router.message(BriefStates.awaiting_tz_file)
async def handle_tz_file_wrong_type(message: Message) -> None:
    await message.answer(texts.TZ_FILE_EXPECTED)
