"""Archive copies have durable claims; uncertain outcomes never auto-retry."""
import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from aiogram.exceptions import TelegramBadRequest, TelegramServerError
from bot import content_store, client_materials_archive as archive


class ArchiveOutboxTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = {}
        def redis(*args):
            op, key = args[:2]
            if op == 'GET':
                return self.db.get(key)
            if op == 'SET':
                self.db[key] = args[2]
                return 'OK'
            raise AssertionError(args)
        self.patches = [patch.object(content_store, 'DATA_DIR', Path(self.tmp.name)),
            patch.object(content_store, '_upstash_enabled', return_value=False),
            patch.object(archive.config, 'UPSTASH_REDIS_REST_URL', 'https://example.invalid'),
            patch.object(archive.config, 'UPSTASH_REDIS_REST_TOKEN', 'test'),
            patch.object(archive.config, 'CLIENT_MATERIALS_CHANNEL', '-100456'),
            patch.object(content_store, '_upstash_command', side_effect=redis)]
        for p in self.patches:
            p.start()
        self.lead = await content_store.add_lead({'service_name': 'Test'}, {'user_id': 123})
        self.bot = SimpleNamespace(create_forum_topic=AsyncMock(return_value=SimpleNamespace(message_thread_id=77)),
                                   copy_message=AsyncMock())
        self.msg = SimpleNamespace(bot=self.bot, chat=SimpleNamespace(id=123), message_id=42)

    async def asyncTearDown(self):
        for p in reversed(self.patches):
            p.stop()
        self.tmp.cleanup()

    async def records(self):
        return (await content_store.get_lead(self.lead['id'])).get('archive_deliveries', [])

    async def test_duplicate_and_concurrent_copy_is_sent_once(self):
        async def copy(**kwargs):
            self.assertEqual((await self.records())[0]['state'], 'sending')
        self.bot.copy_message.side_effect = copy
        await asyncio.gather(*(archive.archive_lead_material(self.msg, self.lead['id']) for _ in range(2)))
        self.assertTrue(await archive.archive_lead_material(self.msg, self.lead['id']))
        self.bot.copy_message.assert_awaited_once()
        self.assertEqual((await self.records())[0]['state'], 'sent')

    async def test_forbidden_source_never_copies_to_other_clients_topic(self):
        self.msg.chat.id = 999
        self.assertFalse(await archive.archive_lead_material(self.msg, self.lead['id']))
        self.bot.copy_message.assert_not_awaited()
        self.bot.create_forum_topic.assert_not_awaited()

    async def test_known_rejection_retries_manually_using_same_topic(self):
        self.bot.copy_message.side_effect = [TelegramBadRequest(method=None, message='not available'), None]
        self.assertFalse(await archive.archive_lead_material(self.msg, self.lead['id']))
        self.assertEqual((await self.records())[0]['state'], 'failed')
        self.assertEqual(await archive.retry_archive_copies(self.bot), 0)  # backoff
        self.assertEqual(await archive.retry_archive_copies(self.bot, lead_id=self.lead['id'], manual=True), 1)
        self.assertEqual((await self.records())[0]['state'], 'sent')
        self.bot.create_forum_topic.assert_awaited_once()
        self.assertEqual(self.bot.copy_message.await_count, 2)

    async def test_timeout_after_copy_start_is_unknown_and_not_retried(self):
        self.bot.copy_message.side_effect = asyncio.TimeoutError()
        self.assertFalse(await archive.archive_lead_material(self.msg, self.lead['id']))
        self.assertEqual((await self.records())[0]['state'], 'unknown')
        self.assertEqual(await archive.retry_archive_copies(self.bot, manual=True), 0)
        self.assertFalse(await archive.archive_lead_material(self.msg, self.lead['id']))
        self.bot.copy_message.assert_awaited_once()

    async def test_server_error_is_uncertain_and_never_retried(self):
        self.bot.copy_message.side_effect = TelegramServerError(method=None, message='server error')
        await archive.archive_lead_material(self.msg, self.lead['id'])
        self.assertEqual((await self.records())[0]['state'], 'unknown')
        self.assertEqual(await archive.retry_archive_copies(self.bot, manual=True), 0)
        self.bot.copy_message.assert_awaited_once()

    async def test_mapping_failure_before_copy_is_retryable(self):
        with patch.object(content_store, '_upstash_command', side_effect=RuntimeError('storage down')):
            self.assertFalse(await archive.archive_lead_material(self.msg, self.lead['id']))
        self.assertEqual((await self.records())[0]['state'], 'failed')
        self.bot.copy_message.assert_not_awaited()
        self.assertEqual(await archive.retry_archive_copies(self.bot, manual=True), 1)

    async def test_crash_leaves_claim_persisted_and_blocks_replay(self):
        await content_store.prepare_archive_copy(self.lead['id'], 123, 42, -100456)
        self.assertFalse(await archive.archive_lead_material(self.msg, self.lead['id']))
        self.assertEqual(await archive.retry_archive_copies(self.bot, manual=True), 0)
        self.bot.copy_message.assert_not_awaited()

    async def test_final_storage_failure_cannot_enable_duplicate_copy(self):
        with patch.object(content_store, 'finish_archive_copy', AsyncMock(side_effect=RuntimeError('down'))):
            self.assertFalse(await archive.archive_lead_material(self.msg, self.lead['id']))
        self.assertFalse(await archive.archive_lead_material(self.msg, self.lead['id']))
        self.bot.copy_message.assert_awaited_once()

    async def test_retry_never_switches_archive_groups(self):
        self.bot.copy_message.side_effect = TelegramBadRequest(method=None, message='failed')
        await archive.archive_lead_material(self.msg, self.lead['id'])
        with patch.object(archive.config, 'CLIENT_MATERIALS_CHANNEL', '-100999'):
            self.assertEqual(await archive.retry_archive_copies(self.bot, manual=True), 0)
        self.bot.copy_message.assert_awaited_once()

    async def test_automatic_retry_cap_still_allows_explicit_owner_retry(self):
        claim = await content_store.prepare_archive_copy(self.lead['id'], 123, 42, -100456)
        await content_store.finish_archive_copy(self.lead['id'], claim['key'], claim['claim_token'], 'failed')
        document = await content_store._read('leads.json')
        document['leads'][0]['archive_deliveries'][0].update(attempts=5, retry_after='2000-01-01')
        await content_store._write('leads.json', document)
        self.assertEqual(await archive.retry_archive_copies(self.bot), 0)
        self.assertEqual(await archive.retry_archive_copies(self.bot, manual=True), 1)

    async def test_stale_result_cannot_overwrite_claim(self):
        claim = await content_store.prepare_archive_copy(self.lead['id'], 123, 42, -100456)
        with self.assertRaises(content_store.StorageLeaseLostError):
            await content_store.finish_archive_copy(self.lead['id'], claim['key'], 'wrong', 'sent')
        self.assertEqual((await self.records())[0]['state'], 'sending')

    async def test_missing_topic_detaches_only_exact_mapping_and_preserves_lead(self):
        self.bot.copy_message.side_effect = TelegramBadRequest(method=None, message='message thread not found')
        self.assertFalse(await archive.archive_lead_material(self.msg, self.lead['id']))
        self.assertIsNotNone(await content_store.get_lead(self.lead['id']))
        self.assertEqual((await self.records())[0]['state'], 'failed')
        # Only this exact stale mapping is invalidated, never all Redis keys.
        commands = content_store._upstash_command.call_args_list
        self.assertTrue(any(c.args[0] == 'EVAL' and c.args[-1] == '77' for c in commands))
