from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

CASE_FIELD_LABELS = {
    "title": "Название",
    "cover": "Фото",
    "task": "Задача",
    "solution": "Решение",
    "result": "Результат",
}

FAQ_FIELD_LABELS = {
    "question": "Вопрос",
    "answer": "Ответ",
}

ABOUT_TEXT_FIELDS = {
    "name": "Имя",
    "tagline": "Тэглайн",
    "experience_years": "Опыт (лет)",
    "experience_text": "Опыт — описание",
    "approach": "Подход к работе",
}

ABOUT_LIST_FIELDS = {
    "specialization": "Специализация (через запятую)",
    "tools": "Инструменты (через запятую)",
}

SERVICE_FIELD_LABELS = {
    "name": "Название",
    "base_price": "Базовая цена",
    "term_min": "Срок мин (дней)",
    "term_max": "Срок макс (дней)",
    "includes": "Что входит",
}

OPTION_FIELD_LABELS = {
    "name": "Название",
    "price": "Цена, +₽",
    "days": "Срок, +дней",
    "multipliable": "Множится на количество",
}

COEFFICIENT_LABELS = {
    "urgent": "Срочный проект — множитель",
    "complex": "Высокая сложность — множитель",
}

ROUNDING_LABELS = {
    "price_from_factor": "Нижняя граница вилки (доля от точной цены)",
    "price_to_factor": "Верхняя граница вилки (доля от точной цены)",
    "round_to": "Шаг округления, ₽",
}

MENU_ITEM_LABELS = {
    "portfolio": "📁 Портфолио",
    "about": "👤 Обо мне",
    "calculator": "💰 Калькулятор",
    "brief": "✍️ Заявка",
    "faq": "❓ Частые вопросы",
}


# ---- Навигация: 2 уровня — раздел -> действие ----

def admin_root_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="📁 Кейсы", callback_data="adminmenu:cases")],
            [InlineKeyboardButton(text="❓ FAQ", callback_data="adminmenu:faq")],
            [InlineKeyboardButton(text="👤 Обо мне", callback_data="adminmenu:about")],
            [InlineKeyboardButton(text="💰 Услуги и цены", callback_data="adminmenu:pricing")],
            [InlineKeyboardButton(text="🏷 Категории портфолио", callback_data="adminmenu:categories")],
            [InlineKeyboardButton(text="🧭 Меню и навигация", callback_data="adminmenu:nav")],
        ]
    )


def admin_cases_menu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="➕ Добавить", callback_data="admincasesaction:add")],
            [InlineKeyboardButton(text="✏️ Редактировать", callback_data="admincasesaction:edit")],
            [InlineKeyboardButton(text="🗑 Удалить", callback_data="admincasesaction:delete")],
            [InlineKeyboardButton(text="◀️ Назад", callback_data="adminmenu:root")],
        ]
    )


def admin_faq_menu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="➕ Добавить", callback_data="adminfaqaction:add")],
            [InlineKeyboardButton(text="✏️ Редактировать", callback_data="adminfaqaction:edit")],
            [InlineKeyboardButton(text="🗑 Удалить", callback_data="adminfaqaction:delete")],
            [InlineKeyboardButton(text="◀️ Назад", callback_data="adminmenu:root")],
        ]
    )


def category_pick_keyboard(types: list[dict]) -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(text=t["label"], callback_data=f"admincat:{t['id']}")] for t in types]
    rows.append([InlineKeyboardButton(text="◀️ Назад", callback_data="adminmenu:cases")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def case_pick_keyboard(cases: list[dict], prefix: str) -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(text=c["title"], callback_data=f"{prefix}:{c['id']}")] for c in cases]
    rows.append([InlineKeyboardButton(text="◀️ Назад", callback_data="adminmenu:cases")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def case_field_keyboard() -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(text=label, callback_data=f"admineditfield:{key}")] for key, label in CASE_FIELD_LABELS.items()]
    rows.append([InlineKeyboardButton(text="◀️ Назад к кейсам", callback_data="admineditfield:done")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def faq_pick_keyboard(items: list[dict], prefix: str) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text=f"{i['id']}. {i['question'][:45]}", callback_data=f"{prefix}:{i['id']}")]
        for i in items
    ]
    rows.append([InlineKeyboardButton(text="◀️ Назад", callback_data="adminmenu:faq")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def faq_field_keyboard() -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(text=label, callback_data=f"admineditfaqfield:{key}")] for key, label in FAQ_FIELD_LABELS.items()]
    rows.append([InlineKeyboardButton(text="◀️ Назад к FAQ", callback_data="admineditfaqfield:done")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def about_field_keyboard() -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(text=label, callback_data=f"admineditabout:{key}")] for key, label in ABOUT_TEXT_FIELDS.items()]
    rows += [[InlineKeyboardButton(text=label, callback_data=f"admineditabout:{key}")] for key, label in ABOUT_LIST_FIELDS.items()]
    rows.append([InlineKeyboardButton(text="Фото профиля", callback_data="admineditabout:avatar")])
    rows.append([InlineKeyboardButton(text="◀️ Назад", callback_data="admineditabout:done")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def pricing_menu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="➕ Добавить услугу", callback_data="adminpriceaction:add")],
            [InlineKeyboardButton(text="✏️ Редактировать услугу", callback_data="adminpriceaction:edit")],
            [InlineKeyboardButton(text="🗑 Удалить услугу", callback_data="adminpriceaction:delete")],
            [InlineKeyboardButton(text="⚙️ Коэффициенты и округление", callback_data="adminpriceaction:coef")],
            [InlineKeyboardButton(text="◀️ Назад", callback_data="adminmenu:root")],
        ]
    )


