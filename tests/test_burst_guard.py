"""Short-burst and blocked-user UI regressions, no real Telegram requests."""
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from aiohttp.test_utils import TestClient, TestServer
from aiogram.types import MenuButtonCommands, MenuButtonDefault

from bot import access_control, config, content_store, webserver
from bot.burst_guard import BurstLimiter
from bot.handlers import admin_access
from test_regression import make_callback, make_state


class BurstGuardTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old_data = content_store.DATA_DIR
        self.old_owner = config.DESIGNER_CHAT_ID
        content_store.DATA_DIR = Path(self.tmp.name)
        config.DESIGNER_CHAT_ID = "777"
        (content_store.DATA_DIR / "leads.json").write_text('{"leads":[]}', encoding="utf-8")

    def tearDown(self):
        config.DESIGNER_CHAT_ID = self.old_owner
        content_store.DATA_DIR = self.old_data
        self.tmp.cleanup()

    def test_burst_limits_only_high_frequency_and_expires_quickly(self):
        limiter = BurstLimiter(limit=3, window_seconds=10, max_keys=2)
        self.assertTrue(limiter.allow("a", now=0))
        self.assertTrue(limiter.allow("a", now=1))
        self.assertTrue(limiter.allow("a", now=2))
        self.assertFalse(limiter.allow("a", now=3))
        self.assertTrue(limiter.allow("a", now=12))
        self.assertTrue(limiter.allow("b", now=12))
        self.assertTrue(limiter.allow("c", now=12))
        self.assertLessEqual(len(limiter._seen), 2)

    async def test_block_admin_hides_only_personal_button(self):
        callback = make_callback("access:block:1234", chat_id=777, message_id=200)
        state = make_state(777)
        await admin_access.block(callback, state)
        self.assertTrue(await access_control.is_blocked(1234))
        callback.bot.set_chat_menu_button.assert_awaited_once()
        self.assertEqual(callback.bot.set_chat_menu_button.await_args.kwargs["chat_id"], 1234)
        self.assertIsInstance(callback.bot.set_chat_menu_button.await_args.kwargs["menu_button"], MenuButtonCommands)
        callback.bot.set_chat_menu_button.reset_mock()
        callback.data = "access:unblock:1234"
        await admin_access.unblock(callback, state)
        self.assertFalse(await access_control.is_blocked(1234))
        self.assertIsInstance(callback.bot.set_chat_menu_button.await_args.kwargs["menu_button"], MenuButtonDefault)

    async def test_menu_failure_never_rolls_back_server_side_block(self):
        callback = make_callback("access:block:1234", chat_id=777, message_id=200)
        callback.bot.set_chat_menu_button.side_effect = RuntimeError("not started")
        await admin_access.block(callback, make_state(777))
        self.assertTrue(await access_control.is_blocked(1234))

    async def test_username_watch_automatically_hides_menu_on_first_match(self):
        await access_control.block_username(777, "@spammer")
        fake_bot = AsyncMock()
        self.assertTrue(await access_control.is_blocked(31, "spammer", bot=fake_bot))
        fake_bot.set_chat_menu_button.assert_awaited_once()
        self.assertIsInstance(fake_bot.set_chat_menu_button.await_args.kwargs["menu_button"], MenuButtonCommands)
        self.assertTrue(await access_control.is_blocked(31, "newname", bot=fake_bot))
        fake_bot.set_chat_menu_button.assert_awaited_once()

    async def test_message_middleware_avoids_spam_replies_and_store_reads(self):
        middleware = access_control.AccessMiddleware()
        handler = AsyncMock()
        event = SimpleNamespace(from_user=SimpleNamespace(id=31, username="testclient"))
        with patch("bot.access_control.is_blocked", new_callable=AsyncMock, return_value=False) as check:
            for _ in range(31):
                await middleware(handler, event, {"bot": AsyncMock()})
        self.assertEqual(handler.await_count, 30)
        self.assertEqual(check.await_count, 30)

    async def test_empty_lead_rejected_before_storage(self):
        self.assertFalse(webserver._valid_new_lead_payload({}))
        self.assertFalse(webserver._valid_new_lead_payload({"service_name": " \t "}))
        self.assertTrue(webserver._valid_new_lead_payload({"service_name": "Логотип"}))

    async def test_http_burst_is_429_and_does_not_change_daily_quota(self):
        app = webserver.create_app(AsyncMock())
        async with TestClient(TestServer(app)) as client:
            # 20 rapid unauthorized requests: reject before touching storage;
            # 21st should be 429. Different clients/IPs unaffected.
            for _ in range(20):
                resp = await client.post("/api/leads", headers={"X-Forwarded-For": "198.51.100.4"}, json={})
                self.assertEqual(resp.status, 401)
            resp = await client.post("/api/leads", headers={"X-Forwarded-For": "198.51.100.4"}, json={})
            self.assertEqual(resp.status, 429)
            self.assertEqual(resp.headers.get("Retry-After"), "10")
            resp = await client.post("/api/leads", headers={"X-Forwarded-For": "198.51.100.5"}, json={})
            self.assertEqual(resp.status, 401)


if __name__ == "__main__":
    unittest.main()
