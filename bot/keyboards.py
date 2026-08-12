from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
    WebAppInfo,
)

from bot import texts


def main_menu_keyboard(webapp_url: str, ui_config: dict | None = None) -> ReplyKeyboardMarkup:
    """ui_config — data/ui_config.json (см. content_store.get_ui_config), задаёт,
    какие пункты меню включены. Порядок кнопок фиксирован, отключённые просто
    выпадают из раскладки, а не оставляют пустое место."""
    menu = (ui_config or {}).get("menu", {})

    def enabled(key: str) -> bool:
        return menu.get(key, True)

    buttons: list[KeyboardButton] = []
    if enabled("portfolio"):
        buttons.append(KeyboardButton(text=texts.MENU_PORTFOLIO, web_app=WebAppInfo(url=f"{webapp_url}/portfolio")))
    if enabled("about"):
        buttons.append(KeyboardButton(text=texts.MENU_ABOUT, web_app=WebAppInfo(url=f"{webapp_url}/about")))
    if enabled("calculator"):
        buttons.append(KeyboardButton(text=texts.MENU_CALCULATOR, web_app=WebAppInfo(url=f"{webapp_url}/calculator")))
    if enabled("brief"):
        buttons.append(KeyboardButton(text=texts.MENU_BRIEF, web_app=WebAppInfo(url=f"{webapp_url}/brief")))
    if enabled("faq"):
        buttons.append(KeyboardButton(text=texts.MENU_FAQ))

    rows = [buttons[i:i + 2] for i in range(0, len(buttons), 2)]
    if not rows:
        # Полностью пустое меню оставлять нельзя — портфолио как минимум
        # всегда доступно, иначе бот выглядит сломанным.
        rows = [[KeyboardButton(text=texts.MENU_PORTFOLIO, web_app=WebAppInfo(url=f"{webapp_url}/portfolio"))]]

    return ReplyKeyboardMarkup(keyboard=rows, resize_keyboard=True)


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


def faq_price_answer_keyboard(faq_item_id: int) -> InlineKeyboardMarkup:
    """"Назад" с ответа про цену конкретной услуги — возвращает к выбору
    услуги (предыдущий шаг), а не сразу к полному списку FAQ, как это было
    бы с faq_back_keyboard(). faq_item_id — id вопроса типа service_price,
    повторный клик по нему в faq_answer() заново рисует список услуг."""
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text=texts.FAQ_BACK_TO_SERVICES, callback_data=f"faq:{faq_item_id}")]]
    )
