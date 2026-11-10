"""Archive delivery failure and isolation regression tests."""
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from bot import client_materials_archive as archive


class ArchiveDeliveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_copy_failure_keeps_canonical_mapping_for_retry(self):
        db = {}
        def redis(*args):
            op, key = args[:2]
            if op == "GET":
                return db.get(key)
            if op == "SET":
                db[key] = args[2]
                return "OK"
            raise AssertionError(args)

        bot = SimpleNamespace(
            create_forum_topic=AsyncMock(return_value=SimpleNamespace(message_thread_id=88)),
            copy_message=AsyncMock(side_effect=[RuntimeError("Telegram unavailable"), None]),
        )
        msg = SimpleNamespace(bot=bot, chat=SimpleNamespace(id=123), message_id=10)
        with patch.object(archive.config, "CLIENT_MATERIALS_CHANNEL", "-100456"), patch.object(
            archive.config, "UPSTASH_REDIS_REST_URL", "https://example.invalid"
        ), patch.object(archive.config, "UPSTASH_REDIS_REST_TOKEN", "test"), patch(
            "bot.content_store._upstash_command", side_effect=redis
        ):
            self.assertFalse(await archive.archive_lead_material(msg, 5))
            self.assertTrue(await archive.archive_lead_material(msg, 5))
        bot.create_forum_topic.assert_awaited_once()
        self.assertEqual(bot.copy_message.await_count, 2)

    async def test_missing_group_is_safe(self):
        with patch.object(archive.config, "CLIENT_MATERIALS_CHANNEL", ""):
            self.assertFalse(await archive.archive_lead_material(SimpleNamespace(), 5))


if __name__ == "__main__":
    unittest.main()
