"""Client history integration: record each routed message exactly once."""
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from bot.handlers import start


class ClientHistoryRoutingTests(unittest.IsolatedAsyncioTestCase):
    def make_message(self, text="Hello"):
        return SimpleNamespace(
            bot=SimpleNamespace(send_message=AsyncMock()),
            chat=SimpleNamespace(id=17, type="private"),
            from_user=SimpleNamespace(id=17, username=None, first_name="Client", last_name=None),
            message_id=42, text=text, caption="Document", answer=AsyncMock(), forward=AsyncMock(),
        )

    async def test_single_lead_text_is_recorded_once(self):
        message = self.make_message()
        with patch.object(start.config, "DESIGNER_CHAT_ID", "900"), patch.object(
            start.content_store, "list_leads_by_user", new_callable=AsyncMock,
            return_value=[{"id": 7, "status": "NEW"}]
        ), patch.object(start.client_materials_archive, "archive_lead_material", new_callable=AsyncMock), patch.object(
            start.content_store, "add_client_message", new_callable=AsyncMock
        ) as record:
            await start.relay_client_text_to_designer(message, SimpleNamespace())
        record.assert_awaited_once_with(17, 7, 42, "text", "Hello")

    async def test_single_lead_media_is_recorded_once(self):
        message = self.make_message()
        with patch.object(start.config, "DESIGNER_CHAT_ID", "900"), patch.object(
            start.content_store, "list_leads_by_user", new_callable=AsyncMock,
            return_value=[{"id": 7, "status": "NEW"}]
        ), patch.object(start.client_materials_archive, "archive_lead_material", new_callable=AsyncMock), patch.object(
            start.content_store, "add_client_message", new_callable=AsyncMock
        ) as record:
            await start.relay_client_media_to_designer(message)
        record.assert_awaited_once_with(17, 7, 42, "media", "Document")


if __name__ == "__main__":
    unittest.main()
