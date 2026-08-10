from aiogram import F, Router
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import Message

from bot import config, texts
from bot.keyboards import main_menu_keyboard

router = Router(name="start")


@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer(texts.WELCOME, reply_markup=main_menu_keyboard(config.WEBAPP_URL))


@router.message(Command("id"))
async def cmd_id(message: Message) -> None:
    await message.answer(texts.MY_ID_TEMPLATE.format(chat_id=message.chat.id), parse_mode="Markdown")


@router.message(Command("cancel"))
async def cmd_cancel(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer("Хорошо, отменил ожидание файла.", reply_markup=main_menu_keyboard(config.WEBAPP_URL))


# Держим последним в этом роутере: любой не распознанный текст — подсказка меню.
@router.message(F.text)
async def fallback_text(message: Message) -> None:
    await message.answer(
        "Не совсем поняла 🙂 Воспользуйтесь кнопками меню ниже.",
        reply_markup=main_menu_keyboard(config.WEBAPP_URL),
    )
