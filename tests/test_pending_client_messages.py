"""Pending inbound messages survive restarts and cannot be misbound."""
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from bot import content_store, lead as lead_format
from bot.handlers import start


class PendingClientMessageTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.patches = [patch.object(content_store, 'DATA_DIR', Path(self.tmp.name)),
                        patch.object(content_store, '_upstash_enabled', return_value=False)]
        for p in self.patches:
            p.start()
        self.one = await content_store.add_lead({'service_name': 'A'}, {'user_id': 17})
        self.two = await content_store.add_lead({'service_name': 'B'}, {'user_id': 17})
        self.other = await content_store.add_lead({'service_name': 'C'}, {'user_id': 18})

    async def asyncTearDown(self):
        for p in reversed(self.patches):
            p.stop()
        self.tmp.cleanup()

    async def test_pending_text_is_reread_from_persistent_document_and_bound_once(self):
        await content_store.save_pending_client_message(17, 42, 'text', '<b>Original & text')
        await content_store.save_pending_client_message(17, 42, 'media', 'Tampered replay')
        result = await content_store.bind_pending_client_message(17, self.one['id'], 42)
        self.assertEqual(result['text'], '<b>Original & text')
        self.assertFalse(result['already_bound'])
        self.assertIsNone(await content_store.bind_pending_client_message(17, self.two['id'], 42))
        duplicate = await content_store.bind_pending_client_message(17, self.one['id'], 42)
        self.assertTrue(duplicate['already_bound'])
        stored = await content_store.get_lead(self.one['id'])
        self.assertEqual(len(stored['client_messages']), 1)
        self.assertEqual(stored['client_messages'][0]['text'], '<b>Original & text')

    async def test_cannot_bind_to_foreign_closed_or_fabricated_source(self):
        await content_store.save_pending_client_message(17, 42, 'media', 'Caption')
        self.assertIsNone(await content_store.bind_pending_client_message(17, self.other['id'], 42))
        self.assertIsNone(await content_store.bind_pending_client_message(18, self.other['id'], 42))
        self.assertIsNone(await content_store.bind_pending_client_message(17, self.one['id'], 999))
        document = await content_store._read('leads.json')
        document['leads'][0]['status'] = 'DONE'
        await content_store._write('leads.json', document)
        self.assertIsNone(await content_store.bind_pending_client_message(17, self.one['id'], 42))

    async def test_callback_persists_caption_when_archive_is_unavailable(self):
        await content_store.save_pending_client_message(17, 42, 'media', 'Caption retained')
        cb = SimpleNamespace(data=f"clientleadroute:42:{self.one['id']}:media", from_user=SimpleNamespace(id=17),
            message=SimpleNamespace(chat=SimpleNamespace(id=17, type='private'), edit_text=AsyncMock()),
            bot=SimpleNamespace(), answer=AsyncMock())
        with patch.object(start.client_materials_archive, 'archive_lead_material', AsyncMock(return_value=False)) as archive:
            await start.route_client_message_to_lead(cb)
            await start.route_client_message_to_lead(cb)
        archive.assert_awaited_once()
        stored = await content_store.get_lead(self.one['id'])
        self.assertEqual(stored['client_messages'][0]['text'], 'Caption retained')
        self.assertIn('прикреплено', cb.answer.await_args.args[0])

    async def test_ambiguous_media_can_be_reopened_from_existing_materials_screen(self):
        media = {"kind": "document", "file_id": "synthetic-file", "file_unique_id": "synthetic-unique"}
        await content_store.save_pending_client_message(17, 55, 'media', 'Document caption', media=media)
        await content_store.bind_pending_client_message(17, self.one['id'], 55)
        stored = await content_store.get_lead(self.one['id'])
        self.assertEqual(lead_format.lead_material_items(stored)[0]['file_id'], 'synthetic-file')
        self.assertEqual(stored['client_messages'][0]['text'], 'Document caption')

    async def test_pending_data_is_preserved_by_subsequent_lead_writes(self):
        await content_store.save_pending_client_message(17, 42, 'text', 'Kept')
        await content_store.add_client_message(17, self.one['id'], 43, 'text', 'Another')
        self.assertIsNotNone(await content_store.bind_pending_client_message(17, self.two['id'], 42))

    async def test_inbound_survives_failed_designer_notification(self):
        # Only one active lead, no guessed routing.
        document = await content_store._read('leads.json')
        document['leads'][1]['status'] = 'DONE'
        await content_store._write('leads.json', document)
        msg = SimpleNamespace(chat=SimpleNamespace(id=17, type='private'), from_user=SimpleNamespace(
            id=17, username=None, first_name='Test', last_name=None), text='Save before sending', message_id=60,
            bot=SimpleNamespace(send_message=AsyncMock(side_effect=RuntimeError('network'))), answer=AsyncMock())
        with patch.object(start.config, 'DESIGNER_CHAT_ID', '900'):
            await start.relay_client_text_to_designer(msg, SimpleNamespace())
        stored = await content_store.get_lead(self.one['id'])
        self.assertEqual(stored['client_messages'][0]['text'], 'Save before sending')
        self.assertIn('Не получилось', msg.answer.await_args.args[0])


class ConversationPagesTests(unittest.TestCase):
    def test_inbound_and_outbound_order_and_delivery_status(self):
        pages = lead_format.conversation_pages({
            'client_messages': [{'text': '<b>Client', 'received_at': '2026-11-11T01:00:00'}],
            'owner_messages': [{'text': 'Owner', 'sent_at': '2026-11-11T02:00:00', 'delivery_status': 'unknown'}]})
        self.assertIn('Дизайнер', pages[0])
        self.assertIn('Доставка не подтверждена', pages[0])
        self.assertIn('<b>Client', pages[1])  # handler uses parse_mode=None

    def test_long_unicode_message_is_split_without_loss(self):
        text = '😀<&' * 2000
        pages = lead_format.conversation_pages({'client_messages': [{'text': text}]})
        self.assertEqual(''.join(page.split('\n\n', 1)[1] for page in pages), text)
        self.assertTrue(all(len(page.encode('utf-16-le')) // 2 < 4096 for page in pages))

    def test_empty_conversation(self):
        self.assertEqual(lead_format.conversation_pages({}), [])
