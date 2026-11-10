"""Moderated project import from a dedicated public Telegram channel.

Only image posts from the configured channel create or update portfolio cases.
No client material is processed. Duplicate deliveries are idempotent.
"""
import logging

from aiogram import Router
from aiogram.types import Message

from bot import config, content_store, telegram_media

router = Router(name="portfolio_channel")
logger = logging.getLogger(__name__)


def is_portfolio_channel(chat_username: str | None) -> bool:
    configured = config.PORTFOLIO_CHANNEL.lstrip("@").casefold()
    return bool(configured and chat_username and chat_username.casefold() == configured)


async def import_post(message: Message) -> None:
    if not is_portfolio_channel(message.chat.username):
        return
    if not message.photo:
        logger.info("Skipped non-photo portfolio post %s", message.message_id)
        return
    post_url = f"https://t.me/{message.chat.username}/{message.message_id}"
    try:
        group_id = getattr(message, "media_group_id", None)
        images = (await telegram_media.resolve_album_urls(post_url) if group_id else
                  [await telegram_media.resolve_cover_url(post_url)])
        cover = images[0]
        title = (message.caption or "").strip().split("\n", 1)[0][:120] or f"Публикация {message.message_id}"
        case, created = await content_store.upsert_channel_case(
            channel_username=message.chat.username,
            message_id=message.message_id,
            title=title,
            cover=cover,
            media_group_id=group_id, images=images, caption=message.caption,
        )
        logger.info("Portfolio case %s: %s", "created" if created else "updated", case["id"])
        if created and config.DESIGNER_CHAT_ID:
            await message.bot.send_message(
                chat_id=int(config.DESIGNER_CHAT_ID),
                text=f"Создан черновик проекта «{title}».\nИзображений: {len(images)}.\n"
                     "Проверьте описание и категорию: Админ → Контент → Кейсы → Статусы и публикация.\n"
                     f"Источник: {post_url}",
                parse_mode=None,
            )
    except Exception:
        logger.exception("Portfolio import failed for %s", post_url)
        if config.DESIGNER_CHAT_ID:
            try:
                await message.bot.send_message(
                    chat_id=int(config.DESIGNER_CHAT_ID),
                    text="Не удалось импортировать публикацию в портфолио. "
                         "Проверьте доступность изображений и повторите, отредактировав пост.\n"
                         f"Источник: {post_url}", parse_mode=None,
                )
            except Exception:
                logger.exception("Could not notify owner about portfolio import failure")


@router.channel_post()
async def on_portfolio_post(message: Message) -> None:
    await import_post(message)


@router.edited_channel_post()
async def on_portfolio_post_edit(message: Message) -> None:
    await import_post(message)
