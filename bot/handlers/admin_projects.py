"""Telegram-native project administration and manual payment journal.

Private to DESIGNER_CHAT_ID. Demo records are explicitly labeled and never
generate client messages. No actual payment APIs or bank confirmations.
"""
import logging
import re
from html import escape

from aiogram import F, Router
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from bot import admin_keyboards, config, flow, project_keyboards as kb, project_store, texts
from bot.states import AdminStates, ProjectStates

router = Router(name="admin_projects")
logger = logging.getLogger(__name__)


def _owner_message(message: Message) -> bool:
    return bool(config.DESIGNER_CHAT_ID) and str(message.chat.id) == config.DESIGNER_CHAT_ID


def _owner_callback(callback: CallbackQuery) -> bool:
    return bool(config.DESIGNER_CHAT_ID) and str(callback.message.chat.id) == config.DESIGNER_CHAT_ID


router.message.filter(_owner_message)
router.callback_query.filter(_owner_callback)


def _rub(value: int | None) -> str:
    return f"{value:,} ₽".replace(",", " ") if value is not None else "не согласована"


def _parse_rub(text: str) -> int | None:
    cleaned = text.replace(" ", "").replace("\u00a0", "").strip()
    if not re.fullmatch(r"[0-9]{1,9}", cleaned):
        return None
    value = int(cleaned)
    return value if 1 <= value <= project_store.MAX_RUB else None


def _project_text(project: dict) -> str:
    paid = project_store.total_paid(project)
    agreed = project.get("price_rub")
    balance = agreed - paid if agreed is not None else None
    tag = "🧪 ТЕСТ — не реальный клиент" if project["is_demo"] else "📂 Коммерческий проект"
    current_stage = project["stages"][project["stage_index"]]
    return (
        f"<b>{tag} #{escape(project['id'])}</b>\n"
        f"<b>{escape(project['title'])}</b>\n"
        f"Клиент: {escape(project['client'])}\n"
        f"Статус: {escape(project_store.STATUSES[project['status']])}\n"
        f"Этап: {escape(current_stage)}\n"
        f"Срок: {escape(project.get('deadline') or 'не указан')}\n\n"
        f"Согласовано: {_rub(agreed)}\n"
        f"Учтено оплат: {_rub(paid)}\n"
        f"Остаток: {_rub(balance)}\n\n"
        "⚠️ Платежи фиксируются вручную после проверки поступления; "
        "бот деньги не принимает."
    )


