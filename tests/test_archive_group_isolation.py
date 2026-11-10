"""Archive topic mapping must not cross private groups."""
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from bot import client_materials_archive as archive


class ArchiveGroupIsolationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        prepare = patch.object(archive.content_store, 'prepare_archive_copy', AsyncMock(
            return_value={'key': 'synthetic', 'claim_token': 'test', 'claimed': True, 'state': 'sending'}))
        finish = patch.object(archive.content_store, 'finish_archive_copy', AsyncMock())
        prepare.start()
        finish.start()
        self.addCleanup(prepare.stop)
        self.addCleanup(finish.stop)

    async def test_same_lead_different_groups_create_distinct_topics(self):
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
            create_forum_topic=AsyncMock(side_effect=[
                SimpleNamespace(message_thread_id=11),
                SimpleNamespace(message_thread_id=22),
            ]),
            copy_message=AsyncMock(),
        )
        msg = SimpleNamespace(bot=bot, chat=SimpleNamespace(id=123), message_id=1)
        with patch.object(archive.config, "UPSTASH_REDIS_REST_URL", "https://example.invalid"), patch.object(
            archive.config, "UPSTASH_REDIS_REST_TOKEN", "test"
        ), patch("bot.content_store._upstash_command", side_effect=redis):
            with patch.object(archive.config, "CLIENT_MATERIALS_CHANNEL", "-100111"):
                self.assertTrue(await archive.archive_lead_material(msg, 7))
            with patch.object(archive.config, "CLIENT_MATERIALS_CHANNEL", "-100222"):
                self.assertTrue(await archive.archive_lead_material(msg, 7))
        self.assertEqual(bot.create_forum_topic.await_count, 2)
        self.assertEqual(
            [c.kwargs["message_thread_id"] for c in bot.copy_message.await_args_list],
            [11, 22],
        )
        self.assertEqual(len(db), 2)


if __name__ == "__main__":
    unittest.main()
