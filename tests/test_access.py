"""Owner-only access policy and signed Mini App checks, without live Telegram."""
import io
import json
import tempfile
import unittest
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from aiohttp.test_utils import TestClient, TestServer

from bot import access_control as ac, config, content_store, webserver
from bot.handlers import admin_access
from test_regression import make_callback, make_state


class BlocklistTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.old_data = content_store.DATA_DIR
        self.old_owner = config.DESIGNER_CHAT_ID
        content_store.DATA_DIR = Path(self.temp.name)
        config.DESIGNER_CHAT_ID = "777"
        (content_store.DATA_DIR / "leads.json").write_text('{"leads":[]}', encoding="utf-8")

    def tearDown(self):
        content_store.DATA_DIR = self.old_data
        config.DESIGNER_CHAT_ID = self.old_owner
        self.temp.cleanup()

    async def test_block_unblock_by_id_does_not_touch_leads(self):
        self.assertTrue(await ac.block_id(777, 12345, "Spam_User"))
        self.assertTrue(await ac.is_blocked(12345, "renamed"))
        self.assertFalse(await ac.block_id(777, 12345))
        self.assertFalse(await ac.is_blocked(67890, None))
        self.assertEqual(await content_store.list_leads("ALL"), [])
        self.assertTrue(await ac.unblock_id(777, 12345))
        self.assertFalse(await ac.is_blocked(12345, "renamed"))
        self.assertFalse(await ac.unblock_id(777, 12345))

    async def test_cannot_block_owner_and_non_owner_cannot_manage(self):
        with self.assertRaises(ValueError):
            await ac.block_id(777, 777)
        with self.assertRaises(content_store.NotDesignerError):
            await ac.block_id(12345, 67890)
        with self.assertRaises(content_store.NotDesignerError):
            await ac.block_username(12345, "@attacker")
        self.assertFalse(await ac.is_blocked(777, "owner"))

    async def test_username_watch_resolves_to_id_once(self):
        self.assertTrue(await ac.block_username(777, "@TroubleUser"))
        self.assertFalse(await ac.is_blocked(111, "otheruser"))
        self.assertTrue(await ac.is_blocked(222, "troubleuser"))
        self.assertTrue(await ac.is_blocked(222, "newhandle"))
        self.assertFalse(await ac.is_blocked(333, "troubleuser"))
        stored = await ac.view()
        self.assertNotIn("troubleuser", stored["watch_usernames"])
        self.assertIn("222", stored["blocked_ids"])

    async def test_historical_username_lookup_and_no_arbitrary_lookup(self):
        await content_store.add_lead({"service_name": "Лендинг"}, {
            "user_id": 234, "username": "OldClient", "first_name": "Тест",
        })
        hits = await ac.history_matches("@oldclient")
        self.assertEqual(hits[0]["id"], 234)
        self.assertEqual(await ac.history_matches("@not_seen"), [])
        self.assertIsNone(ac.normalize_username("@x!"))
        self.assertEqual(ac.normalize_username(" @HELLO_user "), "hello_user")

    async def test_middleware_suppresses_message_and_answers_callback(self):
        await ac.block_id(777, 99)
        executed = AsyncMock()
        event = SimpleNamespace(from_user=SimpleNamespace(id=99, username="x"))
        self.assertIsNone(await ac.AccessMiddleware()(executed, event, {}))
        executed.assert_not_awaited()
        cb = SimpleNamespace(from_user=event.from_user, data="intake:quick", answer=AsyncMock())
        await ac.AccessMiddleware()(executed, cb, {})
        cb.answer.assert_awaited_once()
        allowed = SimpleNamespace(from_user=SimpleNamespace(id=98, username="normaluser"))
        await ac.AccessMiddleware()(executed, allowed, {})
        executed.assert_awaited_once()

    async def test_public_content_policy_and_blocked_user(self):
        (content_store.DATA_DIR / "about.json").write_text('{"name":"Hidden"}', encoding="utf-8")
        await ac.block_id(777, 12345)
        app = webserver.create_app(AsyncMock())
        async with TestClient(TestServer(app)) as client:
            with patch("bot.webserver.validate_init_data", return_value={"id": 12345, "username": "blocked"}):
                r = await client.get("/data/about.json", headers={"X-Telegram-Init-Data": "signed"})
                self.assertEqual(r.status, 403)
                r = await client.get("/api/my-leads", headers={"X-Telegram-Init-Data": "signed"})
                self.assertEqual(r.status, 403)
                r = await client.post("/api/leads", headers={"X-Telegram-Init-Data": "signed"}, json={
                    "mode": "new", "service_name": "Лого",
                })
                self.assertEqual(r.status, 403)
            with patch.object(config, "PRIVATE_MINIAPP_CONTENT", True):
                r = await client.get("/data/about.json")
                self.assertEqual(r.status, 401)
                with patch("bot.webserver.validate_init_data", return_value={"id": 99, "username": "ok"}):
                    r = await client.get("/data/about.json", headers={"X-Telegram-Init-Data": "signed"})
                    self.assertEqual(r.status, 200)
            with patch.object(config, "PRIVATE_MINIAPP_CONTENT", False):
                r = await client.get("/data/about.json")
                self.assertEqual(r.status, 200)  # Browser preview is knowingly public.

    async def test_legacy_lead_mutations_preserve_blocklist(self):
        await ac.block_id(777, 234)
        lead = await content_store.add_lead({"service_name": "Логотип"}, {
            "user_id": 345, "first_name": "Клиент",
        })
        self.assertTrue(await content_store.update_lead_status(777, lead["id"], "VIEWED"))
        self.assertTrue(await ac.is_blocked(234, None))

    async def test_backup_export_contains_blocklist_and_legacy_restore_is_rejected(self):
        await ac.block_id(777, 234)
        current = await content_store._read("leads.json")
        self.assertIn("access_control", current)
        legacy = io.BytesIO()
        with zipfile.ZipFile(legacy, "w") as zf:
            zf.writestr("data/leads.json", json.dumps({"leads": []}))
        with self.assertRaises(content_store.BackupValidationError):
            await content_store.import_backup_bytes(777, legacy.getvalue())
        self.assertTrue(await ac.is_blocked(234, None))

    def test_admin_entry_points_exist_and_ids_are_not_usernames(self):
        items = [b.callback_data for row in admin_access.home_keyboard().inline_keyboard for b in row]
        self.assertIn("access:input", items)
        self.assertIn("access:list", items)
        self.assertFalse(ac.normalize_username("123"))


if __name__ == "__main__":
    unittest.main()
