from aiogram import F, Router
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import Message

from bot import config, content_store, flow, texts
from bot.keyboards import reply_keyboard_for_chat, webapp_open_keyboard

router = Router(name="start")


def _reply_keyboard_for(message: Message):
    return reply_keyboard_for_chat(message.chat.id)


@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext) -> None:
    await flow.open_root(message, state, texts.WELCOME, _reply_keyboard_for(message))


@router.message(Command("id"))
async def cmd_id(message: Message) -> None:
    # Не через flow.open_root — Markdown-форматирование (`chat_id` в
    # обратных кавычках) не проходит через универсальный open_root, который
    # не принимает parse_mode; расширять ради одной команды не стали
    # (см. Part 17 ТЗ — не переписывать без необходимости). RULE 1
    # (удаление триггера) применяем отдельно, best-effort.
    await flow.delete_trigger(message)
    await message.answer(texts.MY_ID_TEMPLATE.format(chat_id=message.chat.id), parse_mode="Markdown")


@router.message(Command("portfolio"))
async def cmd_portfolio(message: Message, state: FSMContext) -> None:
    await flow.open_root(message, state, "Открыть портфолио:", webapp_open_keyboard(config.WEBAPP_URL, "portfolio", "📁 Открыть портфолио"))
    await flow.refresh_reply_keyboard(message, _reply_keyboard_for(message))


@router.message(Command("about"))
async def cmd_about(message: Message, state: FSMContext) -> None:
    await flow.open_root(message, state, "Открыть «Обо мне»:", webapp_open_keyboard(config.WEBAPP_URL, "about", "👤 Открыть «Обо мне»"))
    await flow.refresh_reply_keyboard(message, _reply_keyboard_for(message))


@router.message(Command("brief"))
async def cmd_brief(message: Message, state: FSMContext) -> None:
    await flow.open_root(message, state, "Открыть заявку:", webapp_open_keyboard(config.WEBAPP_URL, "brief", "✍️ Оставить заявку"))
    await flow.refresh_reply_keyboard(message, _reply_keyboard_for(message))


@router.message(Command("cancel"))
async def cmd_cancel(message: Message, state: FSMContext) -> None:
    # Раньше проверялось FSM-состояние BriefStates.awaiting_tz_file — теперь
    # "жду файл" хранится persistent в самой заявке (см.
    # content_store.find_lead_awaiting_file/mark_tz_file_received,
    # bot/handlers/webapp.py::handle_tz_file), переживает рестарт бота.
    # /cancel здесь просто снимает флаг ожидания — заявка остаётся как есть.
    awaiting_lead = content_store.find_lead_awaiting_file(message.from_user.id)
    if awaiting_lead is not None:
        content_store.mark_tz_file_received(awaiting_lead["id"])
        text = "Хорошо, отменил ожидание файла."
    else:
        text = "Отменять было нечего."
    await flow.open_root(message, state, text, _reply_keyboard_for(message))


@router.message(F.text == texts.OPEN_APP_BUTTON)
async def open_app_button(message: Message, state: FSMContext) -> None:
    # Reply-кнопка — только триггер (не web_app, см. main_reply_keyboard);
    # реальный запуск Mini App — этой inline-кнопкой, тем же путём, что и
    # /portfolio (уже подтверждено production-тестами: initData приходит).
    await flow.open_root(
        message, state,
        "Открыть приложение:",
        webapp_open_keyboard(config.WEBAPP_URL, "portfolio", texts.OPEN_APP_BUTTON),
    )
    await flow.refresh_reply_keyboard(message, _reply_keyboard_for(message))


# Держим последним в этом роутере: любой не распознанный текст — подсказка меню.
@router.message(F.text)
async def fallback_text(message: Message, state: FSMContext) -> None:
    await flow.open_root(
        message, state,
        "Не совсем поняла 🙂 Воспользуйтесь кнопками ниже.",
        _reply_keyboard_for(message),
    )
