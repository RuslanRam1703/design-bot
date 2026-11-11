import copy
import io
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch
import zipfile

from aiogram.exceptions import TelegramBadRequest
from aiogram.methods import DeleteForumTopic
from bot import config, content_store as store, owner_maintenance as maintenance


class MaintenanceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp=TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.path=Path(self.tmp.name)
        self.docs={name:json.loads((store.DATA_DIR/name).read_text()) for name in store.DATA_FILENAMES if (store.DATA_DIR/name).exists()}
        self.docs['leads.json']={'leads':[{'id':17,'status':'NEW','created_at':'2026-11-11T00:00:00+00:00','telegram':{'user_id':111},'payload':{}}],'projects':[], 'access_control':{'blocked_ids':{'999':{}},'watch_usernames':{}}}
        for name,data in self.docs.items(): (self.path/name).write_text(json.dumps(data))
        for p in [patch.object(store,'DATA_DIR',self.path),patch.object(store,'_upstash_enabled',return_value=False),patch.object(store,'IMG_PORTFOLIO_DIR',self.path/'none'),patch.object(store,'IMG_ABOUT_DIR',self.path/'none')]:
            p.start();self.addCleanup(p.stop)
        self.actor=config.DESIGNER_CHAT_ID

    async def test_backup_precedes_clean_start_and_preserves_settings_and_identity(self):
        backup,digest,counts=await maintenance.prepare_clean_start(self.actor)
        self.assertEqual(counts['Заявки'],1)
        with zipfile.ZipFile(io.BytesIO(backup)) as z:
            self.assertEqual(json.loads(z.read('data/leads.json'))['leads'][0]['id'],17)
        await maintenance.clean_start(self.actor,digest)
        self.assertEqual((await store._read('portfolio.json'))['cases'],[])
        self.assertEqual((await store._read('portfolio.json'))['types'],[])
        self.assertEqual((await store._read('pricing.json'))['services'],[])
        self.assertEqual((await store._read('faq.json'))['faq'],[])
        self.assertEqual(await store.get_about(),{})
        self.assertEqual((await store._read('leads.json'))['access_control'],self.docs['leads.json']['access_control'])
        self.assertEqual((await store._read('ui_config.json'))['menu'],self.docs['ui_config.json']['menu'])
        await store.ensure_gallery_demo_case()
        self.assertEqual(await store.list_cases(),[])
        lead=await store.add_lead({'service_name':'New'}, {'user_id':222})
        self.assertEqual(lead['id'],18)

    async def test_deleting_highest_lead_never_reuses_its_topic_identity(self):
        await store.delete_lead(self.actor,17)
        lead=await store.add_lead({'service_name':'New'},{'user_id':222})
        self.assertEqual(lead['id'],18)

    async def test_changed_data_rejects_cleanup_and_requires_new_backup(self):
        _,digest,_=await maintenance.prepare_clean_start(self.actor)
        await store.add_lead({'service_name':'New'},{'user_id':222})
        with self.assertRaises(ValueError): await maintenance.clean_start(self.actor,digest)
        self.assertEqual(len(await store.list_leads()),2)

    async def test_partial_failure_rolls_back_all_attempted_keys(self):
        _,digest,_=await maintenance.prepare_clean_start(self.actor)
        original=store._write; failed=False
        async def write(name,data):
            nonlocal failed
            if name=='faq.json' and not failed:
                failed=True;raise RuntimeError('test storage outage')
            await original(name,data)
        with patch.object(store,'_write',side_effect=write):
            with self.assertRaises(maintenance.ResetFailedError): await maintenance.clean_start(self.actor,digest)
        for name,data in self.docs.items(): self.assertEqual(await store._read(name),data)

    async def test_unauthorized_reset_is_rejected(self):
        with self.assertRaises(store.NotDesignerError): await maintenance.prepare_clean_start('other')
        with self.assertRaises(store.NotDesignerError): await maintenance.clean_start('other','x')

    async def test_topic_failure_preserves_lead_and_success_removes_it(self):
        bot=SimpleNamespace(delete_forum_topic=AsyncMock(side_effect=RuntimeError('network failure')))
        with patch.object(config,'CLIENT_MATERIALS_CHANNEL','-100123'),patch.object(store,'is_redis_backed',return_value=True),patch.object(store,'_upstash_command',return_value='77'):
            with self.assertRaises(RuntimeError): await maintenance.delete_lead_with_topic(self.actor,17,bot)
            self.assertEqual(len(await store.list_leads()),1)
            bot.delete_forum_topic=AsyncMock(return_value=True)
            self.assertTrue(await maintenance.delete_lead_with_topic(self.actor,17,bot))
            bot.delete_forum_topic.assert_awaited_once_with(chat_id=-100123,message_thread_id=77)
            self.assertEqual(await store.list_leads(),[])

    async def test_project_protection_happens_before_topic_deletion(self):
        document=await store._read('leads.json');document['projects']=[{'lead_id':17}]
        await store._write('leads.json',document)
        bot=SimpleNamespace(delete_forum_topic=AsyncMock())
        with patch.object(config,'CLIENT_MATERIALS_CHANNEL','-100123'),patch.object(store,'is_redis_backed',return_value=True):
            with self.assertRaises(ValueError): await maintenance.delete_lead_with_topic(self.actor,17,bot)
        bot.delete_forum_topic.assert_not_awaited()

    async def test_missing_topic_allows_explicit_record_removal(self):
        error=TelegramBadRequest(method=DeleteForumTopic(chat_id=-100123,message_thread_id=77),message='message thread not found')
        bot=SimpleNamespace(delete_forum_topic=AsyncMock(side_effect=error))
        with patch.object(config,'CLIENT_MATERIALS_CHANNEL','-100123'),patch.object(store,'is_redis_backed',return_value=True),patch.object(store,'_upstash_command',return_value='77'):
            self.assertTrue(await maintenance.delete_lead_with_topic(self.actor,17,bot))
        self.assertEqual(await store.list_leads(),[])

    async def test_failed_backup_delivery_cannot_enable_reset_confirmation(self):
        from bot.handlers import admin
        callback=SimpleNamespace(answer=AsyncMock(),message=SimpleNamespace(chat=SimpleNamespace(id=self.actor),answer_document=AsyncMock(side_effect=RuntimeError('upload failed')),edit_text=AsyncMock()))
        state=SimpleNamespace(update_data=AsyncMock())
        with self.assertRaises(RuntimeError): await admin.clean_start_prepare(callback,state)
        state.update_data.assert_not_awaited()
        callback.message.edit_text.assert_not_awaited()

    async def test_restore_old_backup_keeps_highwater_from_newer_leads(self):
        backup,_,_=await maintenance.prepare_clean_start(self.actor)
        new=await store.add_lead({'service_name':'New'},{'user_id':222})
        self.assertEqual(new['id'],18)
        await store.import_backup_bytes(self.actor,backup)
        new=await store.add_lead({'service_name':'Next'},{'user_id':333})
        self.assertEqual(new['id'],19)
