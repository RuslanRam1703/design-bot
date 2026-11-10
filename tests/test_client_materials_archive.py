"""Private archive integration with fake Telegram Bot and Redis."""
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from bot import client_materials_archive as archive


class ArchiveTopicTests(unittest.IsolatedAsyncioTestCase):
    async def test_one_topic_for_multiple_files_of_same_lead(self):
        db = {}
        def redis(*args):
            op, key = args[:2]
            if op == "GET":
                return db.get(key)
            if op == "SET":
                if key in db:
                    return None
                db[key] = args[2]
                return "OK"
            raise AssertionError(args)
        bot = SimpleNamespace(
            create_forum_topic=AsyncMock(return_value=SimpleNamespace(message_thread_id=77)),
            copy_message=AsyncMock(),
        )
        msg = SimpleNamespace(bot=bot, chat=SimpleNamespace(id=100), message_id=1)
        with patch.object(archive.config, "CLIENT_MATERIALS_CHANNEL", "-100123"), patch.object(
            archive.config, "UPSTASH_REDIS_REST_URL", "https://example.invalid"
        ), patch.object(archive.config, "UPSTASH_REDIS_REST_TOKEN", "test"), patch(
            "bot.content_store._upstash_command", side_effect=redis
        ):
            self.assertTrue(await archive.archive_lead_material(msg, 42))
            msg.message_id = 2
            self.assertTrue(await archive.archive_lead_material(msg, 42))
        bot.create_forum_topic.assert_awaited_once()
        self.assertEqual(bot.copy_message.await_count, 2)
        self.assertEqual(bot.copy_message.await_args.kwargs["message_thread_id"], 77)

    async def test_no_archive_configuration_does_not_break_client(self):
        msg = SimpleNamespace()
        with patch.object(archive.config, "CLIENT_MATERIALS_CHANNEL", ""):
            self.assertFalse(await archive.archive_lead_material(msg, 42))


if __name__ == "__main__":
    unittest.main()
