"""Private customer-materials archive; never creates public t.me links."""
import logging

from bot import config

logger = logging.getLogger(__name__)


class ClientArchiveUnavailable(Exception):
    pass


async def archive_client_material(bot, *, file_id: str, kind: str, lead_id: int) -> dict:
    channel = config.CLIENT_MATERIALS_CHANNEL
    if not channel:
        raise ClientArchiveUnavailable("CLIENT_MATERIALS_CHANNEL is not configured")
    caption = f"Материал заявки №{lead_id}"
    try:
        if kind == "photo":
            sent = await bot.send_photo(channel, file_id, caption=caption)
        elif kind == "video":
            sent = await bot.send_video(channel, file_id, caption=caption)
        elif kind == "animation":
            sent = await bot.send_animation(channel, file_id, caption=caption)
        elif kind == "document":
            sent = await bot.send_document(channel, file_id, caption=caption)
        else:
            raise ValueError("Unsupported material type")
    except Exception as exc:
        logger.exception("Private archive write failed for lead %s", lead_id)
        raise ClientArchiveUnavailable("Unable to archive customer material") from exc
    return {"chat_id": str(channel), "message_id": sent.message_id}
