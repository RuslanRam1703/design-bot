from aiogram import F, Router
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import Message

from bot import config, flow, texts
from bot.data import load_ui_config
from bot.keyboards import main_menu_keyboard, webapp_open_keyboard
from bot.states import BriefStates

router = Router(name="start")


def _menu_keyboard():
    return main_menu_keyboard(config.WEBAPP_URL, load_ui_config())


@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext) -> None:
    await flow.open_root(message, state, texts.WELCOME, _menu_keyboard())


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


@router.message(Command("about"))
async def cmd_about(message: Message, state: FSMContext) -> None:
    await flow.open_root(message, state, "Открыть «Обо мне»:", webapp_open_keyboard(config.WEBAPP_URL, "about", "👤 Открыть «Обо мне»"))


@router.message(Command("calculator"))
async def cmd_calculator(message: Message, state: FSMContext) -> None:
    # Расчёт стоимости больше не отдельный экран — это шаг 1 заявки (Order
    # Builder). Команда /calculator осталась в меню команд Telegram (нельзя
    # убрать существующую команду без риска "неизвестная команда" у тех, кто
    # успел её сохранить/ввести привычкой), но ведёт теперь туда же, куда
    # /brief — устраняет параллельный старый сценарий, см. bot/keyboards.py.
    await flow.open_root(message, state, "Расчёт стоимости — теперь часть оформления заявки. Открываю заявку:", webapp_open_keyboard(config.WEBAPP_URL, "brief", "✍️ Оставить заявку"))


@router.message(Command("brief"))
async def cmd_brief(message: Message, state: FSMContext) -> None:
    await flow.open_root(message, state, "Открыть заявку:", webapp_open_keyboard(config.WEBAPP_URL, "brief", "✍️ Оставить заявку"))


@router.message(Command("cancel"))
async def cmd_cancel(message: Message, state: FSMContext) -> None:
    was_awaiting_file = (await state.get_state()) == BriefStates.awaiting_tz_file.state
    text = "Хорошо, отменил ожидание файла." if was_awaiting_file else "Отменять было нечего."
    await flow.open_root(message, state, text, _menu_keyboard())


# Держим последним в этом роутере: любой не распознанный текст — подсказка меню.
@router.message(F.text)
async def fallback_text(message: Message, state: FSMContext) -> None:
    await flow.open_root(
        message, state,
        "Не совсем поняла 🙂 Воспользуйтесь кнопками меню ниже.",
        _menu_keyboard(),
    )
