from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
    WebAppInfo,
)

from bot import config, texts


def main_reply_keyboard(*, is_owner: bool = False) -> ReplyKeyboardMarkup:
    """Постоянная reply-клавиатура под полем ввода — только bot-level
    действия (навигация по самому боту), НЕ запуск Mini App. Прямой запуск
    Mini App теперь идёт через системный Telegram Menu Button
    (MenuButtonWebApp, см. bot/main.py::_setup_menu_button) — отдельной
    постоянной кнопки-триггера для этого здесь больше нет (была
    "🚀 Открыть приложение", убрана явно: дублировала бы Menu Button).

    RULE (не изменилась): ни одна из этих кнопок НЕ KeyboardButton(web_app=...)
    — реальный Telegram (Desktop и Mobile, подтверждено production-тестами)
    не передаёт Telegram.WebApp.initData для Mini App, открытого через
    KeyboardButton.web_app.

    "⌂ Главное меню" — bot/handlers/start.py::main_menu_button (и
    admin-версия в bot/handlers/admin.py — должна побеждать раньше
    AdminStates.*, F.text мастеров, поэтому зарегистрирована отдельно и
    рано в admin.py, а не переиспользует этот же хендлер напрямую).
    "❓ Частые вопросы" — обычный bot-flow (bot/handlers/faq.py), Mini App
    вообще не требует. "⚙️ Админ" показываем только владельцу (is_owner) —
    bot/handlers/admin.py, уже под существующим _is_designer_message.

    is_persistent=True — иначе Telegram-клиент вправе скрыть эту
    reply-клавиатуру после любого сообщения, которое несёт ДРУГОЙ
    reply_markup (а /portfolio, /about, /brief, /faq, "Главное меню"
    confirmation все отвечают inline-клавиатурой — Telegram не позволяет
    одному сообщению нести оба типа сразу). Без этого флага пользователь
    терял постоянную клавиатуру после первой же такой команды (реальный
    production-баг, воспроизведённый после предыдущего деплоя) — с флагом
    клавиатура остаётся видимой независимо от того, что несёт очередное
    сообщение бота."""
    rows = [
        [KeyboardButton(text=texts.MAIN_MENU_BUTTON)],
        [KeyboardButton(text=texts.MENU_FAQ)],
    ]
    if is_owner:
        rows.append([KeyboardButton(text=texts.ADMIN_BUTTON)])
    return ReplyKeyboardMarkup(keyboard=rows, resize_keyboard=True, is_persistent=True)


def reply_keyboard_for_chat(chat_id) -> ReplyKeyboardMarkup:
    """Единая точка выбора client/owner варианта main_reply_keyboard() —
    используется исключительно persistent NAV anchor'ом (см.
    bot/flow.py::ensure_nav_anchor/_create_nav_anchor) — единственным
    сообщением в чате, несущим эту клавиатуру. Та же проверка DESIGNER_CHAT_ID, что и в
    bot/handlers/admin.py::_is_designer_message — не подменяет и не влияет
    на авторизацию (та проверяется отдельно и независимо на роутере и на
    уровне content_store._require_designer), это только выбор "что
    показать", не "что разрешить"."""
    is_owner = bool(config.DESIGNER_CHAT_ID) and str(chat_id) == config.DESIGNER_CHAT_ID
    return main_reply_keyboard(is_owner=is_owner)


def webapp_open_keyboard(webapp_url: str, path: str, label: str) -> InlineKeyboardMarkup:
    """Инлайн-кнопка открытия Mini App — вместе с MenuButtonWebApp один из
    двух типов, которые реально передают initData. Основной путь запуска —
    Menu Button (см. bot/main.py::_setup_menu_button); эта клавиатура —
    fallback/contextual launch: legacy-команды /portfolio, /about, /brief
    (см. bot/handlers/start.py, handlers оставлены рабочими намеренно) и
    bot/handlers/start.py::open_app_button (на случай, если у части
    клиентов ещё закэширована старая reply-клавиатура с кнопкой
    "🚀 Открыть приложение", убранной из новой main_reply_keyboard)."""
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text=label, web_app=WebAppInfo(url=f"{webapp_url}/{path}"))]]
    )


def main_menu_confirm_keyboard() -> InlineKeyboardMarkup:
    """Подтверждение "Главное меню" (см. bot/handlers/start.py::
    main_menu_or_confirm) — показывается только когда есть активное bot/FSM-
    состояние, которое реально может быть потеряно (иначе /start-логика
    выполняется сразу, без этого экрана). Намеренно inline, без отдельного
    FSM-состояния под саму confirmation — исход (confirm/decline) решается
    сразу по callback_data, не требует хранить промежуточный шаг."""
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text=texts.MAIN_MENU_CONFIRM_YES, callback_data="mainmenu:confirm"),
        InlineKeyboardButton(text=texts.MAIN_MENU_CONFIRM_NO, callback_data="mainmenu:decline"),
    ]])


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
