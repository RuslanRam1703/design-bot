"""Opt-in real Telegram smoke test for a private client materials forum.

Triggered once by ARCHIVE_SMOKE_TEST_ID (unique non-secret run identifier).
Does not fabricate a client identity or create a real lead.
"""
import asyncio
import base64
import logging
import os
from datetime import datetime, timezone

from aiogram.types import BufferedInputFile
from bot import config, content_store

logger = logging.getLogger(__name__)
# Valid 1x1 PNG; no third-party image library or external URL needed.
PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+/lqkAAAAASUVORK5CYII="
)


async def run_archive_smoke(bot) -> None:
    run_id = os.getenv("ARCHIVE_SMOKE_TEST_ID", "").strip()
    if not run_id:
        return
    if not run_id.isascii() or not run_id.replace("-", "").replace("_", "").isalnum():
        logger.error("Archive smoke: invalid run identifier")
        return
    if not config.CLIENT_MATERIALS_CHANNEL:
        logger.error("Archive smoke: group not configured")
        return
    if not config.UPSTASH_REDIS_REST_URL or not config.UPSTASH_REDIS_REST_TOKEN:
        logger.error("Archive smoke: persistent storage required for once-only execution")
        return
    key = f"design-bot:archive-smoke:once:{run_id}"
    try:
        claimed = await asyncio.to_thread(
            content_store._upstash_command, "SET", key, "started", "NX", "EX", 86400 * 30,
        )
        if claimed != "OK":
            logger.info("Archive smoke: run already claimed")
            return
        chat_id = int(config.CLIENT_MATERIALS_CHANNEL)
        topic = await bot.create_forum_topic(chat_id=chat_id, name=f"Проверка архива {run_id[:16]}")
        thread = topic.message_thread_id
        await bot.send_message(
            chat_id=chat_id, message_thread_id=thread,
            text=f"Технический тест архива. Идентификатор: {run_id}. "
                 "Не является клиентской заявкой.",
        )
        await bot.send_document(
            chat_id=chat_id, message_thread_id=thread,
            document=BufferedInputFile(
                f"Тест архива {datetime.now(timezone.utc).isoformat()}".encode("utf-8"),
                filename="archive-smoke.txt",
            ),
        )
        await bot.send_photo(
            chat_id=chat_id, message_thread_id=thread,
            photo=BufferedInputFile(PNG, filename="archive-smoke.png"),
        )
        logger.info("Archive smoke PASS: topic, message, document and image delivered")
    except Exception:
        logger.exception("Archive smoke FAILED: check bot permissions and Telegram API")


