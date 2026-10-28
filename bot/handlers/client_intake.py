"""Native Telegram question and quick-lead workflows.

Only callbacks create structured leads. Free-text questions reuse the
existing relay without turning informal conversations into CRM records.
Full briefs and calculations remain in Mini App unchanged.
"""
import logging
import uuid
from html import escape

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message

from bot import config, content_store, flow, texts
from bot.keyboards import (
    client_home_keyboard, intake_cancel_keyboard,
    quick_contact_keyboard, quick_lead_confirm_keyboard,
)
from bot.notifications import notify_designer_lead

logger = logging.getLogger(__name__)
router = Router(name="client_intake")
_TASK_MIN = 10
_TASK_MAX = 1500
_CONTACT_MAX = 180


class QuestionStates(StatesGroup):
    awaiting_question = State()


class QuickLeadStates(StatesGroup):
    awaiting_task = State()
    awaiting_contact = State()
    confirmation = State()


def _telegram_contact(user) -> str:
    return f"@{user.username}" if user.username else f"Telegram ID: {user.id}"


def _preview(task: str, contact: str) -> str:
    return (
        "⚡ Быстрая заявка\n\n"
        f"Задача: {escape(task)}\n\n"
        f"Как связаться: {escape(contact)}\n\n"
        "Проверьте данные перед отправкой. Стоимость и сроки "
        "согласуем после обсуждения."
    )


@router.callback_query(F.data == "intake:question")
async def question_start(callback: CallbackQuery, state: FSMContext):
    if not config.DESIGNER_CHAT_ID:
        await callback.answer("Связь с дизайнером временно недоступна", show_alert=True)
        return
    await state.set_state(QuestionStates.awaiting_question)
    await flow.step_from_callback(
        callback, state,
        "💬 Напишите вопрос одним сообщением. Дизайнер получит его в Telegram.\n\n"
        "Для выхода нажмите «К выбору действий».",
        intake_cancel_keyboard(),
    )
    await callback.answer()


@router.message(QuestionStates.awaiting_question, F.text,
                ~F.text.startswith("/"), F.text != texts.MAIN_MENU_BUTTON,
                F.text != texts.MENU_FAQ, F.text != texts.ADMIN_BUTTON)
async def question_receive(message: Message, state: FSMContext):
    if not message.text.strip():
        await message.answer("Введите вопрос текстом или отмените действие.")
        return
    from bot.handlers.start import relay_client_text_to_designer
    # Uses the same owner guard, Telegram identity, active-lead references
    # and error handling as the existing free-text relay.
    await relay_client_text_to_designer(message, state)
    await state.set_state(None)


@router.callback_query(F.data == "intake:quick")
async def quick_start(callback: CallbackQuery, state: FSMContext):
    await state.update_data(quick_draft=uuid.uuid4().hex, quick_task=None, quick_contact=None)
    await state.set_state(QuickLeadStates.awaiting_task)
    await flow.step_from_callback(
        callback, state,
        "⚡ Быстрая заявка\n\nКоротко опишите, что нужно сделать (не менее 10 символов). "
        "Расчёт и подробное ТЗ пока необязательны.",
        intake_cancel_keyboard(),
    )
    await callback.answer()


@router.callback_query(F.data == "intake:task")
async def quick_edit_task(callback: CallbackQuery, state: FSMContext):
    if await state.get_state() != QuickLeadStates.confirmation.state:
        await callback.answer("Откройте заявку заново", show_alert=True)
        return
    await state.set_state(QuickLeadStates.awaiting_task)
    await flow.step_from_callback(
        callback, state, "Опишите задачу заново одним сообщением.", intake_cancel_keyboard(),
    )
    await callback.answer()


@router.message(QuickLeadStates.awaiting_task, F.text,
                ~F.text.startswith("/"), F.text != texts.MAIN_MENU_BUTTON,
                F.text != texts.MENU_FAQ, F.text != texts.ADMIN_BUTTON)
async def quick_task_receive(message: Message, state: FSMContext):
    task = message.text.strip()
    if not _TASK_MIN <= len(task) <= _TASK_MAX:
        await flow.step_from_text(
            message, state, "Описание должно содержать от 10 до 1500 символов. Попробуйте ещё раз.",
            intake_cancel_keyboard(),
        )
        return
    await state.update_data(quick_task=task)
    await state.set_state(QuickLeadStates.confirmation)
    data = await state.get_data()
    await flow.step_from_text(
        message, state,
        _preview(task, data.get("quick_contact") or _telegram_contact(message.from_user)),
        quick_lead_confirm_keyboard(),
    )


