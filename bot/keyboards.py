from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    WebAppInfo,
)

from bot import texts


def main_entry_keyboard(webapp_url: str) -> InlineKeyboardMarkup:
    """Основная точка входа в Mini App — используется /start, /cancel и
    текстовым fallback. Inline WebApp-кнопка, НЕ reply-клавиатура: реальный
    Telegram (Desktop и Mobile, подтверждено production-логами) не передаёт
    Telegram.WebApp.initData для Mini App, открытого через
    KeyboardButton.web_app — только через inline-кнопку, Menu Button
    (bot/main.py::_setup_menu_button) или slash-команду с inline-кнопкой
    (/portfolio, /about, /brief — webapp_open_keyboard ниже). Раньше здесь
    была reply-клавиатура (main_menu_keyboard) — она полностью удалена как
    entry point в Mini App, а не оставлена дублирующим путём с другим
    уровнем авторизации."""
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🚀 Открыть приложение", web_app=WebAppInfo(url=f"{webapp_url}/portfolio"))],
        [InlineKeyboardButton(text=texts.MENU_BRIEF, web_app=WebAppInfo(url=f"{webapp_url}/brief"))],
    ])


def webapp_open_keyboard(webapp_url: str, path: str, label: str) -> InlineKeyboardMarkup:
    """Инлайн-кнопка открытия Mini App — используется командами /portfolio,
    /about, /brief, а также main_entry_keyboard (основная точка входа)."""
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text=label, web_app=WebAppInfo(url=f"{webapp_url}/{path}"))]]
    )


def faq_list_keyboard(faq_items: list[dict]) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text=f"{item['id']}. {item['question']}", callback_data=f"faq:{item['id']}")]
        for item in faq_items
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def faq_back_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text=texts.FAQ_BACK, callback_data="faq:back")]]
    )


def faq_service_picker_keyboard(services: list[dict]) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text=s["name"], callback_data=f"faqprice:{s['id']}")]
        for s in services
    ]
    rows.append([InlineKeyboardButton(text=texts.FAQ_BACK, callback_data="faq:back")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def faq_price_answer_keyboard(faq_item_id: int) -> InlineKeyboardMarkup:
    """"Назад" с ответа про цену конкретной услуги — возвращает к выбору
    услуги (предыдущий шаг), а не сразу к полному списку FAQ, как это было
    бы с faq_back_keyboard(). faq_item_id — id вопроса типа service_price,
    повторный клик по нему в faq_answer() заново рисует список услуг."""
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text=texts.FAQ_BACK_TO_SERVICES, callback_data=f"faq:{faq_item_id}")]]
    )
