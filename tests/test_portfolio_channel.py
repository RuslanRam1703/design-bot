"""Unit tests for channel import (no Telegram token or Redis required)."""
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from bot.handlers import portfolio_channel


def fake_message(*, username="portfolio_rrr17", message_id=42, photo=True, caption="Тестовый проект"):
    return SimpleNamespace(
        chat=SimpleNamespace(username=username),
        message_id=message_id,
        photo=[object()] if photo else None,
        caption=caption,
        bot=SimpleNamespace(send_message=AsyncMock()),
    )


@pytest.mark.asyncio
async def test_channel_photo_creates_case():
    message = fake_message()
    with patch.object(portfolio_channel.config, "PORTFOLIO_CHANNEL", "@portfolio_rrr17"), \
         patch.object(portfolio_channel.config, "DESIGNER_CHAT_ID", "123"), \
         patch.object(portfolio_channel.telegram_media, "resolve_cover_url", new_callable=AsyncMock, return_value="https://cdn.example/test.jpg") as resolve, \
         patch.object(portfolio_channel.content_store, "upsert_channel_case", new_callable=AsyncMock, return_value=({"id": "tg_portfolio_rrr17_42"}, True)) as upsert:
        await portfolio_channel.import_post(message)
    resolve.assert_awaited_once_with("https://t.me/portfolio_rrr17/42")
    assert upsert.await_args.kwargs["title"] == "Тестовый проект"
    message.bot.send_message.assert_awaited_once()


@pytest.mark.asyncio
async def test_other_channel_is_ignored():
    message = fake_message(username="other_channel")
    with patch.object(portfolio_channel.config, "PORTFOLIO_CHANNEL", "@portfolio_rrr17"), \
         patch.object(portfolio_channel.telegram_media, "resolve_cover_url", new_callable=AsyncMock) as resolve:
        await portfolio_channel.import_post(message)
    resolve.assert_not_awaited()


@pytest.mark.asyncio
async def test_text_post_is_ignored():
    message = fake_message(photo=False)
    with patch.object(portfolio_channel.config, "PORTFOLIO_CHANNEL", "@portfolio_rrr17"), \
         patch.object(portfolio_channel.telegram_media, "resolve_cover_url", new_callable=AsyncMock) as resolve:
        await portfolio_channel.import_post(message)
    resolve.assert_not_awaited()
