"""Админ-режим для дизайнера (см. ТЗ, раздел 8): добавление/редактирование/
удаление кейсов портфолио и FAQ, редактирование текста "Обо мне" — прямо
из переписки с ботом, без доступа к коду и без пересборки.

Доступ гарантированно только у DESIGNER_CHAT_ID — см. router.message.filter
и router.callback_query.filter ниже (применяются ко всем хендлерам файла) и
дополнительно content_store._require_designer на каждой мутирующей функции.

Навигация двухуровневая: /admin -> раздел (Кейсы / FAQ / Обо мне) ->
действие (Добавить / Редактировать / Удалить). Callback "adminmenu:*"
универсален — используется отовсюду как "назад" к разделу или к корню.
"""

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from bot import admin_keyboards as kb
from bot import config, content_store
from bot.states import AdminStates

router = Router(name="admin")


def _is_designer_message(message: Message) -> bool:
    return bool(config.DESIGNER_CHAT_ID) and str(message.chat.id) == config.DESIGNER_CHAT_ID


def _is_designer_callback(callback: CallbackQuery) -> bool:
    return bool(config.DESIGNER_CHAT_ID) and str(callback.message.chat.id) == config.DESIGNER_CHAT_ID


router.message.filter(_is_designer_message)
router.callback_query.filter(_is_designer_callback)


def _parse_number(text: str) -> float | None:
    try:
        return float(text.strip().replace(",", "."))
    except ValueError:
        return None


