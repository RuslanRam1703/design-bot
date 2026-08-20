from aiogram import F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from bot import config, content_store, flow, texts
from bot.keyboards import main_menu_confirm_keyboard, webapp_open_keyboard

router = Router(name="start")


@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext) -> None:
    # NAV anchor (persistent reply-клавиатура) и TRANSIENT-экран — теперь
    # разделены (см. bot/flow.py, UX-аудит про исчезающую клавиатуру после
    # FAQ/admin). /start не отправляет WELCOME отдельным сообщением — он
    # либо создаёт NAV anchor с этим текстом (первое взаимодействие в
    # чате), либо просто редактирует уже существующий обратно в WELCOME.
    await flow.reset_nav_screen(message, state)


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
    # NAV anchor (persistent-клавиатура) гарантируется автоматически внутри
    # flow.open_root/open_flow (см. bot/flow.py::ensure_nav_anchor) —
    # отдельно заботиться о ней здесь не нужно.
    await flow.open_root(message, state, "Открыть портфолио:", webapp_open_keyboard(config.WEBAPP_URL, "portfolio", "📁 Открыть портфолио"))


@router.message(Command("about"))
async def cmd_about(message: Message, state: FSMContext) -> None:
    await flow.open_root(message, state, "Открыть «Обо мне»:", webapp_open_keyboard(config.WEBAPP_URL, "about", "👤 Открыть «Обо мне»"))


@router.message(Command("brief"))
async def cmd_brief(message: Message, state: FSMContext) -> None:
    await flow.open_root(message, state, "Открыть заявку:", webapp_open_keyboard(config.WEBAPP_URL, "brief", "✍️ Оставить заявку"))


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
    # reply_markup=None — TRANSIENT-экран не несёт persistent-клавиатуру,
    # её уже обеспечивает NAV anchor (см. bot/flow.py).
    await flow.open_root(message, state, text, None)


@router.message(F.text == texts.OPEN_APP_BUTTON)
async def open_app_button(message: Message, state: FSMContext) -> None:
    # "🚀 Открыть приложение" — этой кнопки больше нет в main_reply_keyboard
    # (прямой запуск теперь через Telegram Menu Button, см. bot/main.py::
    # _setup_menu_button), но handler оставлен: у части клиентов может быть
    # закэширована старая reply-клавиатура ДО этого деплоя — если такая
    # кнопка всё же придёт текстом, она по-прежнему должна работать, а не
    # тихо проваливаться в fallback_text.
    await flow.open_root(
        message, state,
        "Открыть приложение:",
        webapp_open_keyboard(config.WEBAPP_URL, "portfolio", texts.OPEN_APP_BUTTON),
    )


async def main_menu_or_confirm(message: Message, state: FSMContext) -> None:
    """Общая логика "⌂ Главное меню" для клиента (этот router) и владельца
    (bot/handlers/admin.py::admin_main_menu_button — тот же текст кнопки,
    но зарегистрирован отдельно и РАНО в admin.py, до AdminStates.*, F.text
    мастеров, иначе они проглотили бы текст кнопки как обычный ввод — см.
    bot/keyboards.py::main_reply_keyboard).

    Нет активного bot/FSM-состояния — терять нечего, просто сбрасываем
    TRANSIENT-экран и триггер (см. flow.main_menu_cleanup) — БЕЗ WELCOME:
    "Главное меню" не /start, NAV anchor уже существует и его не нужно
    трогать. Есть — показываем подтверждение (inline, без отдельного
    долгоживущего state под саму confirmation, см.
    bot/keyboards.py::main_menu_confirm_keyboard)."""
    if await state.get_state() is None:
        await flow.main_menu_cleanup(message, state)
        return
    await flow.delete_trigger(message)
    # NAV anchor не затронут этим экраном — освежать нечего (см. bot/flow.py).
    await message.answer(texts.MAIN_MENU_CONFIRM_TEXT, reply_markup=main_menu_confirm_keyboard())


@router.message(F.text == texts.MAIN_MENU_BUTTON)
async def main_menu_button(message: Message, state: FSMContext) -> None:
    await main_menu_or_confirm(message, state)


@router.callback_query(F.data == "mainmenu:confirm")
async def main_menu_confirm(callback: CallbackQuery, state: FSMContext) -> None:
    # callback.message — то самое confirmation-сообщение; main_menu_cleanup
    # сам удалит его как "триггер" (RULE 1). NAV anchor не трогаем — это
    # "Главное меню", не /start (см. bot/flow.py::main_menu_cleanup).
    await flow.main_menu_cleanup(callback.message, state)
    await callback.answer()


@router.callback_query(F.data == "mainmenu:decline")
async def main_menu_decline(callback: CallbackQuery, state: FSMContext) -> None:
    # Ничего не сбрасываем — только убираем сам вопрос; экран/состояние,
    # которые были ДО "Главное меню", остаются как есть.
    try:
        await callback.message.delete()
    except TelegramAPIError:
        pass
    await callback.answer()


# Держим последним в этом роутере: любой не распознанный текст — подсказка меню.
@router.message(F.text)
async def fallback_text(message: Message, state: FSMContext) -> None:
    await flow.open_root(
        message, state,
        "Не совсем поняла 🙂 Воспользуйтесь кнопками ниже.",
        None,
    )