def service_pick_keyboard(services: list[dict], prefix: str) -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(text=s["name"], callback_data=f"{prefix}:{s['id']}")] for s in services]
    rows.append([InlineKeyboardButton(text="◀️ Назад", callback_data="adminmenu:pricing")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def service_field_keyboard() -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(text=label, callback_data=f"admineditservicefield:{key}")] for key, label in SERVICE_FIELD_LABELS.items()]
    rows.append([InlineKeyboardButton(text="🧩 Опции услуги", callback_data="admineditservicefield:options")])
    rows.append([InlineKeyboardButton(text="◀️ Назад к услугам", callback_data="admineditservicefield:done")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def options_menu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="➕ Добавить опцию", callback_data="adminoptaction:add")],
            [InlineKeyboardButton(text="✏️ Редактировать опцию", callback_data="adminoptaction:edit")],
            [InlineKeyboardButton(text="🗑 Удалить опцию", callback_data="adminoptaction:delete")],
            [InlineKeyboardButton(text="◀️ Назад к услуге", callback_data="adminoptaction:back")],
        ]
    )


def option_pick_keyboard(options: list[dict], prefix: str) -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(text=o["name"], callback_data=f"{prefix}:{o['id']}")] for o in options]
    rows.append([InlineKeyboardButton(text="◀️ Назад", callback_data="adminoptaction:tomenu")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def option_field_keyboard() -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(text=label, callback_data=f"admineditoptionfield:{key}")] for key, label in OPTION_FIELD_LABELS.items()]
    rows.append([InlineKeyboardButton(text="◀️ Назад к опциям", callback_data="admineditoptionfield:done")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def yes_no_keyboard(prefix: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="Да", callback_data=f"{prefix}:yes"),
                InlineKeyboardButton(text="Нет", callback_data=f"{prefix}:no"),
            ]
        ]
    )


def coefficients_menu_keyboard() -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(text=label, callback_data=f"admineditcoef:{key}")] for key, label in COEFFICIENT_LABELS.items()]
    rows += [[InlineKeyboardButton(text=label, callback_data=f"admineditround:{key}")] for key, label in ROUNDING_LABELS.items()]
    rows.append([InlineKeyboardButton(text="◀️ Назад", callback_data="adminmenu:pricing")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def categories_menu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="➕ Добавить категорию", callback_data="admincataction:add")],
            [InlineKeyboardButton(text="✏️ Переименовать", callback_data="admincataction:rename")],
            [InlineKeyboardButton(text="🗑 Удалить", callback_data="admincataction:delete")],
            [InlineKeyboardButton(text="◀️ Назад", callback_data="adminmenu:root")],
        ]
    )


def category_manage_pick_keyboard(types: list[dict], prefix: str) -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(text=t["label"], callback_data=f"{prefix}:{t['id']}")] for t in types]
    rows.append([InlineKeyboardButton(text="◀️ Назад", callback_data="adminmenu:categories")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def nav_menu_keyboard(ui_config: dict) -> InlineKeyboardMarkup:
    rows = []
    for key, label in MENU_ITEM_LABELS.items():
        enabled = ui_config["menu"].get(key, True)
        mark = "✅" if enabled else "⬜"
        rows.append([InlineKeyboardButton(text=f"{mark} {label}", callback_data=f"adminnavtoggle:{key}")])
    rows.append([InlineKeyboardButton(text="◀️ Назад", callback_data="adminmenu:root")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def cancel_keyboard() -> InlineKeyboardMarkup:
    """Вешается на любой запрос свободного текста/фото в админке — без неё
    пользователь, передумав посреди ввода, не может выйти иначе как заново
    вызвать /admin. Всегда возвращает в корень меню."""
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="❌ Отмена", callback_data="admincancel")]])


def confirm_keyboard(prefix: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="Да, удалить", callback_data=f"{prefix}:yes"),
                InlineKeyboardButton(text="Отмена", callback_data=f"{prefix}:no"),
            ]
        ]
    )
