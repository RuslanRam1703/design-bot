from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

CASE_FIELD_LABELS = {
    "title": "Название",
    "category": "Категория",
    "cover": "Фото (быстрая замена обложки)",
    "images": "🖼 Изображения",
    "sections": "📑 Разделы кейса",
    "task": "Задача",
    "solution": "Решение",
    "result": "Результат",
    "related_service": "Похожая услуга",
    "external_url": "Ссылка (Behance и т.п., необязательно)",
}

SECTION_TYPE_LABELS = {"text": "📝 Текстовый блок", "gallery": "🖼 Галерея"}

FAQ_FIELD_LABELS = {
    "question": "Вопрос",
    "answer": "Ответ",
}

ABOUT_TEXT_FIELDS = {
    "name": "Имя",
    "tagline": "Тэглайн",
    "location": "Локация",
    "experience_years": "Опыт (лет)",
    "experience_text": "Опыт — описание",
    "approach": "Подход к работе",
}

ABOUT_LIST_FIELDS = {
    "specialization": "Специализация (через запятую)",
    "skills": "Навыки (через запятую)",
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
    # "Меню и навигация" убрана из корня — это техническая настройка
    # видимости экранов, не основной рабочий пункт для владельца бота;
    # сам механизм (adminmenu:nav и связанные хендлеры) не удалён, просто
    # больше не предлагается как равноценный пункт наравне с Заявками/Кейсами.
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="📋 Заявки", callback_data="adminmenu:leads")],
            [InlineKeyboardButton(text="📁 Кейсы", callback_data="adminmenu:cases")],
            [InlineKeyboardButton(text="❓ FAQ", callback_data="adminmenu:faq")],
            [InlineKeyboardButton(text="👤 Обо мне", callback_data="adminmenu:about")],
            [InlineKeyboardButton(text="💰 Услуги и цены", callback_data="adminmenu:pricing")],
            [InlineKeyboardButton(text="🏷 Категории портфолио", callback_data="adminmenu:categories")],
            [InlineKeyboardButton(text="💾 Бэкап", callback_data="adminmenu:backup")],
        ]
    )


def backup_menu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="📤 Экспортировать", callback_data="adminbackupaction:export")],
            [InlineKeyboardButton(text="📥 Восстановить из файла", callback_data="adminbackupaction:import")],
            [InlineKeyboardButton(text="◀️ Назад", callback_data="adminmenu:root")],
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


def case_images_menu_keyboard(images: list[str], cover: str | None) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text=f"{'⭐ ' if img == cover else ''}{img.split('/')[-1]}", callback_data=f"admincaseimgpick:{i}")]
        for i, img in enumerate(images)
    ]
    rows.append([InlineKeyboardButton(text="➕ Добавить изображение", callback_data="admincaseimgaction:add")])
    rows.append([InlineKeyboardButton(text="◀️ Назад к кейсу", callback_data="admincaseimgaction:done")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def case_image_action_keyboard(is_cover: bool) -> InlineKeyboardMarkup:
    rows = []
    if not is_cover:
        rows.append([InlineKeyboardButton(text="⭐ Сделать обложкой", callback_data="admincaseimgact:cover")])
    rows.append([
        InlineKeyboardButton(text="⬆️ Выше", callback_data="admincaseimgact:up"),
        InlineKeyboardButton(text="⬇️ Ниже", callback_data="admincaseimgact:down"),
    ])
    rows.append([InlineKeyboardButton(text="🗑 Удалить", callback_data="admincaseimgact:delete")])
    rows.append([InlineKeyboardButton(text="◀️ Назад к списку", callback_data="admincaseimgact:back")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def case_sections_menu_keyboard(sections: list[dict]) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text=f"{'🖼' if s['type'] == 'gallery' else '📝'} {s['title']}", callback_data=f"admincasesecpick:{i}")]
        for i, s in enumerate(sections)
    ]
    rows.append([InlineKeyboardButton(text="➕ Добавить раздел", callback_data="admincasesecaction:add")])
    rows.append([InlineKeyboardButton(text="◀️ Назад к кейсу", callback_data="admincasesecaction:done")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def case_section_type_keyboard() -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(text=label, callback_data=f"admincasesectype:{key}")] for key, label in SECTION_TYPE_LABELS.items()]
    rows.append([InlineKeyboardButton(text="❌ Отмена", callback_data="admincancel")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def case_section_action_keyboard(section_type: str) -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(text="✏️ Название", callback_data="admincasesecact:title")]]
    if section_type == "gallery":
        rows.append([InlineKeyboardButton(text="➕ Добавить фото", callback_data="admincasesecact:addimg")])
        rows.append([InlineKeyboardButton(text="🗑 Убрать фото", callback_data="admincasesecact:removeimg")])
    else:
        rows.append([InlineKeyboardButton(text="✏️ Текст", callback_data="admincasesecact:content")])
    rows.append([
        InlineKeyboardButton(text="⬆️ Выше", callback_data="admincasesecact:up"),
        InlineKeyboardButton(text="⬇️ Ниже", callback_data="admincasesecact:down"),
    ])
    rows.append([InlineKeyboardButton(text="🗑 Удалить раздел", callback_data="admincasesecact:delete")])
    rows.append([InlineKeyboardButton(text="◀️ Назад к разделам", callback_data="admincasesecact:back")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def section_image_pick_keyboard(images: list[str]) -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(text=img.split("/")[-1], callback_data=f"admincasesecimgpick:{i}")] for i, img in enumerate(images)]
    rows.append([InlineKeyboardButton(text="◀️ Назад", callback_data="admincasesecact:back")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def change_case_category_keyboard(types: list[dict]) -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(text=t["label"], callback_data=f"admincasenewcat:{t['id']}")] for t in types]
    rows.append([InlineKeyboardButton(text="❌ Отмена", callback_data="admincancel")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def about_experience_menu_keyboard(entries: list[dict]) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text=f"{e['role']} — {e['company']}", callback_data=f"adminaboutexppick:{i}")]
        for i, e in enumerate(entries)
    ]
    rows.append([InlineKeyboardButton(text="➕ Добавить запись", callback_data="adminaboutexpaction:add")])
    rows.append([InlineKeyboardButton(text="◀️ Назад", callback_data="admineditabout:done")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def about_experience_entry_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🗑 Удалить запись", callback_data="adminaboutexpentry:delete")],
        [InlineKeyboardButton(text="◀️ Назад к списку", callback_data="adminaboutexpentry:back")],
    ])


