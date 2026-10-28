"""Focused regressions for runtime storage and external-input hardening."""
import unittest
from unittest.mock import AsyncMock, patch

from bot import webserver
from bot.handlers import admin, faq
from bot.lead import format_lead_message


class HardeningTests(unittest.IsolatedAsyncioTestCase):
    async def test_pricing_prefers_upstash(self):
        with patch("bot.webserver.content_store.is_redis_backed", return_value=True), \
             patch("bot.webserver.content_store.read_async", new_callable=AsyncMock, return_value={"services": [{"id": "updated"}]}) as read, \
             patch("bot.webserver.load_pricing", side_effect=AssertionError("stale local prices")):
            data = await webserver._pricing_for_calculation()
        self.assertEqual(data["services"][0]["id"], "updated")
        read.assert_awaited_once_with("pricing.json")

    async def test_faq_prefers_upstash(self):
        live = {"faq": [{"id": 1, "needs_review": False}, {"id": 2, "needs_review": True}]}
        with patch("bot.handlers.faq.content_store.is_redis_backed", return_value=True), \
             patch("bot.handlers.faq.content_store.read_async", new_callable=AsyncMock, return_value=live):
            self.assertEqual([v["id"] for v in await faq._client_faq_items_live()], [1])

    def test_reject_nonfinite_numbers(self):
        for value in ("nan", "NaN", "inf", "-inf", "Infinity", "1e400"):
            self.assertIsNone(admin._parse_number(value), value)

    def test_reject_invalid_calculator_quantity(self):
        base = {"service_name": "Лендинг", "calc": {"service_id": "landing", "options": [{"id": "a", "qty": "abc"}]}}
        self.assertFalse(webserver._valid_new_lead_payload(base))
        base["calc"]["options"][0]["qty"] = 2
        self.assertTrue(webserver._valid_new_lead_payload(base))

    def test_options_are_html_escaped(self):
        class Calc:
            valid = True
            service_name = "Лендинг"
            price_from = 100
            price_to = 200
            term_from = 1
            term_to = 2
            selected_options = [{"name": "<unclosed", "qty": 1}]
            urgent = False
            complex_ = False
        rendered = format_lead_message({"service_name": "Лендинг"}, Calc(), lead_id=7, from_user_id=42, username=None)
        self.assertIn("&lt;unclosed", rendered)
        self.assertNotIn("— опции: <unclosed", rendered)

    async def test_notification_failure_then_success_is_persistent(self):
        import tempfile
        from pathlib import Path
        from bot import content_store
        from bot.notifications import notify_designer_lead
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(content_store, "DATA_DIR", Path(tmp)), \
                 patch("bot.notifications.config.DESIGNER_CHAT_ID", "777"):
                lead = await content_store.add_lead({"service_name": "Лендинг"}, {"user_id": 42}, draft_id="retry-test")
                bot = AsyncMock()
                bot.send_message.side_effect = RuntimeError("temporary")
                self.assertFalse(await notify_designer_lead(bot, lead))
                failed = await content_store.get_lead(lead["id"])
                self.assertEqual(failed["designer_notification"]["state"], "failed")
                bot.send_message.side_effect = None
                self.assertTrue(await notify_designer_lead(bot, failed))
                stored = await content_store.get_lead(lead["id"])
                self.assertEqual(stored["designer_notification"]["state"], "sent")

    async def test_changed_draft_creates_new_pending_revision(self):
        import tempfile
        from pathlib import Path
        from bot import content_store
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(content_store, "DATA_DIR", Path(tmp)):
                user = {"user_id": 42}
                await content_store.add_lead({"service_name": "A"}, user, draft_id="dup-test")
                same = await content_store.add_lead({"service_name": "A"}, user, draft_id="dup-test")
                self.assertFalse(same["changed"])
                changed = await content_store.add_lead({"service_name": "B"}, user, draft_id="dup-test")
                self.assertTrue(changed["changed"])
                self.assertEqual(changed["designer_notification"]["revision"], 2)


if __name__ == "__main__":
    unittest.main()
