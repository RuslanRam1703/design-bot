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


@router.message(Command("admin"))
async def cmd_admin(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer("Админ-меню:", reply_markup=kb.admin_root_keyboard())


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
    await callback.message.edit_text("Название кейса (текстом):")
    await state.set_state(AdminStates.add_case_title)
    await callback.answer()


@router.message(AdminStates.add_case_title, F.text)
async def cases_add_title(message: Message, state: FSMContext) -> None:
    case_id = content_store.next_case_id()
    await state.update_data(title=message.text.strip(), case_id=case_id)
    await message.answer("Пришлите фото кейса (как фото):")
    await state.set_state(AdminStates.add_case_photo)


@router.message(AdminStates.add_case_photo, F.photo | F.document)
async def cases_add_photo(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    file_id = message.photo[-1].file_id if message.photo else message.document.file_id
    cover = await content_store.save_case_photo(message.chat.id, message.bot, file_id, data["case_id"])
    await state.update_data(cover=cover)
    await message.answer("Короткое описание задачи (пара предложений):")
    await state.set_state(AdminStates.add_case_description)


@router.message(AdminStates.add_case_photo)
async def cases_add_photo_wrong(message: Message) -> None:
    await message.answer("Нужно фото 📎 (или /cancel, если передумали).")


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
    await callback.message.edit_text(prompt)
    await state.set_state(AdminStates.edit_case_value)
    await callback.answer()


@router.message(AdminStates.edit_case_value)
async def cases_edit_value(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    field = data["field"]
    if field == "cover":
        if not (message.photo or message.document):
            await message.answer("Нужно фото 📎.")
            return
        file_id = message.photo[-1].file_id if message.photo else message.document.file_id
        value = await content_store.save_case_photo(message.chat.id, message.bot, file_id, data["case_id"])
        content_store.update_case(message.chat.id, data["case_id"], cover=value)
    else:
        if not message.text:
            await message.answer("Нужен текст.")
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
    await callback.message.edit_text("Текст вопроса:")
    await state.set_state(AdminStates.add_faq_question)
    await callback.answer()


@router.message(AdminStates.add_faq_question, F.text)
async def faq_add_question(message: Message, state: FSMContext) -> None:
    await state.update_data(question=message.text.strip())
    await message.answer("Текст ответа:")
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
    await callback.message.edit_text("Новый текст:")
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
        await callback.message.edit_text("Пришлите новое фото профиля:")
        await state.set_state(AdminStates.edit_about_photo)
    elif field in kb.ABOUT_LIST_FIELDS:
        await callback.message.edit_text(f"{kb.ABOUT_LIST_FIELDS[field]}:")
        await state.set_state(AdminStates.edit_about_value)
    else:
        await callback.message.edit_text(f"{kb.ABOUT_TEXT_FIELDS[field]}:")
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
    await message.answer("Нужно фото 📎.")


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