@router.callback_query(F.data == "intake:contact")
async def quick_contact_start(callback: CallbackQuery, state: FSMContext):
    if await state.get_state() != QuickLeadStates.confirmation.state:
        await callback.answer("Заявка не активна", show_alert=True)
        return
    await state.set_state(QuickLeadStates.awaiting_contact)
    await flow.step_from_callback(
        callback, state,
        "Укажите Telegram @username, телефон или другой способ связи. "
        "Либо оставьте контакт Telegram, с которого пишете.",
        quick_contact_keyboard(),
    )
    await callback.answer()


@router.message(QuickLeadStates.awaiting_contact, F.text,
                ~F.text.startswith("/"), F.text != texts.MAIN_MENU_BUTTON,
                F.text != texts.MENU_FAQ, F.text != texts.ADMIN_BUTTON)
async def quick_contact_receive(message: Message, state: FSMContext):
    contact = message.text.strip()
    if not 3 <= len(contact) <= _CONTACT_MAX:
        await flow.step_from_text(
            message, state, "Контакт должен содержать от 3 до 180 символов.",
            quick_contact_keyboard(),
        )
        return
    await state.update_data(quick_contact=contact)
    await state.set_state(QuickLeadStates.confirmation)
    data = await state.get_data()
    await flow.step_from_text(message, state, _preview(data["quick_task"], contact), quick_lead_confirm_keyboard())


@router.callback_query(F.data == "intake:use_telegram")
async def quick_use_telegram(callback: CallbackQuery, state: FSMContext):
    if await state.get_state() != QuickLeadStates.awaiting_contact.state:
        await callback.answer("Заявка не активна", show_alert=True)
        return
    await state.update_data(quick_contact=None)
    await state.set_state(QuickLeadStates.confirmation)
    data = await state.get_data()
    await flow.step_from_callback(
        callback, state,
        _preview(data["quick_task"], _telegram_contact(callback.from_user)),
        quick_lead_confirm_keyboard(),
    )
    await callback.answer()


@router.callback_query(F.data == "intake:submit")
async def quick_submit(callback: CallbackQuery, state: FSMContext):
    if await state.get_state() != QuickLeadStates.confirmation.state:
        await callback.answer("Заявка уже отправлена или отменена", show_alert=True)
        return
    data = await state.get_data()
    task = data.get("quick_task")
    draft_id = data.get("quick_draft")
    if not task or not draft_id:
        await callback.answer("Начните заявку заново", show_alert=True)
        return

    user = callback.from_user
    payload = {
        "service_id": "unknown",
        "service_name": "Задача для оценки",
        "task_description": task,
        "contact": data.get("quick_contact") or _telegram_contact(user),
        "source": "quick",
        "have": [], "deadline": "unknown", "budget": "undecided",
        "attach_tz": False,
    }
    telegram = {
        "user_id": user.id, "username": user.username,
        "first_name": user.first_name, "last_name": user.last_name,
    }
    try:
        lead = await content_store.add_lead(payload, telegram, draft_id=draft_id)
    except Exception:
        logger.exception("Quick-lead persistence failed")
        await callback.answer("Не удалось сохранить заявку. Попробуйте ещё раз.", show_alert=True)
        return

    # Use the same persisted Telegram delivery mechanism as Mini App leads.
    if lead["created"] or lead.get("changed") or (lead.get("designer_notification") or {}).get("state") != "sent":
        try:
            await notify_designer_lead(callback.bot, lead)
        except Exception:
            logger.exception("Quick-lead delivery deferred for #%s", lead["id"])
    await flow.step_from_callback(
        callback, state,
        texts.lead_received_ack(lead["id"], "Задача для оценки", None),
        client_home_keyboard(config.WEBAPP_URL),
    )
    await state.set_state(None)
    await callback.answer()


@router.callback_query(F.data == "intake:cancel")
async def intake_cancel(callback: CallbackQuery, state: FSMContext):
    await state.set_state(None)
    await flow.step_from_callback(
        callback, state, texts.CLIENT_HOME, client_home_keyboard(config.WEBAPP_URL),
    )
    await callback.answer()


@router.message(QuestionStates.awaiting_question, Command("cancel"))
@router.message(QuickLeadStates, Command("cancel"))
async def intake_cancel_command(message: Message, state: FSMContext):
    await flow.open_root(message, state, texts.CLIENT_HOME, client_home_keyboard(config.WEBAPP_URL))
