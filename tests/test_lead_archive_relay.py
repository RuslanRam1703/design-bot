"""Regression tests for lead-specific incoming client messages."""
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from bot.handlers import start


def lead(lead_id):
    return {"id": lead_id, "status": "NEW"}


class LeadArchiveRelayTests(unittest.IsolatedAsyncioTestCase):
    def make_message(self, text="Client reply"):
        bot = SimpleNamespace(send_message=AsyncMock())
        return SimpleNamespace(
            bot=bot, text=text, chat=SimpleNamespace(id=41),
            from_user=SimpleNamespace(id=41, username=None, first_name="Client", last_name=None),
            message_id=12, answer=AsyncMock(), forward=AsyncMock(),
        )

    async def test_single_lead_text_is_mirrored(self):
        msg = self.make_message()
        with patch.object(start.config, "DESIGNER_CHAT_ID", "900"), patch.object(
            start.content_store, "list_leads_by_user", new_callable=AsyncMock, return_value=[lead(7)]
        ), patch.object(start.client_materials_archive, "archive_lead_material", new_callable=AsyncMock, return_value=True) as archive:
            await start.relay_client_text_to_designer(msg, SimpleNamespace())
        archive.assert_awaited_once_with(msg, 7)
        msg.bot.send_message.assert_awaited_once()
        msg.answer.assert_awaited_once()

    async def test_multiple_leads_not_guessed(self):
        msg = self.make_message()
        with patch.object(start.config, "DESIGNER_CHAT_ID", "900"), patch.object(
            start.content_store, "list_leads_by_user", new_callable=AsyncMock, return_value=[lead(7), lead(8)]
        ), patch.object(start.client_materials_archive, "archive_lead_material", new_callable=AsyncMock) as archive:
            await start.relay_client_text_to_designer(msg, SimpleNamespace())
        archive.assert_not_awaited()
        msg.bot.send_message.assert_awaited_once()

    async def test_failed_designer_delivery_does_not_archive(self):
        msg = self.make_message()
        msg.bot.send_message.side_effect = RuntimeError("telegram unavailable")
        with patch.object(start.config, "DESIGNER_CHAT_ID", "900"), patch.object(
            start.content_store, "list_leads_by_user", new_callable=AsyncMock, return_value=[lead(7)]
        ), patch.object(start.client_materials_archive, "archive_lead_material", new_callable=AsyncMock) as archive:
            await start.relay_client_text_to_designer(msg, SimpleNamespace())
        archive.assert_not_awaited()
        self.assertIn("Не получилось", msg.answer.await_args.args[0])

    async def test_single_lead_media_is_mirrored(self):
        msg = self.make_message()
        with patch.object(start.config, "DESIGNER_CHAT_ID", "900"), patch.object(
            start.content_store, "list_leads_by_user", new_callable=AsyncMock, return_value=[lead(7)]
        ), patch.object(start.client_materials_archive, "archive_lead_material", new_callable=AsyncMock) as archive:
            await start.relay_client_media_to_designer(msg)
        archive.assert_awaited_once_with(msg, 7)
        msg.forward.assert_awaited_once()
        msg.answer.assert_awaited_once()

    async def test_multiple_leads_media_not_mirrored(self):
        msg = self.make_message()
        with patch.object(start.config, "DESIGNER_CHAT_ID", "900"), patch.object(
            start.content_store, "list_leads_by_user", new_callable=AsyncMock, return_value=[lead(7), lead(8)]
        ), patch.object(start.client_materials_archive, "archive_lead_material", new_callable=AsyncMock) as archive:
            await start.relay_client_media_to_designer(msg)
        archive.assert_not_awaited()
        msg.forward.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
