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

import logging
import uuid
import zipfile
from datetime import datetime, timezone
from typing import Callable

from aiogram import F, Router
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State
from aiogram.types import BufferedInputFile, CallbackQuery, InlineKeyboardMarkup, Message

from bot import admin_keyboards as kb
from bot import config, content_store, flow, texts
from bot import lead as lead_format
from bot.handlers.start import main_menu_or_confirm
from bot.states import AdminStates

router = Router(name="admin")
logger = logging.getLogger(__name__)

# Куда возвращает универсальная "❌ Отмена" (cancel_keyboard) — по значению
# cancel_to, проставленному в state.data в момент входа в конкретный мастер
# (см. _resolve_cancel). Раньше "Отмена" всегда вела в корень /admin, из-за
# чего на 3-м уровне вложенности (опции услуги) отмена выбрасывала мимо и
# услуги, и раздела "Услуги и цены" — не как "Готово", которое всегда
# возвращает к непосредственному родителю.
CANCEL_TARGETS: dict[str, tuple[str, Callable[[], InlineKeyboardMarkup]]] = {
    "cases": ("Отменено. Кейсы портфолио:", kb.admin_cases_menu_keyboard),
    "faq": ("Отменено. FAQ:", kb.admin_faq_menu_keyboard),
    "pricing": ("Отменено. Услуги и цены:", kb.pricing_menu_keyboard),
    "categories": ("Отменено. Категории портфолио:", kb.categories_menu_keyboard),
    "backup": ("Отменено. Бэкап:", kb.backup_menu_keyboard),
    "root": ("Отменено. Админ-меню:", kb.admin_root_keyboard),
}


async def _resolve_cancel(data: dict) -> tuple[str, InlineKeyboardMarkup, State | None, dict]:
    target = data.get("cancel_to")
    if target == "options" and data.get("service_id"):
        return (
            "Отменено. Опции этой услуги:",
            kb.options_menu_keyboard(),
            AdminStates.edit_service_field_pick,
            {"service_id": data["service_id"]},
        )
    if target == "sections" and data.get("case_id"):
        case = next((c for c in await content_store.list_cases() if c["id"] == data["case_id"]), None)
        return (
            "Отменено. Разделы кейса:",
            kb.case_sections_menu_keyboard(case.get("sections", []) if case else []),
            AdminStates.case_sections_menu,
            {"case_id": data["case_id"]},
        )
    if target == "images" and data.get("case_id"):
        case = next((c for c in await content_store.list_cases() if c["id"] == data["case_id"]), None)
        return (
            "Отменено. Изображения кейса:",
            kb.case_images_menu_keyboard(case.get("images", []) if case else [], case.get("cover") if case else None),
            AdminStates.case_images_menu,
            {"case_id": data["case_id"]},
        )
    text, kb_builder = CANCEL_TARGETS.get(target, CANCEL_TARGETS["root"])
    return text, kb_builder(), None, {}


def _is_designer_message(message: Message) -> bool:
    return bool(config.DESIGNER_CHAT_ID) and str(message.chat.id) == config.DESIGNER_CHAT_ID


def _is_designer_callback(callback: CallbackQuery) -> bool:
    return bool(config.DESIGNER_CHAT_ID) and str(callback.message.chat.id) == config.DESIGNER_CHAT_ID


router.message.filter(_is_designer_message)
router.callback_query.filter(_is_designer_callback)


def _parse_number(text: str, *, min_value: float = 0) -> float | None:
    """min_value=0 по умолчанию — цены, сроки, коэффициенты и параметры
    округления не имеют смысла отрицательными; опечатка админа не должна
    напрямую попадать в клиент-facing цену (см. UX-аудит, находка F12)."""
    try:
        value = float(text.strip().replace(",", "."))
    except ValueError:
        return None
    if value < min_value:
        return None
    return value


async def _admin_root_text() -> str:
    """Корень /admin с сводкой контента, ещё не готового к показу клиентам
    (см. UX-аудит, находки F01-F03) — needs_review/заглушки уже
    отслеживались по отдельности, но нигде не были собраны в одну сводку,
    которую дизайнер увидит без необходимости заходить в каждый раздел."""
    summary = await content_store.content_readiness_summary()
    total = summary["placeholder_cases"] + summary["about_pending_fields"] + summary["faq_pending"]
    if total == 0:
        return "Админ-меню:"
    parts = []
    if summary["placeholder_cases"]:
        parts.append(f"{summary['placeholder_cases']} кейс(ов) с фото-заглушкой")
    if summary["about_pending_fields"]:
        parts.append(f"«Обо мне» — {summary['about_pending_fields']} незаполненных полей")
    if summary["faq_pending"]:
        parts.append(f"{summary['faq_pending']} вопрос(ов) FAQ без финального ответа")
    return "Админ-меню:\n\n⚠️ Контент ещё не готов к показу клиентам: " + "; ".join(parts) + "."


@router.message(Command("admin"))
async def cmd_admin(message: Message, state: FSMContext) -> None:
    await flow.open_root(message, state, await _admin_root_text(), kb.admin_root_keyboard())


@router.message(F.text == texts.ADMIN_BUTTON)
async def admin_button(message: Message, state: FSMContext) -> None:
    # "⚙️ Админ" в reply-клавиатуре (см. bot/keyboards.py::main_reply_keyboard)
    # — просто ярлык на /admin; router уже гарантирует _is_designer_message
    # (см. router.message.filter выше), другая проверка здесь не нужна.
    await cmd_admin(message, state)


@router.callback_query(F.data == "admincancel")
async def admin_cancel(callback: CallbackQuery, state: FSMContext) -> None:
    """Универсальная кнопка "Отмена" на любом шаге, где ждём свободный
    текст/фото — без неё пришлось бы заново набирать /admin. Возвращает к
    разделу, с которого начался конкретный мастер (см. _resolve_cancel),
    а не всегда в корень."""
    text, markup, next_state, next_data = await _resolve_cancel(await state.get_data())
    await flow.set_data_keep_nav(state, next_data)
    await state.set_state(next_state)
    await callback.message.edit_text(text, reply_markup=markup)
    await callback.answer()


@router.message(StateFilter(AdminStates), Command("cancel"))
async def admin_cancel_command(message: Message, state: FSMContext) -> None:
    """Текстовый /cancel как альтернатива инлайн-кнопке "❌ Отмена" — в
    клиентском флоу (bot/handlers/start.py) /cancel уже работает так,
    админка была единственным местом без него: набранный /cancel просто
    сохранялся как введённые данные (текст вопроса FAQ, название кейса...).

    В отличие от инлайн "❌ Отмена" (admin_cancel — редактирует
    callback.message на месте, orphan структурно невозможен), у текстовой
    команды нет прямой ссылки на предыдущее сообщение бота — сначала
    best-effort чистим то, что реально отслеживается (см.
    flow.cancel_transient и её докстринг про архитектурные границы этого
    best-effort), и только затем показываем новый экран."""
    text, markup, next_state, next_data = await _resolve_cancel(await state.get_data())
    await flow.cancel_transient(message, state)
    await flow.set_data_keep_nav(state, next_data)
    await state.set_state(next_state)
    await message.answer(text, reply_markup=markup)


@router.message(F.text == texts.MAIN_MENU_BUTTON)
async def admin_main_menu_button(message: Message, state: FSMContext) -> None:
    """"⌂ Главное меню" для владельца — та же логика, что у клиента (см.
    bot/handlers/start.py::main_menu_or_confirm), но зарегистрирована
    ЗДЕСЬ и РАНО (сразу после admin_cancel_command, до любого из
    AdminStates.*, F.text мастеров ниже) — иначе, будь владелец, например,
    в середине ввода ответа FAQ (AdminStates.add_faq_answer, F.text без
    доп. фильтра), текст кнопки был бы молча проглочен как введённые данные
    вместо того, чтобы сработать как аварийный выход (тот же принцип, что
    уже обеспечивает надёжность admin_cancel_command выше — регистрация
    раньше мастеров, а не какая-то особая приоритизация от aiogram)."""
    await main_menu_or_confirm(message, state)


# ---- Навигация между разделами (используется как "назад" отовсюду) ----

@router.callback_query(F.data == "adminmenu:root")
async def menu_root(callback: CallbackQuery, state: FSMContext) -> None:
    # flow.step_from_callback вместо raw callback.message.edit_text (P1-3,
    # Batch 1) — reset_state_keep_nav выше стирает _flow_msg_id/_flow_chat_id
    # (сохраняет только NAV-ключи), и raw edit_text ничего не ставило взамен:
    # экран физически корректен (тот же message_id), но TRANSIENT anchor
    # больше на него не указывает. step_from_callback правит именно это —
    # редактирует то же сообщение и заново фиксирует его как текущий экран,
    # чтобы /cancel и "⌂ Главное меню" знали, что удалять.
    await flow.reset_state_keep_nav(state)
    await flow.step_from_callback(callback, state, await _admin_root_text(), kb.admin_root_keyboard())
    await callback.answer()


