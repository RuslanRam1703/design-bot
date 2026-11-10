"""Production FSM storage regression tests: isolation, failures and TTL."""
import unittest
from unittest.mock import patch
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage
from bot.fsm_storage import UpstashFSMStorage, create_fsm_storage


class FSMFailureTests(unittest.IsolatedAsyncioTestCase):
    async def test_separate_users_do_not_share_state(self):
        values = {}
        def command(*args):
            op, key = args[:2]
            if op == "GET":
                return values.get(key)
            if op == "SET":
                values[key] = args[2]
                return "OK"
            if op == "DEL":
                return int(values.pop(key, None) is not None)
            raise AssertionError(args)
        a = StorageKey(bot_id=1, chat_id=10, user_id=10)
        b = StorageKey(bot_id=1, chat_id=20, user_id=20)
        with patch("bot.content_store._upstash_command", side_effect=command):
            storage = UpstashFSMStorage()
            await storage.set_state(a, "AdminStates:add_case_title")
            self.assertIsNone(await storage.get_state(b))
            await storage.set_data(b, {"draft": "another"})
            self.assertEqual(await storage.get_state(a), "AdminStates:add_case_title")
            self.assertEqual(await storage.get_data(a), {})

    async def test_storage_error_is_not_silently_swallowed(self):
        key = StorageKey(bot_id=1, chat_id=10, user_id=10)
        with patch("bot.content_store._upstash_command", side_effect=ConnectionError("offline")):
            with self.assertRaises(ConnectionError):
                await UpstashFSMStorage().get_state(key)

    async def test_ttl_is_applied(self):
        calls = []
        def command(*args):
            calls.append(args)
            if args[0] == "GET":
                return None
            return "OK"
        key = StorageKey(bot_id=1, chat_id=10, user_id=10)
        with patch("bot.content_store._upstash_command", side_effect=command):
            await UpstashFSMStorage(ttl_seconds=3600).set_state(key, "test")
        self.assertTrue(any(c[0] == "SET" and c[-2:] == ("EX", 3600) for c in calls))

    async def test_local_fallback_only_without_credentials(self):
        with patch("bot.fsm_storage.config.UPSTASH_REDIS_REST_URL", ""), patch(
            "bot.fsm_storage.config.UPSTASH_REDIS_REST_TOKEN", ""
        ):
            self.assertIsInstance(create_fsm_storage(), MemoryStorage)
        with patch("bot.fsm_storage.config.UPSTASH_REDIS_REST_URL", "https://example.invalid"), patch(
            "bot.fsm_storage.config.UPSTASH_REDIS_REST_TOKEN", "test-token"
        ):
            self.assertIsInstance(create_fsm_storage(), UpstashFSMStorage)


if __name__ == "__main__":
    unittest.main()