async def _show_list(callback: CallbackQuery, state: FSMContext, page: int = 0) -> None:
    await flow.reset_state_keep_nav(state)
    projects = await project_store.list_projects(callback.message.chat.id)
    pages = max(1, (len(projects) + 7) // 8)
    page = min(max(page, 0), pages - 1)
    await flow.step_from_callback(
        callback, state, f"📂 Проекты ({len(projects)})\n\n"
        "Здесь только учёт заказов. Тестовые записи отмечены 🧪.\n"
        "Чтобы перевести реальную заявку в проект, откройте её в разделе «Заявки».",
        kb.project_list(projects, page),
    )
    await state.update_data(project_page=page)
    await state.set_state(ProjectStates.list)


async def _show_project(callback: CallbackQuery, state: FSMContext, project: dict) -> None:
    await flow.step_from_callback(callback, state, _project_text(project), kb.project_detail(project))
    await state.update_data(project_id=project["id"])
    await state.set_state(ProjectStates.detail)


async def _current_project(callback: CallbackQuery, state: FSMContext) -> dict | None:
    project_id = (await state.get_data()).get("project_id")
    if not project_id:
        await callback.answer("Выберите проект в списке", show_alert=True)
        return None
    project = await project_store.get_project(callback.message.chat.id, project_id)
    if project is None:
        await callback.answer("Проект не найден", show_alert=True)
    return project


@router.callback_query(F.data == "adminmenu:projects")
async def projects_menu(callback: CallbackQuery, state: FSMContext) -> None:
    await _show_list(callback, state)
    await callback.answer()


@router.callback_query(ProjectStates.list, F.data.startswith("pj:page:"))
async def projects_page(callback: CallbackQuery, state: FSMContext) -> None:
    try:
        page = int(callback.data.split(":")[-1])
    except ValueError:
        await callback.answer("Неверная страница", show_alert=True)
        return
    await _show_list(callback, state, page)
    await callback.answer()


@router.callback_query(F.data == "pj:noop")
async def page_noop(callback: CallbackQuery) -> None:
    await callback.answer()


@router.callback_query(ProjectStates.list, F.data == "pj:newdemo")
async def create_demo(callback: CallbackQuery, state: FSMContext) -> None:
    project = await project_store.create_demo(callback.message.chat.id)
    await _show_project(callback, state, project)
    await callback.answer("Тестовый проект создан")


@router.callback_query(AdminStates.lead_detail, F.data == "adminleadaction:project")
async def create_from_lead(callback: CallbackQuery, state: FSMContext) -> None:
    lead_id = (await state.get_data()).get("lead_id")
    if not lead_id:
        await callback.answer("Сначала откройте заявку", show_alert=True)
        return
    try:
        project = await project_store.create_from_lead(callback.message.chat.id, lead_id)
    except ValueError as exc:
        await callback.answer(str(exc), show_alert=True)
        return
    await _show_project(callback, state, project)
    await callback.answer("Проект открыт")


@router.callback_query(ProjectStates.list, F.data.startswith("pj:open:"))
async def open_project(callback: CallbackQuery, state: FSMContext) -> None:
    project_id = callback.data.split(":")[-1]
    project = await project_store.get_project(callback.message.chat.id, project_id)
    if project is None:
        await callback.answer("Проект не найден", show_alert=True)
        return
    await _show_project(callback, state, project)
    await callback.answer()


@router.callback_query(F.data == "pj:root")
async def go_admin_root(callback: CallbackQuery, state: FSMContext) -> None:
    await flow.reset_state_keep_nav(state)
    await flow.step_from_callback(callback, state, "Админ-меню:", admin_keyboards.admin_root_keyboard())
    await callback.answer()


@router.callback_query(F.data == "pj:back")
async def go_project_back(callback: CallbackQuery, state: FSMContext) -> None:
    project = await _current_project(callback, state)
    if project is None:
        return
    await _show_project(callback, state, project)
    await callback.answer()


@router.callback_query(ProjectStates.detail, F.data == "pj:status")
async def status_menu(callback: CallbackQuery, state: FSMContext) -> None:
    if not await _current_project(callback, state):
        return
    await flow.step_from_callback(callback, state, "Выберите статус проекта:",
                                  kb.choice_keyboard("status", list(project_store.STATUSES.items())))
    await callback.answer()


@router.callback_query(ProjectStates.detail, F.data.startswith("pj:status:"))
async def set_status(callback: CallbackQuery, state: FSMContext) -> None:
    project = await _current_project(callback, state)
    if project is None:
        return
    try:
        changed = await project_store.mutate(callback.message.chat.id, project["id"], "status", callback.data.split(":")[-1])
    except ValueError as exc:
        await callback.answer(str(exc), show_alert=True)
        return
    await _show_project(callback, state, changed)
    await callback.answer("Статус сохранён")


@router.callback_query(ProjectStates.detail, F.data == "pj:stage")
async def stage_menu(callback: CallbackQuery, state: FSMContext) -> None:
    project = await _current_project(callback, state)
    if project is None:
        return
    choices = [(str(i), stage) for i, stage in enumerate(project["stages"])]
    await flow.step_from_callback(callback, state, "Текущий этап работы:", kb.choice_keyboard("stage", choices))
    await callback.answer()


@router.callback_query(ProjectStates.detail, F.data.startswith("pj:stage:"))
async def set_stage(callback: CallbackQuery, state: FSMContext) -> None:
    project = await _current_project(callback, state)
    if project is None:
        return
    try:
        index = int(callback.data.split(":")[-1])
        changed = await project_store.mutate(callback.message.chat.id, project["id"], "stage", index)
    except (ValueError, TypeError) as exc:
        await callback.answer(str(exc), show_alert=True)
        return
    await _show_project(callback, state, changed)
    await callback.answer("Этап сохранён")


@router.callback_query(ProjectStates.detail, F.data == "pj:price")
async def price_prompt(callback: CallbackQuery, state: FSMContext) -> None:
    if not await _current_project(callback, state):
        return
    await flow.step_from_callback(callback, state,
                                  "Введите согласованную стоимость в рублях целым числом (например, 60000):",
                                  kb.cancel_keyboard())
    await state.set_state(ProjectStates.price_input)
    await callback.answer()


@router.message(ProjectStates.price_input, F.text)
async def price_input(message: Message, state: FSMContext) -> None:
    amount = _parse_rub(message.text)
    if amount is None:
        await flow.step_from_text(message, state, "Введите сумму от 1 до 100 000 000 ₽ целым числом.", kb.cancel_keyboard())
        return
    project_id = (await state.get_data()).get("project_id")
    try:
        changed = await project_store.mutate(message.chat.id, project_id, "price", amount)
    except ValueError as exc:
        await flow.step_from_text(message, state, escape(str(exc)), kb.cancel_keyboard())
        return
    await flow.step_from_text(message, state, _project_text(changed), kb.project_detail(changed))
    await state.set_state(ProjectStates.detail)


@router.callback_query(ProjectStates.detail, F.data == "pj:deadline")
async def deadline_prompt(callback: CallbackQuery, state: FSMContext) -> None:
    if not await _current_project(callback, state):
        return
    await flow.step_from_callback(callback, state,
                                  "Введите срок в формате ГГГГ-ММ-ДД (например, 2026-11-15) или «нет», чтобы снять:",
                                  kb.cancel_keyboard())
    await state.set_state(ProjectStates.deadline_input)
    await callback.answer()


@router.message(ProjectStates.deadline_input, F.text)
async def deadline_input(message: Message, state: FSMContext) -> None:
    raw = message.text.strip()
    value = None if raw.lower() in ("нет", "снять", "-") else raw
    project_id = (await state.get_data()).get("project_id")
    try:
        changed = await project_store.mutate(message.chat.id, project_id, "deadline", value)
    except ValueError as exc:
        await flow.step_from_text(message, state, escape(str(exc)), kb.cancel_keyboard())
        return
    await flow.step_from_text(message, state, _project_text(changed), kb.project_detail(changed))
    await state.set_state(ProjectStates.detail)


@router.callback_query(ProjectStates.detail, F.data == "pj:pay")
async def payment_prompt(callback: CallbackQuery, state: FSMContext) -> None:
    project = await _current_project(callback, state)
    if project is None:
        return
    if project.get("price_rub") is None:
        await callback.answer("Сначала укажите стоимость", show_alert=True)
        return
    outstanding = project["price_rub"] - project_store.total_paid(project)
    if outstanding == 0:
        await callback.answer("Проект полностью оплачен по учёту", show_alert=True)
        return
    await flow.step_from_callback(callback, state,
                                  f"Остаток {_rub(outstanding)}.\nВведите сумму реально полученного платежа в рублях:",
                                  kb.cancel_keyboard())
    await state.set_state(ProjectStates.payment_input)
    await callback.answer()


@router.message(ProjectStates.payment_input, F.text)
async def payment_input(message: Message, state: FSMContext) -> None:
    amount = _parse_rub(message.text)
    if amount is None:
        await flow.step_from_text(message, state, "Введите положительную целую сумму в рублях.", kb.cancel_keyboard())
        return
    project_id = (await state.get_data()).get("project_id")
    project = await project_store.get_project(message.chat.id, project_id)
    if project is None or project.get("price_rub") is None:
        await flow.step_from_text(message, state, "Проект или согласованная стоимость недоступны.", kb.cancel_keyboard())
        return
    remaining = project["price_rub"] - project_store.total_paid(project)
    if amount > remaining:
        await flow.step_from_text(message, state, f"Сумма больше остатка ({_rub(remaining)}). Введите меньшую сумму.", kb.cancel_keyboard())
        return
    await state.update_data(pending_amount=amount)
    await flow.step_from_text(
        message, state, f"Подтвердить получение {_rub(amount)}?\n"
        "Запись означает, что деньги уже проверены вне Telegram. "
        "Никакого списания средств бот не выполняет.",
        kb.confirm_keyboard("pay"),
    )
    await state.set_state(ProjectStates.payment_confirm)


@router.callback_query(ProjectStates.payment_confirm, F.data.startswith("pj:pay:"))
async def payment_confirm(callback: CallbackQuery, state: FSMContext) -> None:
    if callback.data.endswith(":no"):
        await go_project_back(callback, state)
        return
    if callback.data != "pj:pay:yes":
        await callback.answer("Неизвестное действие", show_alert=True)
        return
    data = await state.get_data()
    try:
        changed = await project_store.mutate(callback.message.chat.id, data["project_id"], "pay", data["pending_amount"])
    except (ValueError, KeyError) as exc:
        await callback.answer(str(exc), show_alert=True)
        return
    await state.update_data(pending_amount=None)
    await _show_project(callback, state, changed)
    await callback.answer("Платёж учтён")


@router.callback_query(ProjectStates.detail, F.data == "pj:payments")
async def payments_menu(callback: CallbackQuery, state: FSMContext) -> None:
    project = await _current_project(callback, state)
    if not project:
        return
    payments = project.get("payments", [])
    lines = [f"🧾 Платежи проекта {escape(project['id'])}:"]
    for payment in payments[-15:]:
        mark = "↩️ Аннулирован" if payment.get("voided_at") else "✅ Учтён"
        lines.append(f"{mark} №{payment['id']}: {_rub(payment['amount'])}, {payment['recorded_at'][:10]}")
    if not payments:
        lines.append("Пока нет платежей.")
    await flow.step_from_callback(callback, state, "\n".join(lines), kb.payments_keyboard(project))
    await callback.answer()


@router.callback_query(ProjectStates.detail, F.data.startswith("pj:void:"))
async def void_start(callback: CallbackQuery, state: FSMContext) -> None:
    project = await _current_project(callback, state)
    if not project:
        return
    try:
        payment_id = int(callback.data.split(":")[-1])
    except ValueError:
        await callback.answer("Неверный номер", show_alert=True)
        return
    payment = next((p for p in project.get("payments", []) if p["id"] == payment_id and not p.get("voided_at")), None)
    if payment is None:
        await callback.answer("Платёж уже аннулирован или не найден", show_alert=True)
        return
    await state.update_data(void_payment_id=payment_id)
    await flow.step_from_callback(callback, state,
                                  f"Аннулировать запись №{payment_id} на {_rub(payment['amount'])}?\n"
                                  "Это не возврат средств клиенту. Запись останется в истории.",
                                  kb.confirm_keyboard("void", destructive=True))
    await state.set_state(ProjectStates.void_confirm)
    await callback.answer()


@router.callback_query(ProjectStates.void_confirm, F.data.startswith("pj:void:"))
async def void_confirm(callback: CallbackQuery, state: FSMContext) -> None:
    if callback.data.endswith(":no"):
        await go_project_back(callback, state)
        return
    if callback.data != "pj:void:yes":
        await callback.answer("Неизвестное действие", show_alert=True)
        return
    data = await state.get_data()
    try:
        changed = await project_store.mutate(callback.message.chat.id, data["project_id"], "void", data["void_payment_id"])
    except (ValueError, KeyError) as exc:
        await callback.answer(str(exc), show_alert=True)
        return
    await _show_project(callback, state, changed)
    await callback.answer("Запись аннулирована")


@router.callback_query(ProjectStates.detail, F.data == "pj:history")
async def history_menu(callback: CallbackQuery, state: FSMContext) -> None:
    project = await _current_project(callback, state)
    if not project:
        return
    lines = ["🕘 История изменений:"]
    for event in project["history"][-18:]:
        lines.append(f"{event['at'][:16].replace('T', ' ')} — {escape(event['detail'])}")
    await flow.step_from_callback(callback, state, "\n".join(lines), kb.cancel_keyboard())
    await callback.answer()


@router.callback_query(ProjectStates.detail, F.data == "pj:delete")
async def delete_demo_start(callback: CallbackQuery, state: FSMContext) -> None:
    project = await _current_project(callback, state)
    if project is None:
        return
    if not project["is_demo"]:
        await callback.answer("Рабочие проекты нельзя удалить через демо-режим", show_alert=True)
        return
    await flow.step_from_callback(callback, state, "Удалить тестовый проект и его демо-платежи?",
                                  kb.confirm_keyboard("delete", destructive=True))
    await state.set_state(ProjectStates.delete_demo_confirm)
    await callback.answer()


@router.callback_query(ProjectStates.delete_demo_confirm, F.data.startswith("pj:delete:"))
async def delete_demo_confirm(callback: CallbackQuery, state: FSMContext) -> None:
    if callback.data.endswith(":no"):
        await go_project_back(callback, state)
        return
    project_id = (await state.get_data()).get("project_id")
    success = await project_store.delete_demo(callback.message.chat.id, project_id)
    if not success:
        await callback.answer("Нельзя удалить рабочий проект", show_alert=True)
        return
    await _show_list(callback, state)
    await callback.answer("Тестовый проект удалён")


@router.message(StateFilter(ProjectStates), Command("cancel"))
@router.message(StateFilter(ProjectStates), F.text == texts.MAIN_MENU_BUTTON)
async def cancel_command(message: Message, state: FSMContext) -> None:
    await flow.open_root(message, state, "Админ-меню:", admin_keyboards.admin_root_keyboard())
    await state.set_state(None)
