import re

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from bot import flow, texts
from bot.data import get_service, load_faq, load_pricing
from bot.keyboards import (
    faq_back_keyboard,
    faq_list_keyboard,
    faq_price_answer_keyboard,
    faq_service_picker_keyboard,
)

router = Router(name="faq")


def _client_faq_items() -> list[dict]:
    """Пункты needs_review — черновики, ответ которых буквально "уточняется
    у дизайнера" (см. data/faq.json). Дизайнеру они видны в /admin с
    пометкой ⚠️, но клиенту их показывать не нужно: вопрос, который сам
    отправляет "напишите дизайнеру напрямую", противоречит цели FAQ —
    ответить без переписки."""
    return [i for i in load_faq()["faq"] if not i.get("needs_review")]


async def _send_faq_list(message: Message, state: FSMContext) -> None:
    # flow.open_root (не голый message.answer) — FAQ-список сам по себе
    # "корневой экран без FSM-состояния", в точности как /portfolio, /about,
    # /brief, /admin (см. open_root docstring). Раньше этот список никогда
    # не регистрировался как текущий anchor — "⌂ Главное меню"/"/start"
    # после FAQ физически не могли найти его, чтобы удалить (см. UX-аудит,
    # regression после 88acc40 — сам разрыв существовал с первого коммита,
    # просто не был так заметен без persistent-кнопки "вернуться в корень").
    # Внутри самого FAQ (faq_back/faq_answer/faq_price_answer ниже) сообщение
    # редактируется на месте (callback.message.edit_text) — тот же anchor,
    # тот же message_id, повторно регистрировать не нужно. reply-клавиатура
    # "освежается" автоматически внутри open_flow (см. bot/flow.py) —
    # отдельный вызов здесь больше не нужен (и раньше выполнялся уже ПОСЛЕ
    # удаления предыдущего anchor — источник отдельного regression, см. аудит).
    await flow.open_root(message, state, texts.FAQ_INTRO, faq_list_keyboard(_client_faq_items()))


@router.message(F.text == texts.MENU_FAQ)
async def show_faq_list(message: Message, state: FSMContext) -> None:
    await _send_faq_list(message, state)


@router.message(Command("faq"))
async def cmd_faq(message: Message, state: FSMContext) -> None:
    await _send_faq_list(message, state)


@router.callback_query(F.data == "faq:back")
async def faq_back(callback: CallbackQuery) -> None:
    await callback.message.edit_text(texts.FAQ_INTRO, reply_markup=faq_list_keyboard(_client_faq_items()))
    await callback.answer()


@router.callback_query(F.data.regexp(re.compile(r"^faq:(\d+)$")))
async def faq_answer(callback: CallbackQuery) -> None:
    item_id = int(callback.data.split(":", 1)[1])
    # needs_review исключены и здесь — не только из списка выше — на случай
    # устаревшей клавиатуры в старом сообщении, которое клиент мог не закрыть.
    item = next((i for i in _client_faq_items() if i["id"] == item_id), None)
    if item is None:
        await callback.answer("Вопрос не найден", show_alert=True)
        return

    if item["type"] == "service_price":
        pricing = load_pricing()
        await callback.message.edit_text(texts.FAQ_PICK_SERVICE, reply_markup=faq_service_picker_keyboard(pricing["services"]))
    else:
        await callback.message.edit_text(item["answer"], reply_markup=faq_back_keyboard())
    await callback.answer()


@router.callback_query(F.data.regexp(re.compile(r"^faqprice:(.+)$")))
async def faq_price_answer(callback: CallbackQuery) -> None:
    service_id = callback.data.split(":", 1)[1]
    pricing = load_pricing()
    service = get_service(pricing, service_id)
    if service is None:
        await callback.answer("Услуга не найдена", show_alert=True)
        return

    faq_items = load_faq()["faq"]
    # next(..., None) — тот же graceful-паттерн, что и в faq_answer выше
    # (P1-3, Batch 12): шаблонный вопрос service_price мог быть удалён из
    # /admin -> FAQ между показом пикера услуг и нажатием клиента (TOCTOU) —
    # раньше next(...) без default падал с StopIteration.
    template_item = next((i for i in faq_items if i["type"] == "service_price"), None)
    if template_item is None:
        await callback.answer("Вопрос не найден", show_alert=True)
        return
    text = template_item["answer_template"].format(**service)
    await callback.message.edit_text(text, reply_markup=faq_price_answer_keyboard(template_item["id"]))
    await callback.answer()