@router.callback_query(F.data == "adminmenu:cases")
async def menu_cases(callback: CallbackQuery, state: FSMContext) -> None:
    await flow.reset_state_keep_nav(state)
    await flow.step_from_callback(callback, state, "Кейсы портфолио:", kb.admin_cases_menu_keyboard())
    await callback.answer()


@router.callback_query(F.data == "adminmenu:faq")
async def menu_faq(callback: CallbackQuery, state: FSMContext) -> None:
    await flow.reset_state_keep_nav(state)
    await flow.step_from_callback(callback, state, "FAQ:", kb.admin_faq_menu_keyboard())
    await callback.answer()


@router.callback_query(F.data == "adminmenu:pricing")
async def menu_pricing(callback: CallbackQuery, state: FSMContext) -> None:
    await flow.reset_state_keep_nav(state)
    await flow.step_from_callback(callback, state, "Услуги и цены:", kb.pricing_menu_keyboard())
    await callback.answer()


@router.callback_query(F.data == "adminmenu:categories")
async def menu_categories(callback: CallbackQuery, state: FSMContext) -> None:
    await flow.reset_state_keep_nav(state)
    await flow.step_from_callback(callback, state, "Категории портфолио:", kb.categories_menu_keyboard())
    await callback.answer()


@router.callback_query(F.data == "adminmenu:nav")
async def menu_nav(callback: CallbackQuery, state: FSMContext) -> None:
    await flow.reset_state_keep_nav(state)
    ui_config = await content_store.get_ui_config()
    await flow.step_from_callback(
        callback, state,
        "Меню и навигация — нажмите пункт, чтобы включить/выключить:",
        kb.nav_menu_keyboard(ui_config),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("adminnavtoggle:"))
async def nav_toggle(callback: CallbackQuery, state: FSMContext) -> None:
    key = callback.data.split(":", 1)[1]
    current = (await content_store.get_ui_config())["menu"].get(key, True)
    await content_store.set_menu_item_enabled(callback.message.chat.id, key, not current)
    updated = await content_store.get_ui_config()
    await callback.message.edit_text(
        "Меню и навигация — нажмите пункт, чтобы включить/выключить:",
        reply_markup=kb.nav_menu_keyboard(updated),
    )
    await callback.answer()


@router.callback_query(F.data == "adminmenu:about")
async def menu_about(callback: CallbackQuery, state: FSMContext) -> None:
    await flow.reset_state_keep_nav(state)
    about = await content_store.get_about()
    await flow.step_from_callback(
        callback, state,
        "Что изменить в разделе «Обо мне»?",
        kb.about_field_keyboard(about.get("needs_review_fields")),
    )
    await state.set_state(AdminStates.edit_about_field_pick)
    await callback.answer()


# ---- Кейсы: действия ----

@router.callback_query(F.data == "admincasesaction:add")
async def cases_add_start(callback: CallbackQuery, state: FSMContext) -> None:
    types = await content_store.list_portfolio_types()
    await callback.message.edit_text("Категория нового кейса:", reply_markup=kb.category_pick_keyboard(types))
    await state.update_data(cancel_to="cases")
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
    # flow.step_from_text вместо message.answer (P1-3, Batch 1) — редактирует
    # тот же flow message вместо отправки нового (RULE 3), чтобы _flow_msg_id
    # не устаревал и /cancel на следующем шаге удалял актуальный prompt, а не
    # осиротевшее "Название кейса". См. bot/flow.py про архитектурную
    # границу primitives.
    case_id = await content_store.next_case_id()
    await state.update_data(title=message.text.strip(), case_id=case_id)
    await flow.step_from_text(message, state, "Пришлите фото кейса (как фото):", kb.cancel_keyboard())
    await state.set_state(AdminStates.add_case_photo)


