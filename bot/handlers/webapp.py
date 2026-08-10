import json
import logging

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import Message

from bot import config, texts
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

    if payload.get("attach_tz"):
        await message.answer(texts.LEAD_ACK_ASK_FILE)
        await state.set_state(BriefStates.awaiting_tz_file)
    else:
        await message.answer(texts.LEAD_RECEIVED_ACK)


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
