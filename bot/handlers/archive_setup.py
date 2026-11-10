"""Owner-only command to discover the private archive group's numeric chat ID."""
from aiogram import Router
from aiogram.filters import Command
from aiogram.types import Message

from bot import config

router = Router(name="archive_setup")


@router.message(Command("archiveid"))
async def archive_id(message: Message) -> None:
    if not message.from_user or str(message.from_user.id) != str(config.DESIGNER_CHAT_ID):
        return
    if message.chat.type not in ("group", "supergroup"):
        await message.answer("Отправьте /archiveid внутри приватной группы архива.")
        return
    await message.answer(
        f"ID группы: <code>{message.chat.id}</code>\n"
        "Укажите это значение в CLIENT_MATERIALS_CHANNEL на Render. "
        "Не публикуйте ID группы и ссылку-приглашение в открытых источниках.",
        parse_mode="HTML",
    )
