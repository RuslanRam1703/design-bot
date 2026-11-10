"""FSM persistence tests without connecting to production Upstash."""
import asyncio
import json
import unittest
from unittest.mock import patch
from aiogram.fsm.storage.base import StorageKey
from bot.fsm_storage import UpstashFSMStorage


class PersistentFSMTests(unittest.IsolatedAsyncioTestCase):
    async def test_state_data_and_restart(self):
        db = {}
        def redis(*args):
            op, key = args[:2]
            if op == "GET":
                return db.get(key)
            if op == "SET":
                db[key] = args[2]
                return "OK"
            if op == "DEL":
                return int(db.pop(key, None) is not None)
            raise AssertionError(args)

        key = StorageKey(bot_id=123, chat_id=456, user_id=789)
        with patch("bot.content_store._upstash_command", side_effect=redis):
            first = UpstashFSMStorage()
            await first.set_state(key, "AdminStates:add_case_title")
            await first.set_data(key, {"_nav_msg_id": 42, "draft": "example"})
            second = UpstashFSMStorage()
            self.assertEqual(await second.get_state(key), "AdminStates:add_case_title")
            self.assertEqual((await second.get_data(key))["_nav_msg_id"], 42)
            await second.set_state(key, None)
            self.assertEqual((await second.get_data(key))["draft"], "example")
            await second.set_data(key, {})
            self.assertEqual(db, {})


if __name__ == "__main__":
    unittest.main()
