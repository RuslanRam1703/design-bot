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
        rendered = format_lead_message({"service_name": "Лендинг"}, Calc(), lead_id=7, from_user_id=42)
        self.assertIn("&lt;unclosed", rendered)
        self.assertNotIn("— опции: <unclosed", rendered)


if __name__ == "__main__":
    unittest.main()
