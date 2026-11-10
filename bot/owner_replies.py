"""One durable delivery path for private admin cards and archive /reply."""
import asyncio
import logging

from aiogram.exceptions import TelegramAPIError, TelegramNetworkError
from bot import access_control, content_store

logger = logging.getLogger(__name__)

RESULT_TEXT = {
    "sent": "Сообщение отправлено клиенту ✅",
    "failed": "⚠️ Не получилось отправить — клиент мог заблокировать бота. Ответ сохранён в истории заявки.",
    "unknown": "⚠️ Доставка не подтверждена. Проверьте переписку перед повторной отправкой.",
    "duplicate": "Это сообщение уже обработано или отправляется. Повторная отправка отменена.",
    "blocked": "Пользователь заблокирован: отправка отменена.",
    "unavailable": "Не удалось сохранить ответ. Отправка отменена. Попробуйте позже.",
    "invalid": "Напишите сообщение до 4000 символов.",
}


async def deliver_owner_reply(message, lead: dict, text: str) -> str:
    actor = message.from_user.id
    content_store._require_designer(actor)
    text = text.strip()
    if not text or len(text.encode("utf-16-le")) // 2 > 4000:
        return "invalid"
    recipient = lead.get("telegram", {})
    if not recipient.get("user_id"):
        return "unavailable"
    if await access_control.is_blocked(recipient["user_id"], recipient.get("username")):
        return "blocked"
    source_key = f"{message.chat.id}:{message.message_id}"
    try:
        entry = await content_store.prepare_owner_reply(actor, lead["id"], source_key, text)
    except Exception:
        logger.exception("Could not persist reply intent")
        return "unavailable"
    if entry is None:
        return "duplicate"
    try:
        await message.bot.send_message(
            chat_id=entry["recipient_id"],
            text=f"Ответ по заявке #{lead['id']}\n\n{entry['text']}",
            parse_mode=None,
        )
        status = "sent"
    except (TelegramNetworkError, asyncio.TimeoutError):
        status = "unknown"
    except TelegramAPIError:
        logger.exception("Reply delivery rejected")
        status = "failed"
    except Exception:
        logger.exception("Unexpected reply delivery result")
        status = "unknown"
    try:
        await content_store.finish_owner_reply(actor, lead["id"], source_key, status)
    except Exception:
        # Intent remains 'sending'; replay stays blocked, even after restart.
        logger.exception("Could not persist reply result")
        return "unknown"
    return status
