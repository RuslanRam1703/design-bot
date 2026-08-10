import re

from aiogram import F, Router
from aiogram.types import CallbackQuery, Message

from bot import texts
from bot.data import get_service, load_faq, load_pricing
from bot.keyboards import faq_back_keyboard, faq_list_keyboard, faq_service_picker_keyboard

router = Router(name="faq")


@router.message(F.text == texts.MENU_FAQ)
async def show_faq_list(message: Message) -> None:
    faq_items = load_faq()["faq"]
    await message.answer(texts.FAQ_INTRO, reply_markup=faq_list_keyboard(faq_items))


@router.callback_query(F.data == "faq:back")
async def faq_back(callback: CallbackQuery) -> None:
    faq_items = load_faq()["faq"]
    await callback.message.edit_text(texts.FAQ_INTRO, reply_markup=faq_list_keyboard(faq_items))
    await callback.answer()


@router.callback_query(F.data.regexp(re.compile(r"^faq:(\d+)$")))
async def faq_answer(callback: CallbackQuery) -> None:
    item_id = int(callback.data.split(":", 1)[1])
    faq_items = load_faq()["faq"]
    item = next((i for i in faq_items if i["id"] == item_id), None)
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
    template_item = next(i for i in faq_items if i["type"] == "service_price")
    text = template_item["answer_template"].format(**service)
    await callback.message.edit_text(text, reply_markup=faq_back_keyboard())
    await callback.answer()
