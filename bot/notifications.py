"""Persisted at-least-once designer notification delivery.

Telegram timeouts are ambiguous, so an occasional duplicate is possible;
losing a lead notification silently is worse.
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
    calc = lead.get("calc_summary")
    try:
        text = format_lead_message(
            lead["payload"], CalcResult(**calc) if calc else None,
            lead_id=lead["id"], from_user_id=lead["telegram"]["user_id"],
            username=lead["telegram"].get("username"),
        )
        if notice["revision"] > 1:
            text = text.replace("Новая заявка", "Обновление заявки", 1)
        await bot.send_message(chat_id=config.DESIGNER_CHAT_ID, text=text, parse_mode="HTML")
    except Exception:
        logger.exception("Designer notification failed for lead #%s", lead["id"])
        await content_store.update_designer_notification(lead["id"], notice["revision"], "failed")
        return False
    await content_store.update_designer_notification(lead["id"], notice["revision"], "sent")
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
    try:
        text = format_lead_supplement_message(lead["id"], supplement["fields"])
        await bot.send_message(chat_id=config.DESIGNER_CHAT_ID, text=text, parse_mode="HTML")
    except Exception:
        logger.exception("Designer supplement notification failed for lead #%s", lead["id"])
        await content_store.update_designer_notification(lead["id"], notice["revision"], "failed", supplement_id)
        return False
    await content_store.update_designer_notification(lead["id"], notice["revision"], "sent", supplement_id)
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
            logger.exception("Designer delivery retry worker failed")