@router.message(Command("admin"))
async def cmd_admin(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer("Админ-меню:", reply_markup=kb.admin_root_keyboard())


@router.callback_query(F.data == "admincancel")
async def admin_cancel(callback: CallbackQuery, state: FSMContext) -> None:
    """Универсальная кнопка "Отмена" на любом шаге, где ждём свободный
    текст/фото — без нее пришлось бы заново набирать /admin."""
    await state.clear()
    await callback.message.edit_text("Отменено. Админ-меню:", reply_markup=kb.admin_root_keyboard())
    await callback.answer()


# ---- Навигация между разделами (используется как "назад" отовсюду) ----

@router.callback_query(F.data == "adminmenu:root")
async def menu_root(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await callback.message.edit_text("Админ-меню:", reply_markup=kb.admin_root_keyboard())
    await callback.answer()


@router.callback_query(F.data == "adminmenu:cases")
async def menu_cases(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await callback.message.edit_text("Кейсы портфолио:", reply_markup=kb.admin_cases_menu_keyboard())
    await callback.answer()


@router.callback_query(F.data == "adminmenu:faq")
async def menu_faq(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await callback.message.edit_text("FAQ:", reply_markup=kb.admin_faq_menu_keyboard())
    await callback.answer()


@router.callback_query(F.data == "adminmenu:pricing")
async def menu_pricing(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await callback.message.edit_text("Услуги и цены:", reply_markup=kb.pricing_menu_keyboard())
    await callback.answer()


@router.callback_query(F.data == "adminmenu:categories")
async def menu_categories(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await callback.message.edit_text("Категории портфолио:", reply_markup=kb.categories_menu_keyboard())
    await callback.answer()


@router.callback_query(F.data == "adminmenu:nav")
async def menu_nav(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    ui_config = content_store.get_ui_config()
    await callback.message.edit_text(
        "Меню и навигация — нажмите пункт, чтобы включить/выключить:",
        reply_markup=kb.nav_menu_keyboard(ui_config),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("adminnavtoggle:"))
async def nav_toggle(callback: CallbackQuery, state: FSMContext) -> None:
    key = callback.data.split(":", 1)[1]
    current = content_store.get_ui_config()["menu"].get(key, True)
    content_store.set_menu_item_enabled(callback.message.chat.id, key, not current)
    updated = content_store.get_ui_config()
    await callback.message.edit_text(
        "Меню и навигация — нажмите пункт, чтобы включить/выключить:",
        reply_markup=kb.nav_menu_keyboard(updated),
    )
    await callback.answer()


@router.callback_query(F.data == "adminmenu:about")
async def menu_about(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.message.edit_text("Что изменить в разделе «Обо мне»?", reply_markup=kb.about_field_keyboard())
    await state.set_state(AdminStates.edit_about_field_pick)
    await callback.answer()


# ---- Кейсы: действия ----

@router.callback_query(F.data == "admincasesaction:add")
async def cases_add_start(callback: CallbackQuery, state: FSMContext) -> None:
    types = content_store.list_portfolio_types()
    await callback.message.edit_text("Категория нового кейса:", reply_markup=kb.category_pick_keyboard(types))
    await state.set_state(AdminStates.add_case_category)
    await callback.answer()


@router.callback_query(AdminStates.add_case_category, F.data.startswith("admincat:"))
async def cases_add_category(callback: CallbackQuery, state: FSMContext) -> None:
    type_id = callback.data.split(":", 1)[1]
    await state.update_data(type_id=type_id)
    await callback.message.edit_text("Название кейса (текстом):", reply_markup=kb.cancel_keyboard())
    await state.set_state(AdminStates.add_case_title)
    await callback.answer()


@router.message(AdminStates.add_case_title, F.text)
async def cases_add_title(message: Message, state: FSMContext) -> None:
    case_id = content_store.next_case_id()
    await state.update_data(title=message.text.strip(), case_id=case_id)
    await message.answer("Пришлите фото кейса (как фото):", reply_markup=kb.cancel_keyboard())
    await state.set_state(AdminStates.add_case_photo)


@router.message(AdminStates.add_case_photo, F.photo | F.document)
async def cases_add_photo(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    file_id = message.photo[-1].file_id if message.photo else message.document.file_id
    cover = await content_store.save_case_photo(message.chat.id, message.bot, file_id, data["case_id"])
    await state.update_data(cover=cover)
    await message.answer("Короткое описание задачи (пара предложений):", reply_markup=kb.cancel_keyboard())
    await state.set_state(AdminStates.add_case_description)


@router.message(AdminStates.add_case_photo)
async def cases_add_photo_wrong(message: Message) -> None:
    await message.answer("Нужно фото 📎.", reply_markup=kb.cancel_keyboard())


@router.message(AdminStates.add_case_description, F.text)
async def cases_add_description(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    case = content_store.add_case(
        message.chat.id,
        case_id=data["case_id"],
        title=data["title"],
        type_id=data["type_id"],
        cover=data["cover"],
        task=message.text.strip(),
        related_service=content_store.TYPE_TO_SERVICE.get(data["type_id"]),
    )
    await state.clear()
    await message.answer(
        f"Кейс «{case['title']}» добавлен и уже виден в Mini App ✅\n\n"
        "Поля «Решение» и «Результат» пока пустые — заполнить можно через «✏️ Редактировать».",
        reply_markup=kb.admin_cases_menu_keyboard(),
    )


@router.callback_query(F.data == "admincasesaction:edit")
async def cases_edit_start(callback: CallbackQuery, state: FSMContext) -> None:
    cases = content_store.list_cases()
    if not cases:
        await callback.message.edit_text("Кейсов пока нет.", reply_markup=kb.admin_cases_menu_keyboard())
        await callback.answer()
        return
    await callback.message.edit_text("Какой кейс редактировать?", reply_markup=kb.case_pick_keyboard(cases, "admineditcase"))
    await state.set_state(AdminStates.edit_case_pick)
    await callback.answer()


@router.callback_query(AdminStates.edit_case_pick, F.data.startswith("admineditcase:"))
async def cases_edit_picked(callback: CallbackQuery, state: FSMContext) -> None:
    case_id = callback.data.split(":", 1)[1]
    await state.update_data(case_id=case_id)
    await callback.message.edit_text("Что изменить?", reply_markup=kb.case_field_keyboard())
    await state.set_state(AdminStates.edit_case_field_pick)
    await callback.answer()


@router.callback_query(AdminStates.edit_case_field_pick, F.data.startswith("admineditfield:"))
async def cases_edit_field(callback: CallbackQuery, state: FSMContext) -> None:
    field = callback.data.split(":", 1)[1]
    if field == "done":
        await state.clear()
        await callback.message.edit_text("Кейсы портфолио:", reply_markup=kb.admin_cases_menu_keyboard())
        await callback.answer()
        return
    await state.update_data(field=field)
    prompt = "Пришлите новое фото:" if field == "cover" else "Пришлите новый текст:"
    await callback.message.edit_text(prompt, reply_markup=kb.cancel_keyboard())
    await state.set_state(AdminStates.edit_case_value)
    await callback.answer()


@router.message(AdminStates.edit_case_value)
async def cases_edit_value(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    field = data["field"]
    if field == "cover":
        if not (message.photo or message.document):
            await message.answer("Нужно фото 📎.", reply_markup=kb.cancel_keyboard())
            return
        file_id = message.photo[-1].file_id if message.photo else message.document.file_id
        value = await content_store.save_case_photo(message.chat.id, message.bot, file_id, data["case_id"])
        content_store.update_case(message.chat.id, data["case_id"], cover=value)
    else:
        if not message.text:
            await message.answer("Нужен текст.", reply_markup=kb.cancel_keyboard())
            return
        content_store.update_case(message.chat.id, data["case_id"], **{field: message.text.strip()})
    await message.answer("Обновлено ✅\n\nЧто ещё изменить?", reply_markup=kb.case_field_keyboard())
    await state.set_state(AdminStates.edit_case_field_pick)


@router.callback_query(F.data == "admincasesaction:delete")
async def cases_delete_start(callback: CallbackQuery, state: FSMContext) -> None:
    cases = content_store.list_cases()
    if not cases:
        await callback.message.edit_text("Кейсов пока нет.", reply_markup=kb.admin_cases_menu_keyboard())
        await callback.answer()
        return
    await callback.message.edit_text("Какой кейс удалить?", reply_markup=kb.case_pick_keyboard(cases, "admindelcase"))
    await state.set_state(AdminStates.delete_case_pick)
    await callback.answer()


@router.callback_query(AdminStates.delete_case_pick, F.data.startswith("admindelcase:"))
async def cases_delete_confirm(callback: CallbackQuery, state: FSMContext) -> None:
    case_id = callback.data.split(":", 1)[1]
    case = next((c for c in content_store.list_cases() if c["id"] == case_id), None)
    await state.update_data(case_id=case_id)
    title = case["title"] if case else case_id
    await callback.message.edit_text(f"Удалить кейс «{title}»? Это необратимо.", reply_markup=kb.confirm_keyboard("admindelcaseconfirm"))
    await state.set_state(AdminStates.delete_case_confirm)
    await callback.answer()


@router.callback_query(AdminStates.delete_case_confirm, F.data.startswith("admindelcaseconfirm:"))
async def cases_delete_do(callback: CallbackQuery, state: FSMContext) -> None:
    answer = callback.data.split(":", 1)[1]
    data = await state.get_data()
    if answer == "yes":
        content_store.delete_case(callback.message.chat.id, data["case_id"])
        text = "Кейс удалён ✅"
    else:
        text = "Отменено."
    await state.clear()
    await callback.message.edit_text(text, reply_markup=kb.admin_cases_menu_keyboard())
    await callback.answer()


# ---- FAQ: действия ----

@router.callback_query(F.data == "adminfaqaction:add")
async def faq_add_start(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.message.edit_text("Текст вопроса:", reply_markup=kb.cancel_keyboard())
    await state.set_state(AdminStates.add_faq_question)
    await callback.answer()


@router.message(AdminStates.add_faq_question, F.text)
async def faq_add_question(message: Message, state: FSMContext) -> None:
    await state.update_data(question=message.text.strip())
    await message.answer("Текст ответа:", reply_markup=kb.cancel_keyboard())
    await state.set_state(AdminStates.add_faq_answer)


@router.message(AdminStates.add_faq_answer, F.text)
async def faq_add_answer(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    item = content_store.add_faq(message.chat.id, data["question"], message.text.strip())
    await state.clear()
    await message.answer(f"Вопрос №{item['id']} добавлен ✅", reply_markup=kb.admin_faq_menu_keyboard())


@router.callback_query(F.data == "adminfaqaction:edit")
async def faq_edit_start(callback: CallbackQuery, state: FSMContext) -> None:
    items = content_store.list_faq()
    await callback.message.edit_text("Какой вопрос редактировать?", reply_markup=kb.faq_pick_keyboard(items, "admineditfaq"))
    await state.set_state(AdminStates.edit_faq_pick)
    await callback.answer()


@router.callback_query(AdminStates.edit_faq_pick, F.data.startswith("admineditfaq:"))
async def faq_edit_picked(callback: CallbackQuery, state: FSMContext) -> None:
    faq_id = int(callback.data.split(":", 1)[1])
    await state.update_data(faq_id=faq_id)
    await callback.message.edit_text("Что изменить?", reply_markup=kb.faq_field_keyboard())
    await state.set_state(AdminStates.edit_faq_field_pick)
    await callback.answer()


@router.callback_query(AdminStates.edit_faq_field_pick, F.data.startswith("admineditfaqfield:"))
async def faq_edit_field(callback: CallbackQuery, state: FSMContext) -> None:
    field = callback.data.split(":", 1)[1]
    if field == "done":
        await state.clear()
        await callback.message.edit_text("FAQ:", reply_markup=kb.admin_faq_menu_keyboard())
        await callback.answer()
        return
    await state.update_data(field=field)
    await callback.message.edit_text("Новый текст:", reply_markup=kb.cancel_keyboard())
    await state.set_state(AdminStates.edit_faq_value)
    await callback.answer()


@router.message(AdminStates.edit_faq_value, F.text)
async def faq_edit_value(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    content_store.update_faq(message.chat.id, data["faq_id"], **{data["field"]: message.text.strip()})
    await message.answer("Обновлено ✅\n\nЧто ещё изменить?", reply_markup=kb.faq_field_keyboard())
    await state.set_state(AdminStates.edit_faq_field_pick)


@router.callback_query(F.data == "adminfaqaction:delete")
async def faq_delete_start(callback: CallbackQuery, state: FSMContext) -> None:
    items = content_store.list_faq()
    await callback.message.edit_text("Какой вопрос удалить?", reply_markup=kb.faq_pick_keyboard(items, "admindelfaq"))
    await state.set_state(AdminStates.delete_faq_pick)
    await callback.answer()


@router.callback_query(AdminStates.delete_faq_pick, F.data.startswith("admindelfaq:"))
async def faq_delete_confirm(callback: CallbackQuery, state: FSMContext) -> None:
    faq_id = int(callback.data.split(":", 1)[1])
    await state.update_data(faq_id=faq_id)
    await callback.message.edit_text(f"Удалить вопрос №{faq_id}? Это необратимо.", reply_markup=kb.confirm_keyboard("admindelfaqconfirm"))
    await state.set_state(AdminStates.delete_faq_confirm)
    await callback.answer()


@router.callback_query(AdminStates.delete_faq_confirm, F.data.startswith("admindelfaqconfirm:"))
async def faq_delete_do(callback: CallbackQuery, state: FSMContext) -> None:
    answer = callback.data.split(":", 1)[1]
    data = await state.get_data()
    if answer == "yes":
        content_store.delete_faq(callback.message.chat.id, data["faq_id"])
        text = "Вопрос удалён ✅"
    else:
        text = "Отменено."
    await state.clear()
    await callback.message.edit_text(text, reply_markup=kb.admin_faq_menu_keyboard())
    await callback.answer()


# ---- Обо мне: редактирование полей ----

@router.callback_query(AdminStates.edit_about_field_pick, F.data.startswith("admineditabout:"))
async def about_edit_field(callback: CallbackQuery, state: FSMContext) -> None:
    field = callback.data.split(":", 1)[1]
    if field == "done":
        await state.clear()
        await callback.message.edit_text("Админ-меню:", reply_markup=kb.admin_root_keyboard())
        await callback.answer()
        return
    await state.update_data(field=field)
    if field == "avatar":
        await callback.message.edit_text("Пришлите новое фото профиля:", reply_markup=kb.cancel_keyboard())
        await state.set_state(AdminStates.edit_about_photo)
    elif field in kb.ABOUT_LIST_FIELDS:
        await callback.message.edit_text(f"{kb.ABOUT_LIST_FIELDS[field]}:", reply_markup=kb.cancel_keyboard())
        await state.set_state(AdminStates.edit_about_value)
    else:
        await callback.message.edit_text(f"{kb.ABOUT_TEXT_FIELDS[field]}:", reply_markup=kb.cancel_keyboard())
        await state.set_state(AdminStates.edit_about_value)
    await callback.answer()


@router.message(AdminStates.edit_about_photo, F.photo | F.document)
async def about_edit_photo(message: Message, state: FSMContext) -> None:
    file_id = message.photo[-1].file_id if message.photo else message.document.file_id
    path = await content_store.save_about_photo(message.chat.id, message.bot, file_id)
    content_store.update_about_field(message.chat.id, "avatar", path)
    await message.answer("Фото обновлено ✅\n\nЧто ещё изменить?", reply_markup=kb.about_field_keyboard())
    await state.set_state(AdminStates.edit_about_field_pick)


@router.message(AdminStates.edit_about_photo)
async def about_edit_photo_wrong(message: Message) -> None:
    await message.answer("Нужно фото 📎.", reply_markup=kb.cancel_keyboard())


@router.message(AdminStates.edit_about_value, F.text)
async def about_edit_value(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    field = data["field"]
    if field in kb.ABOUT_LIST_FIELDS:
        values = [v.strip() for v in message.text.split(",") if v.strip()]
        content_store.update_about_field(message.chat.id, field, values)
    else:
        content_store.update_about_field(message.chat.id, field, message.text.strip())
    await message.answer("Обновлено ✅\n\nЧто ещё изменить?", reply_markup=kb.about_field_keyboard())
    await state.set_state(AdminStates.edit_about_field_pick)


# ---- Добавить услугу ----

@router.callback_query(F.data == "adminpriceaction:add")
async def price_add_start(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.message.edit_text("Название новой услуги:", reply_markup=kb.cancel_keyboard())
    await state.set_state(AdminStates.add_service_name)
    await callback.answer()


@router.message(AdminStates.add_service_name, F.text)
async def price_add_name(message: Message, state: FSMContext) -> None:
    await state.update_data(name=message.text.strip())
    await message.answer("Базовая цена, ₽ (число):", reply_markup=kb.cancel_keyboard())
    await state.set_state(AdminStates.add_service_price)


@router.message(AdminStates.add_service_price, F.text)
async def price_add_price(message: Message, state: FSMContext) -> None:
    value = _parse_number(message.text)
    if value is None:
        await message.answer("Нужно число, например 25000. Попробуйте ещё раз:", reply_markup=kb.cancel_keyboard())
        return
    await state.update_data(base_price=value)
    await message.answer("Минимальный срок, дней (число):", reply_markup=kb.cancel_keyboard())
    await state.set_state(AdminStates.add_service_term_min)


@router.message(AdminStates.add_service_term_min, F.text)
async def price_add_term_min(message: Message, state: FSMContext) -> None:
    value = _parse_number(message.text)
    if value is None:
        await message.answer("Нужно число. Попробуйте ещё раз:", reply_markup=kb.cancel_keyboard())
        return
    await state.update_data(term_min=value)
    await message.answer("Максимальный срок, дней (число):", reply_markup=kb.cancel_keyboard())
    await state.set_state(AdminStates.add_service_term_max)


@router.message(AdminStates.add_service_term_max, F.text)
async def price_add_term_max(message: Message, state: FSMContext) -> None:
    value = _parse_number(message.text)
    if value is None:
        await message.answer("Нужно число. Попробуйте ещё раз:", reply_markup=kb.cancel_keyboard())
        return
    await state.update_data(term_max=value)
    await message.answer("Что входит в базовую стоимость (коротко текстом):", reply_markup=kb.cancel_keyboard())
    await state.set_state(AdminStates.add_service_includes)


@router.message(AdminStates.add_service_includes, F.text)
async def price_add_includes(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    service_id = content_store.next_service_id()
    service = content_store.add_service(
        message.chat.id,
        service_id=service_id,
        name=data["name"],
        base_price=data["base_price"],
        term_min=data["term_min"],
        term_max=data["term_max"],
        includes=message.text.strip(),
    )
    await state.clear()
    await message.answer(f"Услуга «{service['name']}» добавлена ✅", reply_markup=kb.pricing_menu_keyboard())


# ---- Редактировать услугу (+ опции внутри) ----

@router.callback_query(F.data == "adminpriceaction:edit")
async def price_edit_start(callback: CallbackQuery, state: FSMContext) -> None:
    services = content_store.list_services()
    await callback.message.edit_text("Какую услугу редактировать?", reply_markup=kb.service_pick_keyboard(services, "admineditservice"))
    await state.set_state(AdminStates.edit_service_pick)
    await callback.answer()


@router.callback_query(AdminStates.edit_service_pick, F.data.startswith("admineditservice:"))
async def price_edit_picked(callback: CallbackQuery, state: FSMContext) -> None:
    service_id = callback.data.split(":", 1)[1]
    await state.update_data(service_id=service_id)
    await callback.message.edit_text("Что изменить?", reply_markup=kb.service_field_keyboard())
    await state.set_state(AdminStates.edit_service_field_pick)
    await callback.answer()


@router.callback_query(AdminStates.edit_service_field_pick, F.data.startswith("admineditservicefield:"))
async def price_edit_field(callback: CallbackQuery, state: FSMContext) -> None:
    field = callback.data.split(":", 1)[1]
    if field == "done":
        await state.clear()
        await callback.message.edit_text("Услуги и цены:", reply_markup=kb.pricing_menu_keyboard())
        await callback.answer()
        return
    if field == "options":
        await callback.message.edit_text("Опции этой услуги:", reply_markup=kb.options_menu_keyboard())
        await callback.answer()
        return
    await state.update_data(field=field)
    await callback.message.edit_text(f"Новое значение поля «{kb.SERVICE_FIELD_LABELS[field]}»:", reply_markup=kb.cancel_keyboard())
    await state.set_state(AdminStates.edit_service_value)
    await callback.answer()


@router.message(AdminStates.edit_service_value, F.text)
async def price_edit_value(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    field = data["field"]
    if field in ("base_price", "term_min", "term_max"):
        value = _parse_number(message.text)
        if value is None:
            await message.answer("Нужно число. Попробуйте ещё раз:", reply_markup=kb.cancel_keyboard())
            return
        content_store.update_service(message.chat.id, data["service_id"], **{field: value})
    else:
        content_store.update_service(message.chat.id, data["service_id"], **{field: message.text.strip()})
    await message.answer("Обновлено ✅\n\nЧто изменить?", reply_markup=kb.service_field_keyboard())
    await state.set_state(AdminStates.edit_service_field_pick)


# ---- Удалить услугу ----

@router.callback_query(F.data == "adminpriceaction:delete")
async def price_delete_start(callback: CallbackQuery, state: FSMContext) -> None:
    services = content_store.list_services()
    await callback.message.edit_text("Какую услугу удалить?", reply_markup=kb.service_pick_keyboard(services, "admindelservice"))
    await state.set_state(AdminStates.delete_service_pick)
    await callback.answer()


@router.callback_query(AdminStates.delete_service_pick, F.data.startswith("admindelservice:"))
async def price_delete_confirm(callback: CallbackQuery, state: FSMContext) -> None:
    service_id = callback.data.split(":", 1)[1]
    service = content_store.get_service(service_id)
    await state.update_data(service_id=service_id)
    name = service["name"] if service else service_id
    await callback.message.edit_text(
        f"Удалить услугу «{name}»? Вместе с ней удалятся все её опции. Это необратимо.",
        reply_markup=kb.confirm_keyboard("admindelserviceconfirm"),
    )
    await state.set_state(AdminStates.delete_service_confirm)
    await callback.answer()


@router.callback_query(AdminStates.delete_service_confirm, F.data.startswith("admindelserviceconfirm:"))
async def price_delete_do(callback: CallbackQuery, state: FSMContext) -> None:
    answer = callback.data.split(":", 1)[1]
    data = await state.get_data()
    if answer == "yes":
        content_store.delete_service(callback.message.chat.id, data["service_id"])
        text = "Услуга удалена ✅"
    else:
        text = "Отменено."
    await state.clear()
    await callback.message.edit_text(text, reply_markup=kb.pricing_menu_keyboard())
    await callback.answer()


# ---- Коэффициенты и округление ----

@router.callback_query(F.data == "adminpriceaction:coef")
async def price_coef_start(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.message.edit_text("Что изменить?", reply_markup=kb.coefficients_menu_keyboard())
    await state.set_state(AdminStates.edit_coefficients_pick)
    await callback.answer()


@router.callback_query(AdminStates.edit_coefficients_pick, F.data.startswith("admineditcoef:"))
async def price_coef_pick(callback: CallbackQuery, state: FSMContext) -> None:
    key = callback.data.split(":", 1)[1]
    await state.update_data(kind="coef", key=key)
    await callback.message.edit_text(f"Новое значение для «{kb.COEFFICIENT_LABELS[key]}» (например 1.25):", reply_markup=kb.cancel_keyboard())
    await state.set_state(AdminStates.edit_coefficients_value)
    await callback.answer()


@router.callback_query(AdminStates.edit_coefficients_pick, F.data.startswith("admineditround:"))
async def price_round_pick(callback: CallbackQuery, state: FSMContext) -> None:
    key = callback.data.split(":", 1)[1]
    await state.update_data(kind="round", key=key)
    await callback.message.edit_text(f"Новое значение для «{kb.ROUNDING_LABELS[key]}»:", reply_markup=kb.cancel_keyboard())
    await state.set_state(AdminStates.edit_coefficients_value)
    await callback.answer()


@router.message(AdminStates.edit_coefficients_value, F.text)
async def price_coef_value(message: Message, state: FSMContext) -> None:
    value = _parse_number(message.text)
    if value is None:
        await message.answer("Нужно число. Попробуйте ещё раз:", reply_markup=kb.cancel_keyboard())
        return
    data = await state.get_data()
    if data["kind"] == "coef":
        content_store.update_coefficient(message.chat.id, data["key"], value)
    else:
        content_store.update_rounding(message.chat.id, data["key"], value)
    await message.answer("Обновлено ✅\n\nЧто ещё изменить?", reply_markup=kb.coefficients_menu_keyboard())
    await state.set_state(AdminStates.edit_coefficients_pick)


# ---- Опции услуги (вложены в редактирование услуги — service_id уже в data) ----

@router.callback_query(AdminStates.edit_service_field_pick, F.data.startswith("adminoptaction:"))
async def option_action(callback: CallbackQuery, state: FSMContext) -> None:
    action = callback.data.split(":", 1)[1]
    if action == "back":
        await callback.message.edit_text("Что изменить?", reply_markup=kb.service_field_keyboard())
        await callback.answer()
        return

    data = await state.get_data()
    service_id = data["service_id"]

    if action == "add":
        await callback.message.edit_text("Название новой опции:", reply_markup=kb.cancel_keyboard())
        await state.set_state(AdminStates.option_add_name)
    elif action == "edit":
        options = content_store.list_options(service_id)
        if not options:
            await callback.message.edit_text("У этой услуги пока нет опций.", reply_markup=kb.options_menu_keyboard())
            await callback.answer()
            return
        await callback.message.edit_text("Какую опцию редактировать?", reply_markup=kb.option_pick_keyboard(options, "admineditoption"))
        await state.set_state(AdminStates.option_edit_pick)
    elif action == "delete":
        options = content_store.list_options(service_id)
        if not options:
            await callback.message.edit_text("У этой услуги пока нет опций.", reply_markup=kb.options_menu_keyboard())
            await callback.answer()
            return
        await callback.message.edit_text("Какую опцию удалить?", reply_markup=kb.option_pick_keyboard(options, "admindeloption"))
        await state.set_state(AdminStates.option_delete_pick)
    await callback.answer()


@router.callback_query(AdminStates.option_edit_pick, F.data == "adminoptaction:tomenu")
@router.callback_query(AdminStates.option_delete_pick, F.data == "adminoptaction:tomenu")
async def option_back_to_menu(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.message.edit_text("Опции этой услуги:", reply_markup=kb.options_menu_keyboard())
    await state.set_state(AdminStates.edit_service_field_pick)
    await callback.answer()


@router.message(AdminStates.option_add_name, F.text)
async def option_add_name(message: Message, state: FSMContext) -> None:
    await state.update_data(opt_name=message.text.strip())
    await message.answer("Цена опции, +₽ (число):", reply_markup=kb.cancel_keyboard())
    await state.set_state(AdminStates.option_add_price)


@router.message(AdminStates.option_add_price, F.text)
async def option_add_price(message: Message, state: FSMContext) -> None:
    value = _parse_number(message.text)
    if value is None:
        await message.answer("Нужно число. Попробуйте ещё раз:", reply_markup=kb.cancel_keyboard())
        return
    await state.update_data(opt_price=value)
    await message.answer("Срок опции, +дней (число, можно дробное, например 0.5):", reply_markup=kb.cancel_keyboard())
    await state.set_state(AdminStates.option_add_days)


@router.message(AdminStates.option_add_days, F.text)
async def option_add_days(message: Message, state: FSMContext) -> None:
    value = _parse_number(message.text)
    if value is None:
        await message.answer("Нужно число. Попробуйте ещё раз:", reply_markup=kb.cancel_keyboard())
        return
    await state.update_data(opt_days=value)
    await message.answer(
        "Можно выбирать эту опцию несколько раз (умножается на количество)?",
        reply_markup=kb.yes_no_keyboard("adminoptmultipliable"),
    )
    await state.set_state(AdminStates.option_add_multipliable)


@router.callback_query(AdminStates.option_add_multipliable, F.data.startswith("adminoptmultipliable:"))
async def option_add_multipliable(callback: CallbackQuery, state: FSMContext) -> None:
    multipliable = callback.data.split(":", 1)[1] == "yes"
    data = await state.get_data()
    service_id = data["service_id"]
    option_id = content_store.next_option_id(service_id)
    content_store.add_option(
        callback.message.chat.id,
        option_id=option_id,
        service_id=service_id,
        name=data["opt_name"],
        price=data["opt_price"],
        days=data["opt_days"],
        multipliable=multipliable,
    )
    await callback.message.edit_text("Опция добавлена ✅\n\nОпции этой услуги:", reply_markup=kb.options_menu_keyboard())
    await state.set_state(AdminStates.edit_service_field_pick)
    await callback.answer()


@router.callback_query(AdminStates.option_edit_pick, F.data.startswith("admineditoption:"))
async def option_edit_picked(callback: CallbackQuery, state: FSMContext) -> None:
    option_id = callback.data.split(":", 1)[1]
    await state.update_data(option_id=option_id)
    await callback.message.edit_text("Что изменить?", reply_markup=kb.option_field_keyboard())
    await state.set_state(AdminStates.option_edit_field_pick)
    await callback.answer()


@router.callback_query(AdminStates.option_edit_field_pick, F.data.startswith("admineditoptionfield:"))
async def option_edit_field(callback: CallbackQuery, state: FSMContext) -> None:
    field = callback.data.split(":", 1)[1]
    if field == "done":
        await callback.message.edit_text("Опции этой услуги:", reply_markup=kb.options_menu_keyboard())
        await state.set_state(AdminStates.edit_service_field_pick)
        await callback.answer()
        return
    await state.update_data(field=field)
    if field == "multipliable":
        await callback.message.edit_text("Множится на количество?", reply_markup=kb.yes_no_keyboard("adminoptfieldbool"))
    else:
        await callback.message.edit_text(f"Новое значение поля «{kb.OPTION_FIELD_LABELS[field]}»:", reply_markup=kb.cancel_keyboard())
    await state.set_state(AdminStates.option_edit_value)
    await callback.answer()


@router.callback_query(AdminStates.option_edit_value, F.data.startswith("adminoptfieldbool:"))
async def option_edit_value_bool(callback: CallbackQuery, state: FSMContext) -> None:
    value = callback.data.split(":", 1)[1] == "yes"
    data = await state.get_data()
    content_store.update_option(callback.message.chat.id, data["option_id"], multipliable=value)
    await callback.message.edit_text("Обновлено ✅\n\nЧто изменить?", reply_markup=kb.option_field_keyboard())
    await state.set_state(AdminStates.option_edit_field_pick)
    await callback.answer()


@router.message(AdminStates.option_edit_value, F.text)
async def option_edit_value_text(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    field = data["field"]
    if field in ("price", "days"):
        value = _parse_number(message.text)
        if value is None:
            await message.answer("Нужно число. Попробуйте ещё раз:", reply_markup=kb.cancel_keyboard())
            return
        content_store.update_option(message.chat.id, data["option_id"], **{field: value})
    else:
        content_store.update_option(message.chat.id, data["option_id"], **{field: message.text.strip()})
    await message.answer("Обновлено ✅\n\nЧто изменить?", reply_markup=kb.option_field_keyboard())
    await state.set_state(AdminStates.option_edit_field_pick)


@router.callback_query(AdminStates.option_delete_pick, F.data.startswith("admindeloption:"))
async def option_delete_confirm(callback: CallbackQuery, state: FSMContext) -> None:
    option_id = callback.data.split(":", 1)[1]
    data = await state.get_data()
    option = next((o for o in content_store.list_options(data["service_id"]) if o["id"] == option_id), None)
    await state.update_data(option_id=option_id)
    name = option["name"] if option else option_id
    await callback.message.edit_text(f"Удалить опцию «{name}»? Это необратимо.", reply_markup=kb.confirm_keyboard("admindeloptionconfirm"))
    await state.set_state(AdminStates.option_delete_confirm)
    await callback.answer()


@router.callback_query(AdminStates.option_delete_confirm, F.data.startswith("admindeloptionconfirm:"))
async def option_delete_do(callback: CallbackQuery, state: FSMContext) -> None:
    answer = callback.data.split(":", 1)[1]
    data = await state.get_data()
    if answer == "yes":
        content_store.delete_option(callback.message.chat.id, data["option_id"])
        text = "Опция удалена ✅"
    else:
        text = "Отменено."
    await callback.message.edit_text(f"{text}\n\nОпции этой услуги:", reply_markup=kb.options_menu_keyboard())
    await state.set_state(AdminStates.edit_service_field_pick)
    await callback.answer()


# ---- Категории портфолио ----

@router.callback_query(F.data == "admincataction:add")
async def cat_add_start(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.message.edit_text("Название новой категории:", reply_markup=kb.cancel_keyboard())
    await state.set_state(AdminStates.add_category_label)
    await callback.answer()


@router.message(AdminStates.add_category_label, F.text)
async def cat_add_label(message: Message, state: FSMContext) -> None:
    type_id = content_store.next_portfolio_type_id()
    cat = content_store.add_portfolio_type(message.chat.id, type_id=type_id, label=message.text.strip())
    await state.clear()
    await message.answer(f"Категория «{cat['label']}» добавлена ✅", reply_markup=kb.categories_menu_keyboard())


@router.callback_query(F.data == "admincataction:rename")
async def cat_rename_start(callback: CallbackQuery, state: FSMContext) -> None:
    types = content_store.list_portfolio_types()
    await callback.message.edit_text("Какую категорию переименовать?", reply_markup=kb.category_manage_pick_keyboard(types, "adminrenamecat"))
    await state.set_state(AdminStates.rename_category_pick)
    await callback.answer()


@router.callback_query(AdminStates.rename_category_pick, F.data.startswith("adminrenamecat:"))
async def cat_rename_picked(callback: CallbackQuery, state: FSMContext) -> None:
    type_id = callback.data.split(":", 1)[1]
    await state.update_data(type_id=type_id)
    await callback.message.edit_text("Новое название категории:", reply_markup=kb.cancel_keyboard())
    await state.set_state(AdminStates.rename_category_value)
    await callback.answer()


@router.message(AdminStates.rename_category_value, F.text)
async def cat_rename_value(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    content_store.rename_portfolio_type(message.chat.id, data["type_id"], message.text.strip())
    await state.clear()
    await message.answer("Переименовано ✅", reply_markup=kb.categories_menu_keyboard())


@router.callback_query(F.data == "admincataction:delete")
async def cat_delete_start(callback: CallbackQuery, state: FSMContext) -> None:
    types = content_store.list_portfolio_types()
    await callback.message.edit_text("Какую категорию удалить?", reply_markup=kb.category_manage_pick_keyboard(types, "admindelcat"))
    await state.set_state(AdminStates.delete_category_pick)
    await callback.answer()


@router.callback_query(AdminStates.delete_category_pick, F.data.startswith("admindelcat:"))
async def cat_delete_confirm(callback: CallbackQuery, state: FSMContext) -> None:
    type_id = callback.data.split(":", 1)[1]
    in_use = content_store.count_cases_with_type(type_id)
    if in_use > 0:
        await callback.message.edit_text(
            f"Нельзя удалить — категория используется в {in_use} кейс(ах). "
            "Сначала перенесите эти кейсы в другую категорию (редактирование кейса) или удалите их.",
            reply_markup=kb.categories_menu_keyboard(),
        )
        await state.clear()
        await callback.answer()
        return
    await state.update_data(type_id=type_id)
    await callback.message.edit_text("Удалить категорию? Это необратимо.", reply_markup=kb.confirm_keyboard("admindelcatconfirm"))
    await state.set_state(AdminStates.delete_category_confirm)
    await callback.answer()


@router.callback_query(AdminStates.delete_category_confirm, F.data.startswith("admindelcatconfirm:"))
async def cat_delete_do(callback: CallbackQuery, state: FSMContext) -> None:
    answer = callback.data.split(":", 1)[1]
    data = await state.get_data()
    if answer == "yes":
        content_store.delete_portfolio_type(callback.message.chat.id, data["type_id"])
        text = "Категория удалена ✅"
    else:
        text = "Отменено."
    await state.clear()
    await callback.message.edit_text(text, reply_markup=kb.categories_menu_keyboard())
    await callback.answer()
