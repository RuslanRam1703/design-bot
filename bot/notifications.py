"""Persisted at-least-once Telegram notifications with exclusive delivery claims.

Telegram does not supply sendMessage idempotency keys. A timeout may occur
after acceptance, so exactly-once is impossible; per-message claims prevent
parallel sends, and backoff limits retries after ambiguous failures.
"""
import asyncio
import logging

from bot import config, content_store
from bot.calculator import CalcResult
from bot.lead import format_lead_message, format_lead_supplement_message

logger = logging.getLogger(__name__)


async def notify_designer_lead(bot, lead: dict) -> bool:
    if not config.DESIGNER_CHAT_ID:
        logger.warning("Designer chat missing; lead #%s stays queued", lead["id"])
        return False
    notice = lead.get("designer_notification")
    if not notice or notice.get("state") == "sent":
        return True
    claimed = await content_store.claim_designer_notification(lead["id"], notice["revision"])
    if claimed is None:
        return False
    record = claimed["designer_notification"]
    token = record["claim_token"]
    calc = claimed.get("calc_summary")
    try:
        message = format_lead_message(
            claimed["payload"], CalcResult(**calc) if calc else None,
            lead_id=claimed["id"], from_user_id=claimed["telegram"]["user_id"],
            username=claimed["telegram"].get("username"),
        )
        if record["revision"] > 1:
            message = message.replace("Новая заявка", "Обновление заявки", 1)
        await bot.send_message(chat_id=config.DESIGNER_CHAT_ID, text=message, parse_mode="HTML")
    except Exception:
        logger.exception("Designer notification failed for lead #%s", claimed["id"])
        await content_store.finish_designer_notification(claimed["id"], record["revision"], token, "failed")
        return False
    await content_store.finish_designer_notification(claimed["id"], record["revision"], token, "sent")
    return True


async def notify_designer_supplement(bot, lead: dict, supplement_id: int) -> bool:
    if not config.DESIGNER_CHAT_ID:
        logger.warning("Designer chat missing; supplement for #%s queued", lead["id"])
        return False
    supplement = next((s for s in lead.get("supplements", []) if s["id"] == supplement_id), None)
    if supplement is None:
        return False
    notice = supplement.get("designer_notification")
    if not notice or notice.get("state") == "sent":
        return True
    claimed = await content_store.claim_designer_notification(lead["id"], notice["revision"], supplement_id)
    if claimed is None:
        return False
    current = next(s for s in claimed.get("supplements", []) if s["id"] == supplement_id)
    token = current["designer_notification"]["claim_token"]
    try:
        message = format_lead_supplement_message(claimed["id"], current["fields"])
        await bot.send_message(chat_id=config.DESIGNER_CHAT_ID, text=message, parse_mode="HTML")
    except Exception:
        logger.exception("Designer supplement notification failed for lead #%s", claimed["id"])
        await content_store.finish_designer_notification(claimed["id"], notice["revision"], token, "failed", supplement_id)
        return False
    await content_store.finish_designer_notification(claimed["id"], notice["revision"], token, "sent", supplement_id)
    return True


async def retry_designer_notifications(bot) -> None:
    while True:
        await asyncio.sleep(60)
        if not config.DESIGNER_CHAT_ID:
            continue
        try:
            for lead, supplement_id in await content_store.pending_designer_notifications():
                if supplement_id is None:
                    await notify_designer_lead(bot, lead)
                else:
                    await notify_designer_supplement(bot, lead, supplement_id)
        except Exception:
            logger.exception("Designer notification retry failed")
