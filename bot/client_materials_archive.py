"""Private Telegram forum archive: one topic per lead, best-effort delivery.

The canonical lead attachment stays in content_store. The archive is a
secondary copy and must never cause an accepted client file to be lost.
"""
import asyncio
import logging

from bot import config, content_store

logger = logging.getLogger(__name__)
_local_locks = {}


def _chat_id():
    value = config.CLIENT_MATERIALS_CHANNEL
    if not value:
        return None
    return int(value)


async def archive_lead_material(message, lead_id: int | str) -> bool:
    """Copy a client message to the lead's private forum topic.

    Returns False if archive is not configured or unavailable. Never exposes
    a private group ID or attachment to the client.
    """
    try:
        chat_id = _chat_id()
        if chat_id is None:
            return False
        if not config.UPSTASH_REDIS_REST_URL or not config.UPSTASH_REDIS_REST_TOKEN:
            logger.warning("Client archive requires persistent Upstash mapping")
            return False
        key = f"design-bot:client-materials:topic:{chat_id}:{lead_id}"
        lock = _local_locks.setdefault((chat_id, str(lead_id)), asyncio.Lock())
        async with lock:
            raw = await asyncio.to_thread(content_store._upstash_command, "GET", key)
            if raw is None:
                topic = await message.bot.create_forum_topic(
                    chat_id=chat_id, name=f"Заявка #{lead_id}",
                )
                topic_id = topic.message_thread_id
                inserted = await asyncio.to_thread(
                    content_store._upstash_command, "SET", key, str(topic_id), "NX",
                )
                if inserted != "OK":
                    # Another instance created the canonical topic first.
                    raw = await asyncio.to_thread(content_store._upstash_command, "GET", key)
                    if raw is None:
                        raise RuntimeError("Archive topic mapping missing after conflict")
                    await message.bot.delete_forum_topic(
                        chat_id=chat_id, message_thread_id=topic_id,
                    )
                    topic_id = int(raw)
            else:
                topic_id = int(raw)
        await message.bot.copy_message(
            chat_id=chat_id,
            from_chat_id=message.chat.id,
            message_id=message.message_id,
            message_thread_id=topic_id,
        )
        return True
    except Exception:
        logger.exception("Could not archive material for lead #%s", lead_id)
        return False
