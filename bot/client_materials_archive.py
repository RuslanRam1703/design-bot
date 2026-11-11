"""Private Telegram forum archive: one topic per lead, persistent delivery journal.

The canonical lead attachment stays in content_store. The archive is a
secondary copy and must never cause an accepted client file to be lost.
"""
import asyncio
import logging
from types import SimpleNamespace

from aiogram.exceptions import TelegramBadRequest, TelegramAPIError, TelegramNetworkError, TelegramServerError

from bot import config, content_store

logger = logging.getLogger(__name__)
_local_locks = {}


def _chat_id():
    value = config.CLIENT_MATERIALS_CHANNEL
    if not value:
        return None
    return int(value)


async def archive_lead_material(message, lead_id: int | str, *, retry: bool = False, manual: bool = False) -> bool:
    """Copy a client message to the lead's private forum topic.

    Returns False if archive is not configured or unavailable. Never exposes
    a private group ID or attachment to the client.
    """
    entry = None
    copy_started = False
    key = None
    topic_id = None
    try:
        chat_id = _chat_id()
        if chat_id is None:
            return False
        if not config.UPSTASH_REDIS_REST_URL or not config.UPSTASH_REDIS_REST_TOKEN:
            logger.warning("Client archive requires persistent Upstash mapping")
            return False
        entry = await content_store.prepare_archive_copy(
            int(lead_id), message.chat.id, message.message_id, chat_id, retry=retry, manual=manual,
        )
        if entry is None:
            return False
        if not entry["claimed"]:
            return entry["state"] == "sent"
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
        copy_started = True
        await message.bot.copy_message(
            chat_id=chat_id,
            from_chat_id=message.chat.id,
            message_id=message.message_id,
            message_thread_id=topic_id,
        )
        await content_store.finish_archive_copy(int(lead_id), entry["key"], entry["claim_token"], "sent")
        return True
    except Exception as exc:
        logger.exception("Could not archive material for lead #%s", lead_id)
        if (key and topic_id is not None and isinstance(exc, TelegramBadRequest)
                and any(token in str(exc).lower() for token in ('message thread not found', 'topic_id_invalid', 'topic_deleted'))):
            try:
                # Only forget this exact vanished topic, never a concurrently replaced mapping.
                await asyncio.to_thread(content_store._upstash_command, 'EVAL',
                    "if redis.call('GET', KEYS[1]) == ARGV[1] then return redis.call('DEL', KEYS[1]) else return 0 end",
                    1, key, str(topic_id))
            except Exception:
                logger.exception("Could not detach missing archive topic")
        if entry and entry.get("claimed"):
            # Before copy_message, no material can have been copied. Once it
            # started, only an explicit Bot API rejection is safe to retry.
            rejected = isinstance(exc, TelegramAPIError) and not isinstance(exc, (TelegramNetworkError, TelegramServerError))
            state = "failed" if not copy_started or rejected else "unknown"
            try:
                await content_store.finish_archive_copy(int(lead_id), entry["key"], entry["claim_token"], state)
            except Exception:
                logger.exception("Could not persist archive delivery result")
        return False


async def retry_archive_copies(bot, *, lead_id: int | None = None, manual: bool = False) -> int:
    """Retry known failed copies only; never resend to customers or change groups."""
    if _chat_id() is None:
        return 0
    sent = 0
    for target_lead_id, record in await content_store.pending_archive_copies(lead_id=lead_id, manual=manual):
        if record["archive_chat_id"] != _chat_id():
            continue
        message = SimpleNamespace(bot=bot, chat=SimpleNamespace(id=record["source_chat_id"]),
                                  message_id=record["message_id"])
        if await archive_lead_material(message, target_lead_id, retry=True, manual=manual):
            sent += 1
    return sent
