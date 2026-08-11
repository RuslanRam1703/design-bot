from aiogram import F, Router
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import Message

from bot import config, texts
from bot.data import load_ui_config
from bot.keyboards import main_menu_keyboard, webapp_open_keyboard

router = Router(name="start")


def _menu_keyboard():
    return main_menu_keyboard(config.WEBAPP_URL, load_ui_config())


@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer(texts.WELCOME, reply_markup=_menu_keyboard())


@router.message(Command("id"))
async def cmd_id(message: Message) -> None:
    await message.answer(texts.MY_ID_TEMPLATE.format(chat_id=message.chat.id), parse_mode="Markdown")


@router.message(Command("portfolio"))
async def cmd_portfolio(message: Message) -> None:
    await message.answer("Открыть портфолио:", reply_markup=webapp_open_keyboard(config.WEBAPP_URL, "portfolio", "📁 Открыть портфолио"))


@router.message(Command("about"))
async def cmd_about(message: Message) -> None:
    await message.answer("Открыть «Обо мне»:", reply_markup=webapp_open_keyboard(config.WEBAPP_URL, "about", "👤 Открыть «Обо мне»"))


@router.message(Command("calculator"))
async def cmd_calculator(message: Message) -> None:
    await message.answer("Открыть калькулятор:", reply_markup=webapp_open_keyboard(config.WEBAPP_URL, "calculator", "💰 Открыть калькулятор"))


@router.message(Command("brief"))
async def cmd_brief(message: Message) -> None:
    await message.answer("Открыть заявку:", reply_markup=webapp_open_keyboard(config.WEBAPP_URL, "brief", "✍️ Оставить заявку"))


@router.message(Command("cancel"))
async def cmd_cancel(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer("Хорошо, отменил ожидание файла.", reply_markup=_menu_keyboard())


# Держим последним в этом роутере: любой не распознанный текст — подсказка меню.
@router.message(F.text)
async def fallback_text(message: Message) -> None:
    await message.answer(
        "Не совсем поняла 🙂 Воспользуйтесь кнопками меню ниже.",
        reply_markup=_menu_keyboard(),
    )
