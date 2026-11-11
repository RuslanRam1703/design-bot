"""Owner drafts must never mutate public data before explicit reviewed publication."""
import copy
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from aiohttp.test_utils import TestClient, TestServer
from aiohttp import web
from bot import content_store as store, config, webserver, media_archive
from bot.handlers import admin


class EditorTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.actor = config.DESIGNER_CHAT_ID
        self.data = {
            'portfolio.json': {'types': [{'id': 'web'}], 'cases': [{'id': 'one', 'title': 'Live', 'type': 'web', 'status': 'published', 'cover': 'old.jpg', 'images': ['old.jpg'], 'source_ref': 'archive:@archive:1', 'sections': []}]},
            'pricing.json': {'services': [{'id': 'S', 'name': 'Service', 'base_price': 1000, 'term_min': 1, 'term_max': 3, 'includes': ''}], 'options': [], 'coefficients': {'urgent': {'multiplier': 2}, 'complex': {'multiplier': 1.5}}, 'rounding': {'price_from_factor': .9, 'price_to_factor': 1.1, 'round_to': 100}},
        }
        async def read(name): return copy.deepcopy(self.data[name])
        async def write(name, data): self.data[name] = copy.deepcopy(data)
        for p in [patch.object(store, '_read', side_effect=read), patch.object(store, '_write', side_effect=write), patch.object(store, '_upstash_enabled', return_value=False)]:
            p.start(); self.addCleanup(p.stop)
        self.r2 = patch.object(store.r2_storage, 'delete_image', new_callable=AsyncMock).start()
        self.addCleanup(patch.stopall)

    async def case(self): return await store.get_case_edit(self.actor, 'one')

    async def test_case_changes_stay_private_and_publish_atomically(self):
        await store.begin_case_edit(self.actor, 'one')
        await store.update_case(self.actor, 'one', title='Working', stage=True)
        await store.add_case_image(self.actor, 'one', 'new.jpg', stage=True)
        self.assertEqual((await store.list_cases())[0]['title'], 'Live')
        working = await self.case()
        self.assertEqual(working['title'], 'Working')
        self.assertTrue(await store.finish_case_edit(self.actor, 'one', store.editor_digest(working), publish=True))
        live = (await store.list_cases())[0]
        self.assertEqual(live['title'], 'Working'); self.assertNotIn('_editor', live)
        self.assertEqual(live['images'], ['old.jpg', 'new.jpg'])
        # A subsequent edit from an old form must stage again, never write live.
        await store.update_case(self.actor, 'one', title='Later', stage=True)
        self.assertEqual((await store.list_cases())[0]['title'], 'Working')

    async def test_sections_cover_and_order_use_same_revision(self):
        await store.add_case_image(self.actor, 'one', 'new.jpg', stage=True)
        await store.set_case_cover(self.actor, 'one', 'new.jpg', stage=True)
        await store.reorder_case_image(self.actor, 'one', 'new.jpg', 'up', stage=True)
        await store.add_case_section(self.actor, 'one', section_type='gallery', title='Process', images=['a.jpg'], stage=True)
        await store.add_case_section_image(self.actor, 'one', 0, 'b.jpg', stage=True)
        self.assertEqual((await self.case())['cover'], 'new.jpg')
        self.assertEqual((await self.case())['sections'][0]['images'], ['a.jpg', 'b.jpg'])
        self.assertEqual((await store.list_cases())[0]['sections'], [])

    async def test_media_in_live_version_survives_removal_and_discard(self):
        await store.remove_case_image(self.actor, 'one', 'old.jpg', stage=True)
        self.r2.assert_not_awaited()
        bot = SimpleNamespace(delete_message=AsyncMock())
        self.assertFalse(await media_archive.delete_archive_message(bot, 'archive:@archive:1'))
        bot.delete_message.assert_not_awaited()
        working = await self.case()
        await store.finish_case_edit(self.actor, 'one', store.editor_digest(working), publish=False)
        self.assertEqual((await store.list_cases())[0]['images'], ['old.jpg'])
        self.assertTrue(await store.editor_media_retained('old.jpg'))
        self.assertTrue(await store.editor_media_retained('archive:@archive:1'))

    async def test_changed_case_rejects_old_confirmation(self):
        await store.begin_case_edit(self.actor, 'one'); digest = store.editor_digest(await self.case())
        await store.update_case(self.actor, 'one', title='Later', stage=True)
        with self.assertRaises(ValueError): await store.finish_case_edit(self.actor, 'one', digest, publish=True)
        self.assertEqual((await store.list_cases())[0]['title'], 'Live')

    async def test_live_change_rejects_revision_overwrite(self):
        await store.begin_case_edit(self.actor, 'one')
        await store.set_case_status(self.actor, 'one', 'unpublished')
        with self.assertRaises(ValueError):
            await store.finish_case_edit(self.actor, 'one', store.editor_digest(await self.case()), publish=True)
        self.assertEqual((await store.list_cases())[0]['status'], 'unpublished')

    async def test_drafts_persist_in_storage_and_resume_without_reset(self):
        await store.update_case(self.actor, 'one', title='Draft', stage=True)
        await store.update_service(self.actor, 'S', base_price=2500, stage=True)
        await store.begin_case_edit(self.actor, 'one'); await store.begin_pricing_edit(self.actor)
        self.assertEqual((await self.case())['title'], 'Draft')
        self.assertEqual((await store.get_pricing_edit(self.actor))['services'][0]['base_price'], 2500)
        self.assertEqual(self.data['pricing.json']['services'][0]['base_price'], 1000)

    async def test_price_apply_discard_and_stale_confirmation(self):
        await store.update_service(self.actor, 'S', base_price=2000, stage=True)
        working = await store.get_pricing_edit(self.actor); digest = store.editor_digest(working)
        await store.update_service(self.actor, 'S', base_price=3000, stage=True)
        with self.assertRaises(ValueError): await store.finish_pricing_edit(self.actor, digest, publish=True)
        digest = store.editor_digest(await store.get_pricing_edit(self.actor))
        await store.finish_pricing_edit(self.actor, digest, publish=False)
        self.assertEqual(self.data['pricing.json']['services'][0]['base_price'], 1000)
        await store.update_service(self.actor, 'S', base_price=2000, stage=True)
        digest = store.editor_digest(await store.get_pricing_edit(self.actor))
        await store.finish_pricing_edit(self.actor, digest, publish=True)
        self.assertEqual(self.data['pricing.json']['services'][0]['base_price'], 2000)

    async def test_invalid_price_never_reaches_clients(self):
        for fields in [{'term_min': 10}, {'base_price': float('nan')}, {'base_price': -1}]:
            await store.update_service(self.actor, 'S', **fields, stage=True)
            with self.assertRaises(ValueError):
                await store.finish_pricing_edit(self.actor, store.editor_digest(await store.get_pricing_edit(self.actor)), publish=True)
            self.assertEqual(self.data['pricing.json']['services'][0]['base_price'], 1000)
            await store.finish_pricing_edit(self.actor, store.editor_digest(await store.get_pricing_edit(self.actor)), publish=False)

    async def test_unauthorized_actor_cannot_read_or_publish_drafts(self):
        for operation in [store.begin_case_edit('other', 'one'), store.get_case_edit('other', 'one'), store.begin_pricing_edit('other'), store.get_pricing_edit('other'), store.finish_case_edit('other', 'one', '', publish=True), store.finish_pricing_edit('other', '', publish=True)]:
            with self.assertRaises(store.NotDesignerError): await operation

    async def test_public_routes_hide_revisions_and_owner_preview_requires_identity(self):
        await store.update_case(self.actor, 'one', title='Secret draft', stage=True)
        await store.update_service(self.actor, 'S', base_price=9000, stage=True)
        app = web.Application(); app.router.add_get('/data/{filename}', webserver.handle_public_data)
        async with TestClient(TestServer(app)) as client:
            with patch.object(config, 'PRIVATE_MINIAPP_CONTENT', False):
                r = await client.get('/data/portfolio.json'); public = await r.json()
                self.assertNotIn('Secret draft', str(public)); self.assertNotIn('_editor', str(public))
                r = await client.get('/data/pricing.json'); self.assertNotIn('_editor', await r.text())
                r = await client.get('/data/portfolio.json?preview_case=one'); self.assertEqual(r.status, 403)
                with patch.object(webserver, 'validate_init_data', return_value={'id': int(self.actor)}), patch.object(webserver.access_control, 'is_blocked', return_value=False):
                    r = await client.get('/data/portfolio.json?preview_case=one', headers={'X-Telegram-Init-Data':'test'})
                    self.assertEqual(r.status, 200); self.assertEqual(r.headers['Cache-Control'], 'no-store')
                    self.assertEqual((await r.json())['cases'][0]['title'], 'Secret draft')
                with patch.object(webserver, 'validate_init_data', return_value={'id': 222}), patch.object(webserver.access_control, 'is_blocked', return_value=False):
                    r = await client.get('/data/portfolio.json?preview_case=one', headers={'X-Telegram-Init-Data':'test'})
                    self.assertEqual(r.status,403)

    async def test_review_requires_sample_and_rejects_stale_button(self):
        callback = SimpleNamespace(data='adminapply:abc', message=SimpleNamespace(chat=SimpleNamespace(id=self.actor), edit_text=AsyncMock()), answer=AsyncMock())
        state = SimpleNamespace(get_data=AsyncMock(return_value={'review_digest':'abc','review_kind':'pricing'}))
        with patch.object(store, 'finish_pricing_edit', new_callable=AsyncMock) as finish:
            await admin.review_finish(callback,state); finish.assert_not_awaited()
        callback.data='adminapply:old'
        await admin.review_finish(callback,state)
        self.assertIn('устарела', callback.answer.call_args.args[0])

    async def test_staged_service_deletion_does_not_change_live_prices_or_portfolio(self):
        self.data['pricing.json']['services'].append({'id':'T','name':'Other','base_price':500,'term_min':1,'term_max':2})
        self.data['portfolio.json']['cases'][0]['related_service']='S'
        before=copy.deepcopy(self.data['portfolio.json'])
        await store.delete_service(self.actor,'S',stage=True)
        self.assertEqual(len(self.data['pricing.json']['services']),2)
        self.assertEqual(self.data['portfolio.json'],before)
        self.assertEqual([s['id'] for s in await store.list_services(live=True)],['S','T'])
        self.assertEqual([s['id'] for s in await store.list_services()],['T'])
        await store.finish_pricing_edit(self.actor,store.editor_digest(await store.get_pricing_edit(self.actor)),publish=True)
        self.assertEqual([s['id'] for s in self.data['pricing.json']['services']],['T'])
        self.assertEqual(self.data['portfolio.json'],before)

    async def test_cover_replacement_retains_reference_of_image_kept_in_gallery(self):
        await store.update_case(self.actor,'one',cover='new.jpg',source_ref='archive:@archive:2',stage=True)
        working=await self.case()
        self.assertEqual(working['image_refs']['old.jpg'],'archive:@archive:1')
        await store.finish_case_edit(self.actor,'one',store.editor_digest(working),publish=True)
        self.assertTrue(await store.editor_media_retained('archive:@archive:1'))

    async def test_price_sample_uses_real_calculator_without_creating_lead(self):
        await store.begin_pricing_edit(self.actor)
        saved={'review_digest':store.editor_digest(await store.get_pricing_edit(self.actor))}
        async def update(**kwargs): saved.update(kwargs)
        state=SimpleNamespace(get_data=AsyncMock(side_effect=lambda:copy.deepcopy(saved)), update_data=AsyncMock(side_effect=update))
        callback=SimpleNamespace(data='adminsample:0',message=SimpleNamespace(chat=SimpleNamespace(id=self.actor),edit_text=AsyncMock()),answer=AsyncMock())
        await admin.review_price_sample(callback,state)
        text=callback.message.edit_text.call_args.args[0]
        self.assertIn('900–1,100',text)
        self.assertIn('1,800–2,200',text)
        self.assertEqual(saved['sample_digest'],saved['review_digest'])
        self.assertNotIn('leads.json',self.data)

    async def test_overflowing_combination_cannot_publish_even_when_values_are_finite(self):
        await store.update_service(self.actor,'S',base_price=1e308,stage=True)
        await store.update_coefficient(self.actor,'urgent',1e308,stage=True)
        with self.assertRaises(ValueError):
            await store.finish_pricing_edit(self.actor,store.editor_digest(await store.get_pricing_edit(self.actor)),publish=True)
        self.assertEqual(self.data['pricing.json']['services'][0]['base_price'],1000)
