from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
    WebAppInfo,
)

from bot import texts


def main_menu_keyboard(webapp_url: str) -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [
                KeyboardButton(
                    text=texts.MENU_PORTFOLIO,
                    web_app=WebAppInfo(url=f"{webapp_url}/portfolio"),
                ),
                KeyboardButton(
                    text=texts.MENU_ABOUT,
                    web_app=WebAppInfo(url=f"{webapp_url}/about"),
                ),
            ],
            [
                KeyboardButton(
                    text=texts.MENU_CALCULATOR,
                    web_app=WebAppInfo(url=f"{webapp_url}/calculator"),
                ),
                KeyboardButton(
                    text=texts.MENU_BRIEF,
                    web_app=WebAppInfo(url=f"{webapp_url}/brief"),
                ),
            ],
            [
                KeyboardButton(text=texts.MENU_FAQ),
            ],
        ],
        resize_keyboard=True,
    )


def webapp_open_keyboard(webapp_url: str, path: str, label: str) -> InlineKeyboardMarkup:
    """Инлайн-кнопка открытия Mini App — используется командами /portfolio,
    /about, /calculator, /brief (реплай-кнопки в main_menu_keyboard делают
    то же самое, это дублирующий вход через меню команд)."""
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
