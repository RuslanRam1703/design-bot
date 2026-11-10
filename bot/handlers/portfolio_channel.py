"""Read-only intake for posts from the dedicated public portfolio channel.

A channel post is NOT automatically a portfolio case: publication and curation
are separate actions. Existing portfolio data and client attachments are untouched.
"""
import logging

from aiogram import Router
from aiogram.types import Message

from bot import config

router = Router(name="portfolio_channel")
logger = logging.getLogger(__name__)


def is_portfolio_channel(chat_username: str | None) -> bool:
    configured = config.PORTFOLIO_CHANNEL.lstrip("@").casefold()
    return bool(configured and chat_username and chat_username.casefold() == configured)


@router.channel_post()
async def on_portfolio_post(message: Message) -> None:
    if not is_portfolio_channel(message.chat.username):
        return
    post_url = f"https://t.me/{message.chat.username}/{message.message_id}"
    logger.info("Portfolio channel post received: channel=%s message_id=%s", message.chat.username, message.message_id)
    if not config.DESIGNER_CHAT_ID:
        return
    # Do not store files, expose client information, or create incomplete cases.
    try:
        await message.bot.send_message(
            chat_id=int(config.DESIGNER_CHAT_ID),
            text=f"Новая публикация в канале портфолио: {post_url}\nДобавить её в кейс можно через /admin.",
        )
    except Exception:
        logger.exception("Could not notify designer about portfolio post %s", message.message_id)
