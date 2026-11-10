"""Private archive topic reply access and delivery tests."""
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from bot.handlers import archive_setup


class ArchiveTopicReplyTests(unittest.IsolatedAsyncioTestCase):
    def make_message(self, user_id=17, chat_id=-100123, thread_id=42, text="/reply Hello"):
        return SimpleNamespace(
            message_id=501,
            from_user=SimpleNamespace(id=user_id),
            chat=SimpleNamespace(id=chat_id),
            message_thread_id=thread_id, is_topic_message=True, text=text,
            bot=SimpleNamespace(send_message=AsyncMock()), answer=AsyncMock(),
        )

    async def test_non_owner_cannot_reply(self):
        message = self.make_message(user_id=18)
        with patch.object(archive_setup.config, "UPSTASH_REDIS_REST_URL", "https://redis.example"), patch.object(archive_setup.config, "UPSTASH_REDIS_REST_TOKEN", "test"), patch.object(archive_setup.config, "DESIGNER_CHAT_ID", "17"), patch.object(
            archive_setup.config, "CLIENT_MATERIALS_CHANNEL", "-100123"
        ):
            await archive_setup.reply_from_archive_topic(message)
        message.bot.send_message.assert_not_awaited()

    async def test_wrong_group_cannot_reply(self):
        message = self.make_message(chat_id=-100999)
        with patch.object(archive_setup.config, "DESIGNER_CHAT_ID", "17"), patch.object(
            archive_setup.config, "CLIENT_MATERIALS_CHANNEL", "-100123"
        ):
            await archive_setup.reply_from_archive_topic(message)
        message.bot.send_message.assert_not_awaited()

    async def test_valid_topic_delivers_and_records(self):
        message = self.make_message()
        with patch.object(archive_setup.config, "UPSTASH_REDIS_REST_URL", "https://redis.example"), patch.object(archive_setup.config, "UPSTASH_REDIS_REST_TOKEN", "test"), patch.object(archive_setup.config, "DESIGNER_CHAT_ID", "17"), patch.object(
            archive_setup.config, "CLIENT_MATERIALS_CHANNEL", "-100123"
        ), patch("bot.content_store.list_leads", new_callable=AsyncMock, return_value=[
            {"id": 7, "telegram": {"user_id": 55, "username": None}}
        ]), patch("bot.content_store._upstash_command", return_value="42"), patch(
            "bot.access_control.is_blocked", new_callable=AsyncMock, return_value=False
        ), patch("bot.content_store.prepare_owner_reply", new_callable=AsyncMock,
                return_value={"recipient_id": 55, "text": "Hello"}) as record, patch(
            "bot.content_store.finish_owner_reply", new_callable=AsyncMock):
            await archive_setup.reply_from_archive_topic(message)
        message.bot.send_message.assert_awaited_once()
        record.assert_awaited_once_with(17, 7, "-100123:501", "Hello")
        self.assertEqual(message.bot.send_message.await_args.kwargs["text"], "Ответ по заявке #7\n\nHello")


if __name__ == "__main__":
    unittest.main()