def related_service_pick_keyboard(services: list[dict], prefix: str) -> InlineKeyboardMarkup:
    """Ручной выбор "похожей услуги" — переиспользуется и для конкретного
    кейса (prefix="admincaserelservice"), и для категории целиком
    (prefix="admincatrelservice", задаёт дефолт для новых кейсов в ней)."""
    rows = [[InlineKeyboardButton(text=s["name"], callback_data=f"{prefix}:{s['id']}")] for s in services]
    rows.append([InlineKeyboardButton(text="Без привязки", callback_data=f"{prefix}:none")])
    rows.append([InlineKeyboardButton(text="❌ Отмена", callback_data="admincancel")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def faq_pick_keyboard(items: list[dict], prefix: str) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(
            text=f"{'⚠️ ' if i.get('needs_review') else ''}{i['id']}. {i['question'][:45]}",
            callback_data=f"{prefix}:{i['id']}",
        )]
        for i in items
    ]
    rows.append([InlineKeyboardButton(text="◀️ Назад", callback_data="adminmenu:faq")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def faq_field_keyboard() -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(text=label, callback_data=f"admineditfaqfield:{key}")] for key, label in FAQ_FIELD_LABELS.items()]
    rows.append([InlineKeyboardButton(text="◀️ Назад к FAQ", callback_data="admineditfaqfield:done")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def about_field_keyboard(needs_review_fields: list[str] | None = None) -> InlineKeyboardMarkup:
    """needs_review_fields — data/about.json -> needs_review_fields: поля,
    которые всё ещё заглушки (см. README, раздел 7). Помечаем ⚠️ прямо на
    кнопке — раньше это было видно только из README/сырого JSON, не из
    самого /admin."""
    pending = set(needs_review_fields or [])

    def mark(key: str, label: str) -> str:
        return f"⚠️ {label}" if key in pending else label

    rows = [[InlineKeyboardButton(text=mark(key, label), callback_data=f"admineditabout:{key}")] for key, label in ABOUT_TEXT_FIELDS.items()]
    rows += [[InlineKeyboardButton(text=mark(key, label), callback_data=f"admineditabout:{key}")] for key, label in ABOUT_LIST_FIELDS.items()]
    rows.append([InlineKeyboardButton(text="Фото профиля", callback_data="admineditabout:avatar")])
    rows.append([InlineKeyboardButton(text="💼 Опыт работы", callback_data="admineditabout:experience")])
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
            [InlineKeyboardButton(text="🔗 Похожая услуга", callback_data="admincataction:relservice")],
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


LEAD_STATUS_LABELS = {
    "NEW": "🆕 Новая",
    "VIEWED": "👀 Просмотрена",
    "IN_PROGRESS": "💬 В работе",
    "WAITING_CLIENT": "⏸ Ожидание клиента",
    "DONE": "✅ Завершена",
    "CANCELLED": "❌ Отменена",
}
LEAD_FILTER_LABELS = {
    "NEW": "🆕 Новые",
    "VIEWED": "👀 Просмотренные",
    "IN_PROGRESS": "💬 В работе",
    "WAITING_CLIENT": "⏸ Ожидание клиента",
    "DONE": "✅ Завершённые",
    "CANCELLED": "❌ Отменённые",
    "ALL": "Все",
}


def leads_filter_keyboard() -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(text=label, callback_data=f"adminleadfilter:{key}")] for key, label in LEAD_FILTER_LABELS.items()]
    rows.append([InlineKeyboardButton(text="◀️ Назад", callback_data="adminmenu:root")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def leads_list_keyboard(leads: list[dict], status_filter: str) -> InlineKeyboardMarkup:
    rows = []
    for lead in leads:
        service = (lead["payload"].get("service_name") or "без услуги")[:24]
        name = (lead["telegram"].get("first_name") or "клиент")[:16]
        rows.append([InlineKeyboardButton(text=f"#{lead['id']} · {name} · {service}", callback_data=f"adminleadpick:{lead['id']}")])
    rows.append([InlineKeyboardButton(text="🔀 Фильтр", callback_data="adminleadaction:filter")])
    rows.append([InlineKeyboardButton(text="◀️ Назад", callback_data="adminmenu:root")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def lead_detail_keyboard(lead: dict) -> InlineKeyboardMarkup:
    rows = [[
        InlineKeyboardButton(text=("▶ " if lead["status"] == key else "") + label, callback_data=f"adminleadstatus:{key}")
    ] for key, label in LEAD_STATUS_LABELS.items()]
    rows.append([InlineKeyboardButton(text="💬 Ответить через бота", callback_data="adminleadaction:reply")])
    username = lead["telegram"].get("username")
    if username:
        rows.append([InlineKeyboardButton(text="🔗 Открыть в Telegram", url=f"https://t.me/{username}")])
    rows.append([InlineKeyboardButton(text="🗑 Удалить заявку", callback_data="adminleadaction:delete")])
    rows.append([InlineKeyboardButton(text="◀️ К списку заявок", callback_data="adminleadaction:back")])
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
