from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
    WebAppInfo,
)

from bot import texts


def main_reply_keyboard(*, is_owner: bool = False) -> ReplyKeyboardMarkup:
    """Постоянная reply-клавиатура под полем ввода — RULE: ни одна из этих
    кнопок НЕ KeyboardButton(web_app=...). Реальный Telegram (Desktop и
    Mobile, подтверждено production-тестами) не передаёт
    Telegram.WebApp.initData для Mini App, открытого через
    KeyboardButton.web_app — поэтому "🚀 Открыть приложение" здесь обычная
    текстовая кнопка-триггер (см. bot/handlers/start.py::open_app_button),
    которая в ОТВЕТ показывает InlineKeyboardButton.web_app
    (webapp_open_keyboard ниже) — единственный подтверждённо рабочий способ
    реального запуска Mini App с identity. "❓ Частые вопросы" — обычный
    bot-flow (bot/handlers/faq.py), Mini App вообще не требует.
    "⚙️ Админ" показываем только владельцу (is_owner) — обрабатывается в
    bot/handlers/admin.py, уже под существующим _is_designer_message.

    is_persistent=True — иначе Telegram-клиент вправе скрыть эту
    reply-клавиатуру после любого сообщения, которое несёт ДРУГОЙ
    reply_markup (а /portfolio, /about, /brief, /faq, "🚀 Открыть
    приложение" все отвечают inline-клавиатурой — Telegram не позволяет
    одному сообщению нести оба типа сразу). Без этого флага пользователь
    терял постоянную клавиатуру после первой же такой команды (реальный
    production-баг, воспроизведённый после предыдущего деплоя) — с флагом
    клавиатура остаётся видимой независимо от того, что несёт очередное
    сообщение бота."""
    rows = [
        [KeyboardButton(text=texts.OPEN_APP_BUTTON)],
        [KeyboardButton(text=texts.MENU_FAQ)],
    ]
    if is_owner:
        rows.append([KeyboardButton(text=texts.ADMIN_BUTTON)])
    return ReplyKeyboardMarkup(keyboard=rows, resize_keyboard=True, is_persistent=True)


def webapp_open_keyboard(webapp_url: str, path: str, label: str) -> InlineKeyboardMarkup:
    """Инлайн-кнопка открытия Mini App — единственный тип кнопки, который
    реально передаёт initData. Используется командами /portfolio, /about,
    /brief, а также ответом на "🚀 Открыть приложение" (см. main_reply_keyboard)."""
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
