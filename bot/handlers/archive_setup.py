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


@router.message(Command("reply"))
async def reply_from_archive_topic(message: Message) -> None:
    """Owner reply from a private forum topic; command works with Telegram privacy mode."""
    if not message.from_user or str(message.from_user.id) != str(config.DESIGNER_CHAT_ID):
        return
    if not config.CLIENT_MATERIALS_CHANNEL or str(message.chat.id) != str(config.CLIENT_MATERIALS_CHANNEL):
        return
    thread_id = getattr(message, "message_thread_id", None)
    if not thread_id or not getattr(message, "is_topic_message", False):
        await message.answer("Откройте тему заявки и отправьте /reply текст ответа.")
        return
    text = (message.text or "").partition(" ")[2].strip()
    if not text:
        await message.answer("Напишите /reply и текст сообщения для клиента.")
        return
    from bot import content_store
    if not config.UPSTASH_REDIS_REST_URL or not config.UPSTASH_REDIS_REST_TOKEN:
        await message.answer("Архив не подключён к постоянному хранилищу.")
        return
    import asyncio
    import logging
    logger = logging.getLogger(__name__)
    try:
        leads = await content_store.list_leads()
        matched = []
        for lead in leads:
            key = f"design-bot:client-materials:topic:{message.chat.id}:{lead['id']}"
            raw = await asyncio.to_thread(content_store._upstash_command, "GET", key)
            if raw is not None and str(raw) == str(thread_id):
                matched.append(lead)
        if len(matched) != 1:
            await message.answer("Не удалось однозначно определить заявку для этой темы.")
            return
        lead = matched[0]
        from bot.owner_replies import deliver_owner_reply, RESULT_TEXT
        status = await deliver_owner_reply(message, lead, text)
        await message.answer(RESULT_TEXT[status])
    except Exception:
        logger.exception("Archive topic reply failed")
        await message.answer("Ошибка обработки ответа. Повторите позже.")
