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


# ---- Навигация: 2 уровня — раздел (Кейсы/FAQ/Обо мне) -> действие ----

def admin_root_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="📁 Кейсы", callback_data="adminmenu:cases")],
            [InlineKeyboardButton(text="❓ FAQ", callback_data="adminmenu:faq")],
            [InlineKeyboardButton(text="👤 Обо мне", callback_data="adminmenu:about")],
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


def confirm_keyboard(prefix: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="Да, удалить", callback_data=f"{prefix}:yes"),
                InlineKeyboardButton(text="Отмена", callback_data=f"{prefix}:no"),
            ]
        ]
    )