@router.message(AdminStates.add_case_photo, F.photo | F.document)
async def cases_add_photo(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    file_id = message.photo[-1].file_id if message.photo else message.document.file_id
    cover = await content_store.save_case_photo(message.chat.id, message.bot, file_id, data["case_id"])
    await state.update_data(cover=cover)
    await flow.step_from_text(message, state, "Короткое описание задачи (пара предложений):", kb.cancel_keyboard())
    await state.set_state(AdminStates.add_case_description)


@router.message(AdminStates.add_case_photo)
async def cases_add_photo_wrong(message: Message, state: FSMContext) -> None:
    await flow.step_from_text(message, state, "Нужно фото 📎.", kb.cancel_keyboard())


@router.message(AdminStates.add_case_description, F.text)
async def cases_add_description(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    related_service = await content_store.default_related_service_for_type(data["type_id"])
    case = await content_store.add_case(
        message.chat.id,
        case_id=data["case_id"],
        title=data["title"],
        type_id=data["type_id"],
        cover=data["cover"],
        task=message.text.strip(),
        related_service=related_service,
    )
    # step_from_text ДО reset_state_keep_nav — иначе последний перестаёт
    # видеть tracked _flow_msg_id (reset уже стёр бы его) и просто шлёт
    # новое сообщение, теряя смысл миграции. reset_state_keep_nav ниже
    # по-прежнему стирает то же самое, что и раньше — порядок не меняет
    # итоговые state/data, только то, каким сообщением показан результат.
    await flow.step_from_text(
        message, state,
        f"Кейс «{case['title']}» добавлен и уже виден в Mini App ✅\n\n"
        "Поля «Решение» и «Результат» пока пустые — заполнить можно через «✏️ Редактировать».",
        kb.admin_cases_menu_keyboard(),
    )
    await flow.reset_state_keep_nav(state)


@router.callback_query(F.data == "admincasesaction:edit")
async def cases_edit_start(callback: CallbackQuery, state: FSMContext) -> None:
    cases = await content_store.list_cases()
    if not cases:
        await callback.message.edit_text("Кейсов пока нет.", reply_markup=kb.admin_cases_menu_keyboard())
        await callback.answer()
        return
    await callback.message.edit_text("Какой кейс редактировать?", reply_markup=kb.case_pick_keyboard(cases, "admineditcase"))
    await state.update_data(cancel_to="cases")
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
        await flow.reset_state_keep_nav(state)
        await callback.message.edit_text("Кейсы портфолио:", reply_markup=kb.admin_cases_menu_keyboard())
        await callback.answer()
        return
    await state.update_data(field=field)
    if field == "related_service":
        services = await content_store.list_services()
        await callback.message.edit_text(
            "С какой услугой связать кейс (для «Хочу похожий проект»)?",
            reply_markup=kb.related_service_pick_keyboard(services, "admincaserelservice"),
        )
        await callback.answer()
        return
    if field == "category":
        types = await content_store.list_portfolio_types()
        await callback.message.edit_text("Новая категория кейса:", reply_markup=kb.change_case_category_keyboard(types))
        await callback.answer()
        return
    if field == "images":
        case_id = (await state.get_data())["case_id"]
        case = await _current_case(case_id)
        await callback.message.edit_text(
            "Изображения кейса — ⭐ отмечает текущую обложку:",
            reply_markup=kb.case_images_menu_keyboard(case.get("images", []) if case else [], case.get("cover") if case else None),
        )
        await state.update_data(cancel_to="images")
        await state.set_state(AdminStates.case_images_menu)
        await callback.answer()
        return
    if field == "sections":
        case_id = (await state.get_data())["case_id"]
        case = await _current_case(case_id)
        await callback.message.edit_text(
            "Разделы кейса:",
            reply_markup=kb.case_sections_menu_keyboard(case.get("sections", []) if case else []),
        )
        await state.update_data(cancel_to="sections")
        await state.set_state(AdminStates.case_sections_menu)
        await callback.answer()
        return
    prompt = "Пришлите новое фото:" if field == "cover" else "Пришлите новый текст:"
    await callback.message.edit_text(prompt, reply_markup=kb.cancel_keyboard())
    await state.set_state(AdminStates.edit_case_value)
    await callback.answer()


@router.callback_query(AdminStates.edit_case_field_pick, F.data.startswith("admincaserelservice:"))
async def cases_edit_related_service(callback: CallbackQuery, state: FSMContext) -> None:
    value = callback.data.split(":", 1)[1]
    data = await state.get_data()
    await content_store.update_case(callback.message.chat.id, data["case_id"], related_service=None if value == "none" else value)
    await callback.message.edit_text("Обновлено ✅\n\nЧто ещё изменить?", reply_markup=kb.case_field_keyboard())
    await callback.answer()


@router.callback_query(AdminStates.edit_case_field_pick, F.data.startswith("admincasenewcat:"))
async def cases_edit_category(callback: CallbackQuery, state: FSMContext) -> None:
    new_type_id = callback.data.split(":", 1)[1]
    data = await state.get_data()
    await content_store.update_case_category(callback.message.chat.id, data["case_id"], new_type_id)
    await callback.message.edit_text("Обновлено ✅\n\nЧто ещё изменить?", reply_markup=kb.case_field_keyboard())
    await callback.answer()


# ---- Изображения кейса (вложены в редактирование кейса — case_id уже в data) ----

async def _current_case(case_id: str) -> dict | None:
    return next((c for c in await content_store.list_cases() if c["id"] == case_id), None)


@router.callback_query(AdminStates.case_images_menu, F.data == "admincaseimgaction:add")
async def case_image_add_start(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.message.edit_text("Пришлите фото для добавления в галерею:", reply_markup=kb.cancel_keyboard())
    await state.update_data(cancel_to="images")
    await state.set_state(AdminStates.case_image_add)
    await callback.answer()


@router.message(AdminStates.case_image_add, F.photo | F.document)
async def case_image_add_receive(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    file_id = message.photo[-1].file_id if message.photo else message.document.file_id
    path = await content_store.save_case_photo(message.chat.id, message.bot, file_id, f"{data['case_id']}_{uuid.uuid4().hex[:8]}")
    await content_store.add_case_image(message.chat.id, data["case_id"], path)
    case = await _current_case(data["case_id"])
    # flow.step_from_text (P1-3, Batch 3) — success-переход в case_images_menu
    # (AdminStates-активное, cancel_to="images" сохраняется) — без него
    # /cancel из case_images_menu удалял бы устаревший "Пришлите фото..."
    # prompt, оставляя этот, актуальный, экран осиротевшим (см. аудит).
    await flow.step_from_text(
        message, state,
        "Добавлено ✅\n\nИзображения кейса:",
        kb.case_images_menu_keyboard(case.get("images", []), case.get("cover")),
    )
    await state.set_state(AdminStates.case_images_menu)


@router.message(AdminStates.case_image_add)
async def case_image_add_wrong(message: Message, state: FSMContext) -> None:
    # flow.step_from_text (P1-3, Batch 2) — та же прогулка через RULE 3,
    # что и Batch 1: без неё эта reprompt-ветка оставляла бы неотслеживаемое
    # сообщение, и /cancel на повторной попытке удалял бы не его.
    await flow.step_from_text(message, state, "Нужно фото 📎.", kb.cancel_keyboard())


@router.callback_query(AdminStates.case_images_menu, F.data.startswith("admincaseimgpick:"))
async def case_image_picked(callback: CallbackQuery, state: FSMContext) -> None:
    index = int(callback.data.split(":", 1)[1])
    data = await state.get_data()
    case = await _current_case(data["case_id"])
    images = case.get("images", []) if case else []
    if not (0 <= index < len(images)):
        await callback.answer("Изображение не найдено", show_alert=True)
        return
    image_path = images[index]
    await state.update_data(image_path=image_path)
    is_cover = case.get("cover") == image_path
    await callback.message.edit_text(
        f"{image_path.split('/')[-1]}{' (обложка)' if is_cover else ''}:",
        reply_markup=kb.case_image_action_keyboard(is_cover),
    )
    await callback.answer()


@router.callback_query(AdminStates.case_images_menu, F.data.startswith("admincaseimgact:"))
async def case_image_action(callback: CallbackQuery, state: FSMContext) -> None:
    action = callback.data.split(":", 1)[1]
    data = await state.get_data()
    case_id, image_path = data["case_id"], data.get("image_path")

    if action == "cover":
        await content_store.set_case_cover(callback.message.chat.id, case_id, image_path)
    elif action in ("up", "down"):
        await content_store.reorder_case_image(callback.message.chat.id, case_id, image_path, action)
    elif action == "delete":
        await content_store.remove_case_image(callback.message.chat.id, case_id, image_path)
    # "back" — просто возвращает к списку, ничего не меняя

    case = await _current_case(case_id)
    await callback.message.edit_text(
        "Изображения кейса — ⭐ отмечает текущую обложку:",
        reply_markup=kb.case_images_menu_keyboard(case.get("images", []) if case else [], case.get("cover") if case else None),
    )
    await callback.answer()


@router.callback_query(AdminStates.case_images_menu, F.data == "admincaseimgaction:done")
async def case_images_done(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.message.edit_text("Что изменить?", reply_markup=kb.case_field_keyboard())
    await state.set_state(AdminStates.edit_case_field_pick)
    await callback.answer()


# ---- Разделы кейса (sections) ----

@router.callback_query(AdminStates.case_sections_menu, F.data == "admincasesecaction:add")
async def case_section_add_start(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.message.edit_text("Тип раздела:", reply_markup=kb.case_section_type_keyboard())
    await state.update_data(cancel_to="sections")
    await state.set_state(AdminStates.case_section_add_type)
    await callback.answer()


@router.callback_query(AdminStates.case_section_add_type, F.data.startswith("admincasesectype:"))
async def case_section_add_type(callback: CallbackQuery, state: FSMContext) -> None:
    section_type = callback.data.split(":", 1)[1]
    await state.update_data(section_type=section_type)
    await callback.message.edit_text("Название раздела:", reply_markup=kb.cancel_keyboard())
    await state.set_state(AdminStates.case_section_add_title)
    await callback.answer()


@router.message(AdminStates.case_section_add_title, F.text)
async def case_section_add_title(message: Message, state: FSMContext) -> None:
    # flow.step_from_text (P1-3, Batch 1) в обеих ветках — gallery
    # завершает сценарий здесь же, non-gallery продолжает в
    # case_section_add_content, которому нужен актуальный _flow_msg_id,
    # а не тот, что оставил бы raw message.answer.
    data = await state.get_data()
    title = message.text.strip()
    if data["section_type"] == "gallery":
        await content_store.add_case_section(message.chat.id, data["case_id"], section_type="gallery", title=title, images=[])
        case = await _current_case(data["case_id"])
        await flow.step_from_text(
            message, state,
            "Раздел-галерея добавлен ✅ Добавьте в него изображения через список разделов.\n\nРазделы кейса:",
            kb.case_sections_menu_keyboard(case.get("sections", []) if case else []),
        )
        await state.set_state(AdminStates.case_sections_menu)
        return
    await state.update_data(section_title=title)
    await flow.step_from_text(message, state, "Текст раздела:", kb.cancel_keyboard())
    await state.set_state(AdminStates.case_section_add_content)


@router.message(AdminStates.case_section_add_content, F.text)
async def case_section_add_content(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    await content_store.add_case_section(message.chat.id, data["case_id"], section_type="text", title=data["section_title"], content=message.text.strip())
    case = await _current_case(data["case_id"])
    await flow.step_from_text(message, state, "Раздел добавлен ✅\n\nРазделы кейса:", kb.case_sections_menu_keyboard(case.get("sections", []) if case else []))
    await state.set_state(AdminStates.case_sections_menu)


@router.callback_query(AdminStates.case_sections_menu, F.data.startswith("admincasesecpick:"))
async def case_section_picked(callback: CallbackQuery, state: FSMContext) -> None:
    index = int(callback.data.split(":", 1)[1])
    data = await state.get_data()
    case = await _current_case(data["case_id"])
    sections = case.get("sections", []) if case else []
    if not (0 <= index < len(sections)):
        await callback.answer("Раздел не найден", show_alert=True)
        return
    await state.update_data(section_index=index)
    section = sections[index]
    await callback.message.edit_text(f"«{section['title']}»:", reply_markup=kb.case_section_action_keyboard(section["type"]))
    await state.set_state(AdminStates.case_section_edit_field_pick)
    await callback.answer()


@router.callback_query(AdminStates.case_section_edit_field_pick, F.data.startswith("admincasesecact:"))
async def case_section_action(callback: CallbackQuery, state: FSMContext) -> None:
    action = callback.data.split(":", 1)[1]
    data = await state.get_data()
    case_id, index = data["case_id"], data["section_index"]

    if action == "title":
        await callback.message.edit_text("Новое название раздела:", reply_markup=kb.cancel_keyboard())
        await state.update_data(section_field="title")
        await state.set_state(AdminStates.case_section_edit_value)
        await callback.answer()
        return
    if action == "content":
        await callback.message.edit_text("Новый текст раздела:", reply_markup=kb.cancel_keyboard())
        await state.update_data(section_field="content")
        await state.set_state(AdminStates.case_section_edit_value)
        await callback.answer()
        return
    if action == "addimg":
        await callback.message.edit_text("Пришлите фото для этого раздела:", reply_markup=kb.cancel_keyboard())
        await state.set_state(AdminStates.case_section_edit_value)
        await state.update_data(section_field="addimg")
        await callback.answer()
        return
    if action == "removeimg":
        case = await _current_case(case_id)
        images = case["sections"][index].get("images", []) if case else []
        if not images:
            await callback.answer("В разделе пока нет изображений", show_alert=True)
            return
        await callback.message.edit_text("Убрать какое изображение?", reply_markup=kb.section_image_pick_keyboard(images))
        await callback.answer()
        return
    if action in ("up", "down"):
        await content_store.reorder_case_section(callback.message.chat.id, case_id, index, action)
        case = await _current_case(case_id)
        await callback.message.edit_text("Разделы кейса:", reply_markup=kb.case_sections_menu_keyboard(case.get("sections", []) if case else []))
        await state.set_state(AdminStates.case_sections_menu)
        await callback.answer()
        return
    if action == "delete":
        await content_store.delete_case_section(callback.message.chat.id, case_id, index)
        case = await _current_case(case_id)
        await callback.message.edit_text("Раздел удалён ✅\n\nРазделы кейса:", reply_markup=kb.case_sections_menu_keyboard(case.get("sections", []) if case else []))
        await state.set_state(AdminStates.case_sections_menu)
        await callback.answer()
        return
    # "back"
    case = await _current_case(case_id)
    await callback.message.edit_text("Разделы кейса:", reply_markup=kb.case_sections_menu_keyboard(case.get("sections", []) if case else []))
    await state.set_state(AdminStates.case_sections_menu)
    await callback.answer()


@router.callback_query(AdminStates.case_section_edit_field_pick, F.data.startswith("admincasesecimgpick:"))
async def case_section_remove_image(callback: CallbackQuery, state: FSMContext) -> None:
    img_index = int(callback.data.split(":", 1)[1])
    data = await state.get_data()
    case = await _current_case(data["case_id"])
    images = case["sections"][data["section_index"]].get("images", []) if case else []
    if 0 <= img_index < len(images):
        remaining = [img for i, img in enumerate(images) if i != img_index]
        await content_store.update_case_section(callback.message.chat.id, data["case_id"], data["section_index"], images=remaining)
    section = (await _current_case(data["case_id"]))["sections"][data["section_index"]]
    await callback.message.edit_text(f"«{section['title']}»:", reply_markup=kb.case_section_action_keyboard(section["type"]))
    await callback.answer()


@router.message(AdminStates.case_section_edit_value)
async def case_section_edit_value(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    field = data.get("section_field")
    case_id, index = data["case_id"], data["section_index"]

    # Обе reprompt-ветки ниже — flow.step_from_text (P1-3, Batch 2): без
    # него invalid-попытка оставляла бы неотслеживаемое сообщение, и
    # /cancel на следующей попытке удалял бы не его (тот же механизм,
    # что Batch 1 исправил для guaranteed-stale valid-path шагов).
    if field == "addimg":
        if not (message.photo or message.document):
            await flow.step_from_text(message, state, "Нужно фото 📎.", kb.cancel_keyboard())
            return
        file_id = message.photo[-1].file_id if message.photo else message.document.file_id
        path = await content_store.save_case_photo(message.chat.id, message.bot, file_id, f"{case_id}_{uuid.uuid4().hex[:8]}")
        case = await _current_case(case_id)
        images = case["sections"][index].get("images", []) if case else []
        await content_store.update_case_section(message.chat.id, case_id, index, images=images + [path])
    else:
        if not message.text:
            await flow.step_from_text(message, state, "Нужен текст.", kb.cancel_keyboard())
            return
        await content_store.update_case_section(message.chat.id, case_id, index, **{field: message.text.strip()})

    section = (await _current_case(case_id))["sections"][index]
    # flow.step_from_text (P1-3, Batch 3) — success-переход в
    # case_section_edit_field_pick (cancel_to="sections" сохраняется).
    await flow.step_from_text(message, state, f"Обновлено ✅\n\n«{section['title']}»:", kb.case_section_action_keyboard(section["type"]))
    await state.set_state(AdminStates.case_section_edit_field_pick)


@router.callback_query(AdminStates.case_sections_menu, F.data == "admincasesecaction:done")
async def case_sections_done(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.message.edit_text("Что изменить?", reply_markup=kb.case_field_keyboard())
    await state.set_state(AdminStates.edit_case_field_pick)
    await callback.answer()


@router.message(AdminStates.edit_case_value)
async def cases_edit_value(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    field = data["field"]
    if field == "cover":
        if not (message.photo or message.document):
            await flow.step_from_text(message, state, "Нужно фото 📎.", kb.cancel_keyboard())
            return
        file_id = message.photo[-1].file_id if message.photo else message.document.file_id
        value = await content_store.save_case_photo(message.chat.id, message.bot, file_id, data["case_id"])
        await content_store.update_case(message.chat.id, data["case_id"], cover=value)
    else:
        if not message.text:
            await flow.step_from_text(message, state, "Нужен текст.", kb.cancel_keyboard())
            return
        await content_store.update_case(message.chat.id, data["case_id"], **{field: message.text.strip()})
    # flow.step_from_text (P1-3, Batch 3) — success-переход в
    # edit_case_field_pick (cancel_to="cases" сохраняется).
    await flow.step_from_text(message, state, "Обновлено ✅\n\nЧто ещё изменить?", kb.case_field_keyboard())
    await state.set_state(AdminStates.edit_case_field_pick)


@router.callback_query(F.data == "admincasesaction:delete")
async def cases_delete_start(callback: CallbackQuery, state: FSMContext) -> None:
    cases = await content_store.list_cases()
    if not cases:
        await callback.message.edit_text("Кейсов пока нет.", reply_markup=kb.admin_cases_menu_keyboard())
        await callback.answer()
        return
    await callback.message.edit_text("Какой кейс удалить?", reply_markup=kb.case_pick_keyboard(cases, "admindelcase"))
    await state.update_data(cancel_to="cases")
    await state.set_state(AdminStates.delete_case_pick)
    await callback.answer()


@router.callback_query(AdminStates.delete_case_pick, F.data.startswith("admindelcase:"))
async def cases_delete_confirm(callback: CallbackQuery, state: FSMContext) -> None:
    case_id = callback.data.split(":", 1)[1]
    case = next((c for c in await content_store.list_cases() if c["id"] == case_id), None)
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
        await content_store.delete_case(callback.message.chat.id, data["case_id"])
        text = "Кейс удалён ✅"
    else:
        text = "Отменено."
    await flow.reset_state_keep_nav(state)
    await callback.message.edit_text(text, reply_markup=kb.admin_cases_menu_keyboard())
    await callback.answer()


# ---- FAQ: действия ----

@router.callback_query(F.data == "adminfaqaction:add")
async def faq_add_start(callback: CallbackQuery, state: FSMContext) -> None:
    # Продемонстрированный пример RULE 3 (flow.py) — шаги мастера
    # редактируют одно сообщение вместо накопления новых. Остальные
    # текстовые мастера в этом файле пока НЕ переведены (см. отчёт) —
    # это точечный перенос принципа, не массовый рефакторинг.
    await flow.step_from_callback(callback, state, "Текст вопроса:", kb.cancel_keyboard())
    await state.update_data(cancel_to="faq")
    await state.set_state(AdminStates.add_faq_question)
    await callback.answer()


@router.message(AdminStates.add_faq_question, F.text)
async def faq_add_question(message: Message, state: FSMContext) -> None:
    await state.update_data(question=message.text.strip())
    await flow.step_from_text(message, state, "Текст ответа:", kb.cancel_keyboard())
    await state.set_state(AdminStates.add_faq_answer)


@router.message(AdminStates.add_faq_answer, F.text)
async def faq_add_answer(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    item = await content_store.add_faq(message.chat.id, data["question"], message.text.strip())
    await flow.step_from_text(message, state, f"Вопрос №{item['id']} добавлен ✅", kb.admin_faq_menu_keyboard())
    await state.set_state(None)  # не state.clear() — flow хранит id текущего экрана в data для RULE 2


@router.callback_query(F.data == "adminfaqaction:edit")
async def faq_edit_start(callback: CallbackQuery, state: FSMContext) -> None:
    items = await content_store.list_faq()
    await callback.message.edit_text("Какой вопрос редактировать?", reply_markup=kb.faq_pick_keyboard(items, "admineditfaq"))
    await state.update_data(cancel_to="faq")
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
        await flow.reset_state_keep_nav(state)
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
    await content_store.update_faq(message.chat.id, data["faq_id"], **{data["field"]: message.text.strip()})
    # flow.step_from_text (P1-3, Batch 3) — success-переход в
    # edit_faq_field_pick (cancel_to="faq" сохраняется).
    await flow.step_from_text(message, state, "Обновлено ✅\n\nЧто ещё изменить?", kb.faq_field_keyboard())
    await state.set_state(AdminStates.edit_faq_field_pick)


@router.callback_query(F.data == "adminfaqaction:delete")
async def faq_delete_start(callback: CallbackQuery, state: FSMContext) -> None:
    items = await content_store.list_faq()
    await callback.message.edit_text("Какой вопрос удалить?", reply_markup=kb.faq_pick_keyboard(items, "admindelfaq"))
    await state.update_data(cancel_to="faq")
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
        await content_store.delete_faq(callback.message.chat.id, data["faq_id"])
        text = "Вопрос удалён ✅"
    else:
        text = "Отменено."
    await flow.reset_state_keep_nav(state)
    await callback.message.edit_text(text, reply_markup=kb.admin_faq_menu_keyboard())
    await callback.answer()


# ---- Обо мне: редактирование полей ----

@router.callback_query(AdminStates.edit_about_field_pick, F.data.startswith("admineditabout:"))
async def about_edit_field(callback: CallbackQuery, state: FSMContext) -> None:
    field = callback.data.split(":", 1)[1]
    if field == "done":
        await flow.reset_state_keep_nav(state)
        await callback.message.edit_text("Админ-меню:", reply_markup=kb.admin_root_keyboard())
        await callback.answer()
        return
    if field == "experience":
        entries = (await content_store.get_about()).get("experience", [])
        await callback.message.edit_text("Опыт работы:", reply_markup=kb.about_experience_menu_keyboard(entries))
        await state.update_data(cancel_to="root")
        await state.set_state(AdminStates.about_experience_menu)
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


@router.callback_query(AdminStates.about_experience_menu, F.data == "adminaboutexpaction:add")
async def about_experience_add_start(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.message.edit_text("Должность / роль:", reply_markup=kb.cancel_keyboard())
    await state.update_data(cancel_to="root")
    await state.set_state(AdminStates.about_experience_add_role)
    await callback.answer()


@router.message(AdminStates.about_experience_add_role, F.text)
async def about_experience_add_role(message: Message, state: FSMContext) -> None:
    # flow.step_from_text (P1-3, Batch 1) — не названа явно в scope Batch 1,
    # но без неё _flow_msg_id устаревал бы уже на шаге 1→2 (company), и
    # исправление about_experience_add_company ниже было бы бессмысленным:
    # оно бы редактировало ЭТО сообщение, только если оно и так актуально.
    await state.update_data(exp_role=message.text.strip())
    await flow.step_from_text(message, state, "Компания / проект:", kb.cancel_keyboard())
    await state.set_state(AdminStates.about_experience_add_company)


@router.message(AdminStates.about_experience_add_company, F.text)
async def about_experience_add_company(message: Message, state: FSMContext) -> None:
    await state.update_data(exp_company=message.text.strip())
    await flow.step_from_text(message, state, "Период (например «2019 — настоящее время»):", kb.cancel_keyboard())
    await state.set_state(AdminStates.about_experience_add_period)


@router.message(AdminStates.about_experience_add_period, F.text)
async def about_experience_add_period(message: Message, state: FSMContext) -> None:
    await state.update_data(exp_period=message.text.strip())
    await flow.step_from_text(message, state, "Короткое описание (необязательно — можно отправить «-»):", kb.cancel_keyboard())
    await state.set_state(AdminStates.about_experience_add_description)


@router.message(AdminStates.about_experience_add_description, F.text)
async def about_experience_add_description(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    description = message.text.strip()
    await content_store.add_about_experience(
        message.chat.id,
        role=data["exp_role"],
        company=data["exp_company"],
        period=data["exp_period"],
        description="" if description == "-" else description,
    )
    entries = (await content_store.get_about()).get("experience", [])
    await flow.step_from_text(message, state, "Запись добавлена ✅\n\nОпыт работы:", kb.about_experience_menu_keyboard(entries))
    await state.set_state(AdminStates.about_experience_menu)


@router.callback_query(AdminStates.about_experience_menu, F.data.startswith("adminaboutexppick:"))
async def about_experience_picked(callback: CallbackQuery, state: FSMContext) -> None:
    index = int(callback.data.split(":", 1)[1])
    entries = (await content_store.get_about()).get("experience", [])
    if not (0 <= index < len(entries)):
        await callback.answer("Запись не найдена", show_alert=True)
        return
    await state.update_data(exp_index=index)
    e = entries[index]
    text = f"{e['role']} — {e['company']}\n{e['period']}"
    if e.get("description"):
        text += f"\n\n{e['description']}"
    await callback.message.edit_text(text, reply_markup=kb.about_experience_entry_keyboard())
    await callback.answer()


@router.callback_query(AdminStates.about_experience_menu, F.data.startswith("adminaboutexpentry:"))
async def about_experience_entry_action(callback: CallbackQuery, state: FSMContext) -> None:
    action = callback.data.split(":", 1)[1]
    if action == "delete":
        data = await state.get_data()
        await content_store.delete_about_experience(callback.message.chat.id, data["exp_index"])
    entries = (await content_store.get_about()).get("experience", [])
    await callback.message.edit_text("Опыт работы:", reply_markup=kb.about_experience_menu_keyboard(entries))
    await callback.answer()


@router.message(AdminStates.edit_about_photo, F.photo | F.document)
async def about_edit_photo(message: Message, state: FSMContext) -> None:
    file_id = message.photo[-1].file_id if message.photo else message.document.file_id
    path = await content_store.save_about_photo(message.chat.id, message.bot, file_id)
    await content_store.update_about_field(message.chat.id, "avatar", path)
    about = await content_store.get_about()
    # flow.step_from_text (P1-3, Batch 3) — success-переход в
    # edit_about_field_pick (AdminStates-активное, /cancel остаётся
    # доступен даже без явного cancel_to — резолвится в "root").
    await flow.step_from_text(
        message, state,
        "Фото обновлено ✅\n\nЧто ещё изменить?",
        kb.about_field_keyboard(about.get("needs_review_fields")),
    )
    await state.set_state(AdminStates.edit_about_field_pick)


@router.message(AdminStates.edit_about_photo)
async def about_edit_photo_wrong(message: Message, state: FSMContext) -> None:
    await flow.step_from_text(message, state, "Нужно фото 📎.", kb.cancel_keyboard())


@router.message(AdminStates.edit_about_value, F.text)
async def about_edit_value(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    field = data["field"]
    if field in kb.ABOUT_LIST_FIELDS:
        values = [v.strip() for v in message.text.split(",") if v.strip()]
        await content_store.update_about_field(message.chat.id, field, values)
    else:
        await content_store.update_about_field(message.chat.id, field, message.text.strip())
    about = await content_store.get_about()
    # flow.step_from_text (P1-3, Batch 3) — success-переход в edit_about_field_pick.
    await flow.step_from_text(
        message, state,
        "Обновлено ✅\n\nЧто ещё изменить?",
        kb.about_field_keyboard(about.get("needs_review_fields")),
    )
    await state.set_state(AdminStates.edit_about_field_pick)


# ---- Добавить услугу ----

@router.callback_query(F.data == "adminpriceaction:add")
async def price_add_start(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.message.edit_text("Название новой услуги:", reply_markup=kb.cancel_keyboard())
    await state.update_data(cancel_to="pricing")
    await state.set_state(AdminStates.add_service_name)
    await callback.answer()


@router.message(AdminStates.add_service_name, F.text)
async def price_add_name(message: Message, state: FSMContext) -> None:
    # flow.step_from_text (P1-3, Batch 1) на каждом шаге, включая
    # invalid-retry ветки ниже — без них retry-сообщение тоже создавало бы
    # новый неотслеживаемый prompt, и /cancel сразу после неудачной
    # попытки по-прежнему удалял бы не тот message.
    await state.update_data(name=message.text.strip())
    await flow.step_from_text(message, state, "Базовая цена, ₽ (число):", kb.cancel_keyboard())
    await state.set_state(AdminStates.add_service_price)


@router.message(AdminStates.add_service_price, F.text)
async def price_add_price(message: Message, state: FSMContext) -> None:
    value = _parse_number(message.text)
    if value is None:
        await flow.step_from_text(message, state, "Нужно число, например 25000. Попробуйте ещё раз:", kb.cancel_keyboard())
        return
    await state.update_data(base_price=value)
    await flow.step_from_text(message, state, "Минимальный срок, дней (число):", kb.cancel_keyboard())
    await state.set_state(AdminStates.add_service_term_min)


@router.message(AdminStates.add_service_term_min, F.text)
async def price_add_term_min(message: Message, state: FSMContext) -> None:
    value = _parse_number(message.text)
    if value is None:
        await flow.step_from_text(message, state, "Нужно число. Попробуйте ещё раз:", kb.cancel_keyboard())
        return
    await state.update_data(term_min=value)
    await flow.step_from_text(message, state, "Максимальный срок, дней (число):", kb.cancel_keyboard())
    await state.set_state(AdminStates.add_service_term_max)


@router.message(AdminStates.add_service_term_max, F.text)
async def price_add_term_max(message: Message, state: FSMContext) -> None:
    value = _parse_number(message.text)
    if value is None:
        await flow.step_from_text(message, state, "Нужно число. Попробуйте ещё раз:", kb.cancel_keyboard())
        return
    await state.update_data(term_max=value)
    await flow.step_from_text(message, state, "Что входит в базовую стоимость (коротко текстом):", kb.cancel_keyboard())
    await state.set_state(AdminStates.add_service_includes)


@router.message(AdminStates.add_service_includes, F.text)
async def price_add_includes(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    service_id = await content_store.next_service_id()
    service = await content_store.add_service(
        message.chat.id,
        service_id=service_id,
        name=data["name"],
        base_price=data["base_price"],
        term_min=data["term_min"],
        term_max=data["term_max"],
        includes=message.text.strip(),
    )
    # step_from_text ДО reset_state_keep_nav — см. то же обоснование в
    # cases_add_description выше.
    await flow.step_from_text(message, state, f"Услуга «{service['name']}» добавлена ✅", kb.pricing_menu_keyboard())
    await flow.reset_state_keep_nav(state)


# ---- Редактировать услугу (+ опции внутри) ----

@router.callback_query(F.data == "adminpriceaction:edit")
async def price_edit_start(callback: CallbackQuery, state: FSMContext) -> None:
    services = await content_store.list_services()
    await callback.message.edit_text("Какую услугу редактировать?", reply_markup=kb.service_pick_keyboard(services, "admineditservice"))
    await state.update_data(cancel_to="pricing")
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
        await flow.reset_state_keep_nav(state)
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
            await flow.step_from_text(message, state, "Нужно число. Попробуйте ещё раз:", kb.cancel_keyboard())
            return
        await content_store.update_service(message.chat.id, data["service_id"], **{field: value})
    else:
        await content_store.update_service(message.chat.id, data["service_id"], **{field: message.text.strip()})
    # flow.step_from_text (P1-3, Batch 3) — success-переход в
    # edit_service_field_pick (cancel_to="pricing" сохраняется).
    await flow.step_from_text(message, state, "Обновлено ✅\n\nЧто изменить?", kb.service_field_keyboard())
    await state.set_state(AdminStates.edit_service_field_pick)


# ---- Удалить услугу ----

@router.callback_query(F.data == "adminpriceaction:delete")
async def price_delete_start(callback: CallbackQuery, state: FSMContext) -> None:
    services = await content_store.list_services()
    await callback.message.edit_text("Какую услугу удалить?", reply_markup=kb.service_pick_keyboard(services, "admindelservice"))
    await state.update_data(cancel_to="pricing")
    await state.set_state(AdminStates.delete_service_pick)
    await callback.answer()


@router.callback_query(AdminStates.delete_service_pick, F.data.startswith("admindelservice:"))
async def price_delete_confirm(callback: CallbackQuery, state: FSMContext) -> None:
    service_id = callback.data.split(":", 1)[1]
    service = await content_store.get_service(service_id)
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
        await content_store.delete_service(callback.message.chat.id, data["service_id"])
        text = "Услуга удалена ✅"
    else:
        text = "Отменено."
    await flow.reset_state_keep_nav(state)
    await callback.message.edit_text(text, reply_markup=kb.pricing_menu_keyboard())
    await callback.answer()


# ---- Коэффициенты и округление ----

@router.callback_query(F.data == "adminpriceaction:coef")
async def price_coef_start(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.message.edit_text("Что изменить?", reply_markup=kb.coefficients_menu_keyboard())
    await state.update_data(cancel_to="pricing")
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
    data = await state.get_data()
    # round_to используется как делитель в формуле вилки цены (bot/calculator.py
    # и webapp/js/calculator.js) — ноль сломал бы расчёт для всех клиентов сразу.
    min_value = 0.01 if data.get("key") == "round_to" else 0
    value = _parse_number(message.text, min_value=min_value)
    if value is None:
        await flow.step_from_text(message, state, "Нужно число. Попробуйте ещё раз:", kb.cancel_keyboard())
        return
    if data["kind"] == "coef":
        await content_store.update_coefficient(message.chat.id, data["key"], value)
    else:
        await content_store.update_rounding(message.chat.id, data["key"], value)
    # flow.step_from_text (P1-3, Batch 3) — success-переход в
    # edit_coefficients_pick (cancel_to="pricing" сохраняется).
    await flow.step_from_text(message, state, "Обновлено ✅\n\nЧто ещё изменить?", kb.coefficients_menu_keyboard())
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
    await state.update_data(cancel_to="options")

    if action == "add":
        await callback.message.edit_text("Название новой опции:", reply_markup=kb.cancel_keyboard())
        await state.set_state(AdminStates.option_add_name)
    elif action == "edit":
        options = await content_store.list_options(service_id)
        if not options:
            await callback.message.edit_text("У этой услуги пока нет опций.", reply_markup=kb.options_menu_keyboard())
            await callback.answer()
            return
        await callback.message.edit_text("Какую опцию редактировать?", reply_markup=kb.option_pick_keyboard(options, "admineditoption"))
        await state.set_state(AdminStates.option_edit_pick)
    elif action == "delete":
        options = await content_store.list_options(service_id)
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
    # flow.step_from_text (P1-3, Batch 1) — не названа явно в scope Batch 1,
    # но нужна по той же причине, что и about_experience_add_role: без неё
    # option_add_price ниже редактировало бы уже устаревший message.
    await state.update_data(opt_name=message.text.strip())
    await flow.step_from_text(message, state, "Цена опции, +₽ (число):", kb.cancel_keyboard())
    await state.set_state(AdminStates.option_add_price)


@router.message(AdminStates.option_add_price, F.text)
async def option_add_price(message: Message, state: FSMContext) -> None:
    value = _parse_number(message.text)
    if value is None:
        await flow.step_from_text(message, state, "Нужно число. Попробуйте ещё раз:", kb.cancel_keyboard())
        return
    await state.update_data(opt_price=value)
    await flow.step_from_text(message, state, "Срок опции, +дней (число, можно дробное, например 0.5):", kb.cancel_keyboard())
    await state.set_state(AdminStates.option_add_days)


@router.message(AdminStates.option_add_days, F.text)
async def option_add_days(message: Message, state: FSMContext) -> None:
    value = _parse_number(message.text)
    if value is None:
        await flow.step_from_text(message, state, "Нужно число. Попробуйте ещё раз:", kb.cancel_keyboard())
        return
    await state.update_data(opt_days=value)
    await flow.step_from_text(
        message, state,
        "Можно выбирать эту опцию несколько раз (умножается на количество)?",
        kb.yes_no_keyboard("adminoptmultipliable"),
    )
    await state.set_state(AdminStates.option_add_multipliable)


@router.callback_query(AdminStates.option_add_multipliable, F.data.startswith("adminoptmultipliable:"))
async def option_add_multipliable(callback: CallbackQuery, state: FSMContext) -> None:
    multipliable = callback.data.split(":", 1)[1] == "yes"
    data = await state.get_data()
    service_id = data["service_id"]
    option_id = await content_store.next_option_id(service_id)
    await content_store.add_option(
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
    await content_store.update_option(callback.message.chat.id, data["option_id"], multipliable=value)
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
            await flow.step_from_text(message, state, "Нужно число. Попробуйте ещё раз:", kb.cancel_keyboard())
            return
        await content_store.update_option(message.chat.id, data["option_id"], **{field: value})
    else:
        await content_store.update_option(message.chat.id, data["option_id"], **{field: message.text.strip()})
    # flow.step_from_text (P1-3, Batch 3) — success-переход в
    # option_edit_field_pick (cancel_to="options" сохраняется).
    await flow.step_from_text(message, state, "Обновлено ✅\n\nЧто изменить?", kb.option_field_keyboard())
    await state.set_state(AdminStates.option_edit_field_pick)


@router.callback_query(AdminStates.option_delete_pick, F.data.startswith("admindeloption:"))
async def option_delete_confirm(callback: CallbackQuery, state: FSMContext) -> None:
    option_id = callback.data.split(":", 1)[1]
    data = await state.get_data()
    option = next((o for o in await content_store.list_options(data["service_id"]) if o["id"] == option_id), None)
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
        await content_store.delete_option(callback.message.chat.id, data["option_id"])
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
    await state.update_data(cancel_to="categories")
    await state.set_state(AdminStates.add_category_label)
    await callback.answer()


@router.message(AdminStates.add_category_label, F.text)
async def cat_add_label(message: Message, state: FSMContext) -> None:
    type_id = await content_store.next_portfolio_type_id()
    cat = await content_store.add_portfolio_type(message.chat.id, type_id=type_id, label=message.text.strip())
    await flow.reset_state_keep_nav(state)
    await message.answer(f"Категория «{cat['label']}» добавлена ✅", reply_markup=kb.categories_menu_keyboard())


@router.callback_query(F.data == "admincataction:rename")
async def cat_rename_start(callback: CallbackQuery, state: FSMContext) -> None:
    types = await content_store.list_portfolio_types()
    await callback.message.edit_text("Какую категорию переименовать?", reply_markup=kb.category_manage_pick_keyboard(types, "adminrenamecat"))
    await state.update_data(cancel_to="categories")
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
    await content_store.rename_portfolio_type(message.chat.id, data["type_id"], message.text.strip())
    await flow.reset_state_keep_nav(state)
    await message.answer("Переименовано ✅", reply_markup=kb.categories_menu_keyboard())


@router.callback_query(F.data == "admincataction:relservice")
async def cat_relservice_start(callback: CallbackQuery, state: FSMContext) -> None:
    """Дефолтная "похожая услуга" для новых кейсов в категории — раньше эта
    связь была захардкожена в TYPE_TO_SERVICE и не редактировалась вообще;
    теперь живёт в data/portfolio.json -> types[].related_service."""
    types = await content_store.list_portfolio_types()
    await callback.message.edit_text("Для какой категории задать похожую услугу?", reply_markup=kb.category_manage_pick_keyboard(types, "admincatrelpick"))
    await state.update_data(cancel_to="categories")
    await state.set_state(AdminStates.category_related_service_pick)
    await callback.answer()


@router.callback_query(AdminStates.category_related_service_pick, F.data.startswith("admincatrelpick:"))
async def cat_relservice_picked(callback: CallbackQuery, state: FSMContext) -> None:
    type_id = callback.data.split(":", 1)[1]
    await state.update_data(type_id=type_id)
    services = await content_store.list_services()
    await callback.message.edit_text(
        "С какой услугой связать эту категорию (подставляется в новые кейсы этой категории)?",
        reply_markup=kb.related_service_pick_keyboard(services, "admincatrelservice"),
    )
    await callback.answer()


@router.callback_query(AdminStates.category_related_service_pick, F.data.startswith("admincatrelservice:"))
async def cat_relservice_set(callback: CallbackQuery, state: FSMContext) -> None:
    value = callback.data.split(":", 1)[1]
    data = await state.get_data()
    await content_store.update_portfolio_type_related_service(callback.message.chat.id, data["type_id"], None if value == "none" else value)
    await flow.reset_state_keep_nav(state)
    await callback.message.edit_text("Обновлено ✅", reply_markup=kb.categories_menu_keyboard())
    await callback.answer()


@router.callback_query(F.data == "admincataction:delete")
async def cat_delete_start(callback: CallbackQuery, state: FSMContext) -> None:
    types = await content_store.list_portfolio_types()
    await callback.message.edit_text("Какую категорию удалить?", reply_markup=kb.category_manage_pick_keyboard(types, "admindelcat"))
    await state.update_data(cancel_to="categories")
    await state.set_state(AdminStates.delete_category_pick)
    await callback.answer()


@router.callback_query(AdminStates.delete_category_pick, F.data.startswith("admindelcat:"))
async def cat_delete_confirm(callback: CallbackQuery, state: FSMContext) -> None:
    type_id = callback.data.split(":", 1)[1]
    in_use = await content_store.count_cases_with_type(type_id)
    if in_use > 0:
        await callback.message.edit_text(
            f"Нельзя удалить — категория используется в {in_use} кейс(ах). "
            "Сначала перенесите эти кейсы в другую категорию (редактирование кейса) или удалите их.",
            reply_markup=kb.categories_menu_keyboard(),
        )
        await flow.reset_state_keep_nav(state)
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
        await content_store.delete_portfolio_type(callback.message.chat.id, data["type_id"])
        text = "Категория удалена ✅"
    else:
        text = "Отменено."
    await flow.reset_state_keep_nav(state)
    await callback.message.edit_text(text, reply_markup=kb.categories_menu_keyboard())
    await callback.answer()


# ---- Заявки ----

@router.callback_query(F.data == "adminmenu:leads")
async def menu_leads(callback: CallbackQuery, state: FSMContext) -> None:
    # Дефолтный экран — активные заявки (см. content_store.ACTIVE_LEAD_STATUSES,
    # UX-аудит "Заявки как рабочая очередь"): DONE/CANCELLED не должны
    # заслонять то, что реально требует внимания. "Заявок вообще нет"
    # проверяем отдельно по ВСЕМ заявкам — иначе "0 активных, но есть
    # завершённые" ошибочно выглядело бы как пустая система и уводило бы
    # сразу в корень меню, а не в список с доступом к фильтру "Все".
    await flow.reset_state_keep_nav(state)
    all_leads = await content_store.list_leads("ALL")
    if not all_leads:
        await flow.step_from_callback(callback, state, "Заявок пока нет.", kb.admin_root_keyboard())
        await callback.answer()
        return
    leads = await content_store.list_leads("ACTIVE")
    await state.update_data(lead_filter="ACTIVE")
    text = f"Заявки — активные ({len(leads)}):" if leads else "Активных заявок нет."
    await flow.step_from_callback(callback, state, text, kb.leads_list_keyboard(leads, "ACTIVE"))
    await state.set_state(AdminStates.leads_list)
    await callback.answer()


@router.callback_query(AdminStates.leads_list, F.data == "adminleadaction:filter")
async def leads_filter_start(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.message.edit_text("Показать заявки:", reply_markup=kb.leads_filter_keyboard())
    await callback.answer()


@router.callback_query(AdminStates.leads_list, F.data.startswith("adminleadfilter:"))
async def leads_filter_apply(callback: CallbackQuery, state: FSMContext) -> None:
    status = callback.data.split(":", 1)[1]
    leads = await content_store.list_leads(status)
    await state.update_data(lead_filter=status)
    label = kb.LEAD_FILTER_LABELS[status]
    text = f"Заявки — {label} ({len(leads)}):" if leads else f"Заявок в статусе «{label}» нет."
    await callback.message.edit_text(text, reply_markup=kb.leads_list_keyboard(leads, status))
    await callback.answer()


@router.callback_query(AdminStates.leads_list, F.data.startswith("adminleadpick:"))
async def lead_open_detail(callback: CallbackQuery, state: FSMContext) -> None:
    lead_id = int(callback.data.split(":", 1)[1])
    lead = await content_store.get_lead(lead_id)
    if lead is None:
        await callback.answer("Заявка не найдена", show_alert=True)
        return
    if lead["status"] == "NEW":
        # Открытие карточки владельцем = "заявка просмотрена" — тот же
        # content_store.update_lead_status(), что и у кнопок статуса в
        # деталях, но БЕЗ уведомления клиента (его шлёт только
        # lead_change_status — явная смена статуса кликом, не сам факт
        # чтения) и без owner_message (см. UX-аудит "Заявки как рабочая
        # очередь"). Ровно один update; на любом другом статусе (включая
        # уже VIEWED) — ничего не меняем.
        await content_store.update_lead_status(callback.message.chat.id, lead_id, "VIEWED")
        lead = await content_store.get_lead(lead_id)
    await state.update_data(lead_id=lead_id)
    await callback.message.edit_text(lead_format.format_lead_admin_detail(lead), reply_markup=kb.lead_detail_keyboard(lead))
    await state.set_state(AdminStates.lead_detail)
    await callback.answer()


@router.callback_query(AdminStates.lead_detail, F.data.startswith("adminleadstatus:"))
async def lead_change_status(callback: CallbackQuery, state: FSMContext) -> None:
    status = callback.data.split(":", 1)[1]
    data = await state.get_data()
    # Старый статус — ДО update_lead_status, иначе "изменился ли статус
    # реально" неизвестно (сигнатуру update_lead_status не меняем, она
    # по-прежнему просто bool успех/неуспех — см. аудит).
    lead_before = await content_store.get_lead(data["lead_id"])
    old_status = lead_before["status"] if lead_before else None

    await content_store.update_lead_status(callback.message.chat.id, data["lead_id"], status)
    lead = await content_store.get_lead(data["lead_id"])

    # Уведомление клиенту — только если статус реально поменялся (не
    # повторная установка того же значения) и есть куда слать. owner_messages[]
    # здесь не трогается вообще — это отдельный, независимый поток (только
    # явные текстовые ответы дизайнера через "Ответить через бота"), не
    # персистентный лог статус-уведомлений — сам lead["status"] уже
    # источник правды, Mini App покажет его корректно независимо от того,
    # дошло ли сейчас сообщение в Telegram.
    if lead and old_status != status and lead.get("telegram", {}).get("user_id"):
        try:
            await callback.bot.send_message(
                chat_id=lead["telegram"]["user_id"],
                text=lead_format.format_status_notification(lead.get("payload", {}).get("service_name"), status),
            )
        except Exception:
            logger.exception("Не удалось уведомить клиента о смене статуса заявки #%s", lead["id"])

    await callback.message.edit_text(lead_format.format_lead_admin_detail(lead), reply_markup=kb.lead_detail_keyboard(lead))
    await callback.answer("Статус обновлён")


@router.callback_query(AdminStates.lead_detail, F.data == "adminleadaction:reply")
async def lead_reply_start(callback: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    lead = await content_store.get_lead(data["lead_id"])
    if lead is None or not lead.get("telegram", {}).get("user_id"):
        await callback.answer("Нет Telegram ID клиента — ответить через бота нельзя", show_alert=True)
        return
    await callback.message.edit_text("Текст сообщения клиенту:", reply_markup=kb.cancel_keyboard())
    await state.update_data(cancel_to="root")
    await state.set_state(AdminStates.lead_reply_text)
    await callback.answer()


@router.message(AdminStates.lead_reply_text, F.text)
async def lead_reply_send(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    lead = await content_store.get_lead(data["lead_id"])
    if lead is None:
        await message.answer("Заявка не найдена.", reply_markup=kb.admin_root_keyboard())
        await flow.reset_state_keep_nav(state)
        return
    text = message.text.strip()
    try:
        # "Ответ по заявке #N" — контекст только в исходящем клиенту
        # сообщении (сам клиент мог получать от бота что угодно ещё); в
        # owner_messages сохраняется чистый текст без этой обёртки — внутри
        # detail конкретной заявки номер и так очевиден.
        await message.bot.send_message(
            chat_id=lead["telegram"]["user_id"],
            text=f"Ответ по заявке #{lead['id']}\n\n{text}",
        )
        delivery_status = "sent"
        result_text = "Сообщение отправлено клиенту ✅"
    except Exception:
        logger.exception("Не удалось отправить сообщение клиенту по заявке #%s", lead["id"])
        delivery_status = "failed"
        result_text = "⚠️ Не получилось отправить — клиент мог заблокировать бота. Ответ всё равно сохранён в истории заявки."
    # Сохраняем независимо от результата отправки — иначе неудачная
    # доставка (клиент заблокировал бота) стирала бы сам факт, что дизайнер
    # вообще отвечал, см. аудит.
    lead = await content_store.add_owner_message(message.chat.id, lead["id"], text, delivery_status) or lead
    # flow.step_from_text (P1-3, Batch 3) — success-переход в lead_detail
    # (cancel_to="root" сохраняется). "Заявка не найдена" веткой выше НЕ
    # мигрирована сознательно — reset_state_keep_nav сразу после неё
    # переводит state в None, /cancel там уже недостижим (см. аудит).
    await flow.step_from_text(message, state, f"{result_text}\n\n{lead_format.format_lead_admin_detail(lead)}", kb.lead_detail_keyboard(lead))
    await state.set_state(AdminStates.lead_detail)


@router.callback_query(AdminStates.lead_detail, F.data == "adminleadaction:back")
async def lead_back_to_list(callback: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    status = data.get("lead_filter", "ALL")
    leads = await content_store.list_leads(status)
    await callback.message.edit_text(f"Заявки ({len(leads)}):", reply_markup=kb.leads_list_keyboard(leads, status))
    await state.set_state(AdminStates.leads_list)
    await callback.answer()


@router.callback_query(AdminStates.lead_detail, F.data == "adminleadaction:delete")
async def lead_delete_start(callback: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    await callback.message.edit_text(
        f"Удалить заявку #{data['lead_id']}? Это необратимо.", reply_markup=kb.confirm_keyboard("admindelleadconfirm")
    )
    await state.set_state(AdminStates.lead_delete_confirm)
    await callback.answer()


@router.callback_query(AdminStates.lead_delete_confirm, F.data.startswith("admindelleadconfirm:"))
async def lead_delete_do(callback: CallbackQuery, state: FSMContext) -> None:
    answer = callback.data.split(":", 1)[1]
    data = await state.get_data()
    status = data.get("lead_filter", "ALL")
    if answer == "yes":
        await content_store.delete_lead(callback.message.chat.id, data["lead_id"])
    leads = await content_store.list_leads(status)
    text = f"Заявка удалена ✅\n\nЗаявки ({len(leads)}):" if answer == "yes" else f"Отменено.\n\nЗаявки ({len(leads)}):"
    await callback.message.edit_text(text, reply_markup=kb.leads_list_keyboard(leads, status))
    await state.set_state(AdminStates.leads_list)
    await callback.answer()


# ---- Бэкап (экспорт/восстановление data/*.json + фото через .zip) ----
# На бесплатном Render нет персистентного диска — единственный бесплатный
# способ пережить redeploy без стороннего сервиса: дизайнер сам выгружает
# .zip себе в Telegram (сохраняется у него как любой присланный файл) и
# загружает его обратно после деплоя через "Восстановить".

@router.callback_query(F.data == "adminmenu:backup")
async def menu_backup(callback: CallbackQuery, state: FSMContext) -> None:
    await flow.reset_state_keep_nav(state)
    await flow.step_from_callback(
        callback, state,
        "Бэкап данных (заявки, кейсы, «Обо мне», услуги, FAQ, фото) — переживает деплой, только если вы его восстановите после каждого обновления бота:",
        kb.backup_menu_keyboard(),
    )
    await callback.answer()


@router.callback_query(F.data == "adminbackupaction:export")
async def backup_export(callback: CallbackQuery, state: FSMContext) -> None:
    try:
        zip_bytes = await content_store.export_backup_bytes()
    except content_store.BackupExportError as e:
        await callback.message.answer(
            f"❌ Резервная копия НЕ создана: отсутствуют данные — {', '.join(e.missing_filenames)}.\n\n"
            "Архив не выдан, чтобы неполный бэкап случайно не приняли за полноценный.\n\nБэкап:",
            reply_markup=kb.backup_menu_keyboard(),
        )
        await callback.answer()
        return
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d_%H%M")
    filename = f"design-bot-backup-{stamp}.zip"
    await callback.message.answer_document(
        BufferedInputFile(zip_bytes, filename=filename),
        caption="Бэкап готов ✅ Telegram сохранит его у вас как обычный файл — этим же файлом восстанавливайте после деплоя.",
    )
    await callback.answer()


@router.callback_query(F.data == "adminbackupaction:import")
async def backup_import_start(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.message.edit_text("Пришлите .zip файл бэкапа:", reply_markup=kb.cancel_keyboard())
    await state.update_data(cancel_to="backup")
    await state.set_state(AdminStates.backup_restore_wait_file)
    await callback.answer()


@router.message(AdminStates.backup_restore_wait_file, F.document)
async def backup_import_receive(message: Message, state: FSMContext) -> None:
    file = await message.bot.get_file(message.document.file_id)
    file_bytes_io = await message.bot.download_file(file.file_path)
    try:
        result = await content_store.import_backup_bytes(message.chat.id, file_bytes_io.read())
    except zipfile.BadZipFile:
        # flow.step_from_text (P1-3, Batch 2) — единственная ветка этого
        # handler'а, что остаётся в том же AdminStates.backup_restore_wait_file
        # (ждёт другой файл, настоящий retry). Остальные ветки ниже переводят
        # в AdminStates.backup_menu — не retry, но AdminStates-активное
        # состояние с сохранённым cancel_to="backup", поэтому тоже мигрированы
        # (P1-3, Batch 3) — без этого /cancel из backup_menu удалял бы
        # устаревший "Пришлите .zip файл..." prompt, оставляя этот
        # результат-экран (с backup_menu_keyboard) осиротевшим.
        await flow.step_from_text(message, state, "Файл повреждён или не .zip — пришлите другой файл.", kb.cancel_keyboard())
        return
    except content_store.BackupValidationError as e:
        found = ", ".join(e.found_filenames) if e.found_filenames else "—"
        await flow.step_from_text(
            message, state,
            "❌ Восстановление ПОЛНОСТЬЮ отменено, ничего не изменено.\n\n"
            f"Файл: {e.filename}\nОшибка: {e.reason}\nНайдено в архиве: {found}",
            kb.backup_menu_keyboard(),
        )
        await state.set_state(AdminStates.backup_menu)
        return
    except content_store.BackupSnapshotError as e:
        # P2-6: не удалось прочитать текущее значение файла перед записью
        # (Phase 1) — restore отменён ДО первой записи, Phase 2 не начался,
        # ничего не изменено. Не раскрываем токен/детали исходного
        # исключения клиенту — только имя файла и факт отмены (полная
        # причина уже залогирована через logger.exception внутри
        # content_store.import_backup_bytes).
        await flow.step_from_text(
            message, state,
            "❌ Восстановление отменено: не удалось прочитать текущее значение "
            f"{e.filename!r} перед записью. Ничего не изменено — попробуйте ещё раз.",
            kb.backup_menu_keyboard(),
        )
        await state.set_state(AdminStates.backup_menu)
        return
    except content_store.BackupRestoreFailedError as e:
        if e.rollback_failed:
            await flow.step_from_text(
                message, state,
                f"🔴 КРИТИЧНО: восстановление прервано на файле {e.failed_filename}, "
                f"откат НЕ полностью удался для: {', '.join(e.rollback_failed)}.\n\n"
                "Требуется ручная проверка данных через /admin!",
                kb.backup_menu_keyboard(),
            )
        else:
            await flow.step_from_text(
                message, state,
                f"⚠️ Восстановление отменено из-за ошибки записи ({e.failed_filename}). "
                "Исходное состояние данных восстановлено.",
                kb.backup_menu_keyboard(),
            )
        await state.set_state(AdminStates.backup_menu)
        return

    lines: list[str] = []
    if result.restored_json:
        lines.append(f"Восстановлено файлов данных: {len(result.restored_json)} — {', '.join(result.restored_json)}")
    if result.missing_json:
        lines.append(f"Отсутствовали в бэкапе (не восстановлены, текущие данные не тронуты): {', '.join(result.missing_json)}")
    if result.restored_images:
        lines.append(f"Восстановлено изображений: {len(result.restored_images)}")
    if result.failed_images:
        lines.append(f"⚠️ Не удалось восстановить {len(result.failed_images)} изображений (данные уже восстановлены): {', '.join(result.failed_images)}")
    if not result.restored_json and not result.restored_images:
        lines.append("В архиве не нашлось знакомых файлов данных/фото — ничего не восстановлено.")
    await flow.step_from_text(message, state, "\n".join(lines) + "\n\nБэкап:", kb.backup_menu_keyboard())
    await state.set_state(AdminStates.backup_menu)


@router.message(AdminStates.backup_restore_wait_file)
async def backup_import_wrong(message: Message, state: FSMContext) -> None:
    await flow.step_from_text(message, state, "Нужен .zip файл 📎.", kb.cancel_keyboard())


# ---- Фолбэк: сообщение не того типа/формата на любом шаге админки ----
# Держим самым последним хендлером файла — сработает, только если ни один
# более конкретный хендлер выше не подошёл (например, стикер вместо текста,
# голосовое вместо числа). Раньше такие сообщения проваливались сквозь все
# роутеры бота без единого ответа — админ не понимал, жив ли бот.
@router.message(StateFilter(AdminStates))
async def admin_unexpected_input(message: Message) -> None:
    await message.answer(
        "Не поняла это сообщение 🙂 Нажмите кнопку в сообщении выше или пришлите нужный текст/число. "
        "Чтобы выйти — /cancel."
    )
