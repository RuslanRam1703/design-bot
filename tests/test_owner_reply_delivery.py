"""Delivery uses synthetic leads and mocks; never contacts Telegram or Redis."""
import asyncio
import copy
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from bot import content_store, owner_replies


class DurableOwnerReplyTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.disk = [{"id": 7, "telegram": {"user_id": 55}, "owner_messages": []},
                     {"id": 8, "telegram": {"user_id": 66}, "owner_messages": []}]
        async def read():
            return copy.deepcopy(self.disk)
        async def write(leads):
            self.disk = copy.deepcopy(leads)
        self.patches = [patch.object(content_store.config, "DESIGNER_CHAT_ID", "17"),
                        patch.object(content_store, "_read_leads", read),
                        patch.object(content_store, "_write_leads", write),
                        patch.object(owner_replies.access_control, "is_blocked", AsyncMock(return_value=False))]
        for p in self.patches:
            p.start()
        self.message = SimpleNamespace(from_user=SimpleNamespace(id=17),
            chat=SimpleNamespace(id=17), message_id=51,
            bot=SimpleNamespace(send_message=AsyncMock()))

    async def asyncTearDown(self):
        for p in reversed(self.patches):
            p.stop()

    async def test_concurrent_redelivery_sends_once_and_persists_before_send(self):
        async def send(**kwargs):
            self.assertEqual(self.disk[0]["owner_messages"][0]["delivery_status"], "sending")
            self.assertEqual(kwargs["chat_id"], 55)
            self.assertIsNone(kwargs["parse_mode"])
            self.assertIn('<b>& text', kwargs["text"])
        self.message.bot.send_message.side_effect = send
        results = await asyncio.gather(*(owner_replies.deliver_owner_reply(
            self.message, self.disk[0], '<b>& text') for _ in range(2)))
        self.assertCountEqual(results, ["sent", "duplicate"])
        self.message.bot.send_message.assert_awaited_once()
        self.assertEqual(self.disk[0]["owner_messages"][0]["delivery_status"], "sent")

    async def test_replayed_update_cannot_switch_to_different_client(self):
        await owner_replies.deliver_owner_reply(self.message, self.disk[0], 'Hello')
        status = await owner_replies.deliver_owner_reply(self.message, self.disk[1], 'Hello')
        self.assertEqual(status, 'duplicate')
        self.message.bot.send_message.assert_awaited_once()
        self.assertEqual(self.disk[1]['owner_messages'], [])

    async def test_crashed_intent_remains_blocked_after_rereading_storage(self):
        await content_store.prepare_owner_reply(17, 7, '17:51', 'Hello')
        self.assertEqual(await owner_replies.deliver_owner_reply(self.message, self.disk[0], 'Hello'), 'duplicate')
        self.message.bot.send_message.assert_not_awaited()

    async def test_no_send_if_initial_storage_write_fails(self):
        with patch.object(content_store, '_write_leads', AsyncMock(side_effect=RuntimeError('storage down'))):
            status = await owner_replies.deliver_owner_reply(self.message, self.disk[0], 'Hello')
        self.assertEqual(status, 'unavailable')
        self.message.bot.send_message.assert_not_awaited()

    async def test_timeout_is_unknown_and_never_automatically_retried(self):
        self.message.bot.send_message.side_effect = asyncio.TimeoutError()
        self.assertEqual(await owner_replies.deliver_owner_reply(self.message, self.disk[0], 'Hello'), 'unknown')
        self.assertEqual(self.disk[0]['owner_messages'][0]['delivery_status'], 'unknown')
        self.assertEqual(await owner_replies.deliver_owner_reply(self.message, self.disk[0], 'Hello'), 'duplicate')
        self.message.bot.send_message.assert_awaited_once()

    async def test_final_storage_failure_does_not_claim_success(self):
        with patch.object(content_store, 'finish_owner_reply', AsyncMock(side_effect=RuntimeError('storage down'))):
            status = await owner_replies.deliver_owner_reply(self.message, self.disk[0], 'Hello')
        self.assertEqual(status, 'unknown')
        self.assertEqual(await owner_replies.deliver_owner_reply(self.message, self.disk[0], 'Hello'), 'duplicate')

    async def test_blocked_recipient_and_oversized_unicode_never_send(self):
        with patch.object(owner_replies.access_control, 'is_blocked', AsyncMock(return_value=True)):
            self.assertEqual(await owner_replies.deliver_owner_reply(self.message, self.disk[0], 'Hello'), 'blocked')
        self.assertEqual(await owner_replies.deliver_owner_reply(self.message, self.disk[0], '😀' * 2001), 'invalid')
        self.message.bot.send_message.assert_not_awaited()

    async def test_wrong_actor_cannot_prepare_or_send(self):
        self.message.from_user.id = 18
        with self.assertRaises(content_store.NotDesignerError):
            await owner_replies.deliver_owner_reply(self.message, self.disk[0], 'Hello')
        self.message.bot.send_message.assert_not_awaited()

    async def test_frozen_recipient_comes_from_storage_not_handler_snapshot(self):
        stale = dict(self.disk[0], telegram={"user_id": 99})
        self.assertEqual(await owner_replies.deliver_owner_reply(self.message, stale, 'Hello'), 'sent')
        self.assertEqual(self.message.bot.send_message.await_args.kwargs['chat_id'], 55)

    async def test_server_error_does_not_claim_definitive_rejection(self):
        from aiogram.exceptions import TelegramServerError
        self.message.bot.send_message.side_effect = TelegramServerError(method=None, message='server error')
        self.assertEqual(await owner_replies.deliver_owner_reply(self.message, self.disk[0], 'Hello'), 'unknown')
