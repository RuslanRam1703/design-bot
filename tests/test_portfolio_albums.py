"""Album import, moderation, source edits and safe owner notifications."""
import copy
import unittest
from unittest.mock import AsyncMock, patch
from bot import content_store, telegram_media
from bot.handlers import portfolio_channel
from test_portfolio_channel import fake_message


class AlbumStoreTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.data = {"cases": [], "types": []}
        async def read(_):
            return copy.deepcopy(self.data)
        async def write(_, data):
            self.data = copy.deepcopy(data)
        self.patches = [patch.object(content_store, '_upstash_enabled', return_value=False),
                        patch.object(content_store, '_read', read), patch.object(content_store, '_write', write)]
        for p in self.patches:
            p.start()

    async def asyncTearDown(self):
        for p in reversed(self.patches):
            p.stop()

    async def upsert(self, message_id=11, **kwargs):
        args = dict(channel_username='Portfolio', message_id=message_id, title='Title', cover='a.jpg',
                    media_group_id='album1', images=['a.jpg', 'b.jpg'], caption='Title\nFull description')
        args.update(kwargs)
        return await content_store.upsert_channel_case(**args)

    async def test_album_has_one_draft_ordered_gallery_and_full_caption(self):
        await self.upsert(12, caption=None)
        case, created = await self.upsert(11)
        self.assertFalse(created)
        self.assertEqual(len(self.data['cases']), 1)
        self.assertEqual(case['images'], ['a.jpg', 'b.jpg'])
        self.assertEqual(case['task'], 'Title\nFull description')
        self.assertEqual(case['source_post_url'], 'https://t.me/portfolio/11')
        self.assertEqual(case['status'], 'draft')
        self.assertIsNone(case['type'])
        self.assertEqual(case['title'], 'Title')

    async def test_other_album_and_channel_are_distinct(self):
        await self.upsert()
        await self.upsert(20, media_group_id='album2')
        await self.upsert(channel_username='Other')
        self.assertEqual(len(self.data['cases']), 3)

    async def test_unmodified_draft_follows_source_edit(self):
        await self.upsert()
        case, _ = await self.upsert(title='New', cover='new.jpg', images=['new.jpg'], caption='New description')
        self.assertEqual(case['title'], 'New')
        self.assertEqual(case['cover'], 'new.jpg')
        self.assertEqual(case['images'], ['new.jpg'])
        self.assertEqual(case['task'], 'New description')

    async def test_curated_draft_fields_survive_source_edit(self):
        await self.upsert()
        self.data['cases'][0].update(title='Curated', cover='custom.jpg', images=['custom.jpg'], task='My task')
        case, _ = await self.upsert(title='Edited', cover='new.jpg', images=['new.jpg'], caption='Edited caption')
        self.assertEqual([case['title'], case['cover'], case['images'], case['task']],
                         ['Curated', 'custom.jpg', ['custom.jpg'], 'My task'])
        self.assertEqual(case['source_images'], ['new.jpg'])

    async def test_published_rejected_and_trashed_projects_never_change_from_channel(self):
        await self.upsert()
        for status in ('published', 'rejected', 'trashed'):
            self.data['cases'][0]['status'] = status
            before = copy.deepcopy(self.data)
            await self.upsert(title='Edited', caption='Changed')
            self.assertEqual(self.data, before)


class AlbumResolverTests(unittest.IsolatedAsyncioTestCase):
    async def test_order_dedup_and_validation(self):
        urls = ['https://cdn1.telesco.pe/file/one.jpg', 'https://cdn1.telesco.pe/file/two.jpg']
        with patch.object(telegram_media, '_http_get', return_value=(200, b'gallery')), \
             patch.object(telegram_media, 'extract_embed_media_urls', return_value=[urls[0], urls[1], urls[0]]), \
             patch.object(telegram_media, '_is_accessible_image', AsyncMock(return_value=True)):
            self.assertEqual(await telegram_media.resolve_album_urls('https://t.me/portfolio/11'), urls)

    async def test_partial_album_failure_is_visible_instead_of_publishing_subset(self):
        with patch.object(telegram_media, '_http_get', return_value=(200, b'gallery')), \
             patch.object(telegram_media, 'extract_embed_media_urls', return_value=['https://cdn1.telesco.pe/file/a.jpg']), \
             patch.object(telegram_media, '_is_accessible_image', AsyncMock(return_value=False)):
            with self.assertRaises(telegram_media.TelegramMediaResolveError):
                await telegram_media.resolve_album_urls('https://t.me/portfolio/11')

    async def test_import_failure_notifies_owner_without_false_success(self):
        message = fake_message(caption='<b>Untrusted')
        message.media_group_id = 'album1'
        with patch.object(portfolio_channel.config, 'PORTFOLIO_CHANNEL', '@portfolio_rrr17'), \
             patch.object(portfolio_channel.config, 'DESIGNER_CHAT_ID', '17'), \
             patch.object(telegram_media, 'resolve_album_urls', AsyncMock(side_effect=RuntimeError('network'))), \
             patch.object(content_store, 'upsert_channel_case', AsyncMock()) as upsert:
            await portfolio_channel.import_post(message)
        upsert.assert_not_awaited()
        self.assertIn('Не удалось импортировать', message.bot.send_message.await_args.kwargs['text'])
        self.assertIsNone(message.bot.send_message.await_args.kwargs['parse_mode'])
