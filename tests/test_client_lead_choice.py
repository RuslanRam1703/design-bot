"""Client chooses a lead explicitly when several orders are active."""
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from bot.handlers import start


class LeadChoiceTests(unittest.IsolatedAsyncioTestCase):
    def test_choice_keyboard(self):
        kb = start._lead_choice_keyboard([{"id": 7}, {"id": 8}], 42)
        self.assertEqual([r[0].callback_data for r in kb.inline_keyboard],
                         ["clientleadroute:42:7", "clientleadroute:42:8"])

    async def test_unauthorised_lead_is_not_archived(self):
        cb = SimpleNamespace(
            data="clientleadroute:42:99",
            from_user=SimpleNamespace(id=17),
            message=SimpleNamespace(chat=SimpleNamespace(id=17, type="private")),
            answer=AsyncMock(),
        )
        with patch.object(start.content_store, "list_leads_by_user", new_callable=AsyncMock,
                          return_value=[{"id": 7, "status": "NEW"}]), patch.object(
            start.client_materials_archive, "archive_lead_material", new_callable=AsyncMock
        ) as archive:
            await start.route_client_message_to_lead(cb)
        archive.assert_not_awaited()
        cb.answer.assert_awaited_once()

    async def test_valid_lead_is_archived(self):
        bot = SimpleNamespace()
        msg = SimpleNamespace(chat=SimpleNamespace(id=17, type="private"), edit_text=AsyncMock())
        cb = SimpleNamespace(
            data="clientleadroute:42:7", from_user=SimpleNamespace(id=17),
            message=msg, bot=bot, answer=AsyncMock(),
        )
        with patch.object(start.content_store, "list_leads_by_user", new_callable=AsyncMock,
                          return_value=[{"id": 7, "status": "NEW"}]), patch.object(
            start.client_materials_archive, "archive_lead_material", new_callable=AsyncMock,
            return_value=True
        ) as archive, patch.object(start.content_store, "bind_pending_client_message", AsyncMock(
            return_value={"kind": "text", "text": "Hello", "already_bound": False}
        )):
            await start.route_client_message_to_lead(cb)
        self.assertEqual(archive.await_args.args[1], 7)
        self.assertEqual(archive.await_args.args[0].message_id, 42)
        msg.edit_text.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
