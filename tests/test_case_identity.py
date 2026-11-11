"""Colliding wizard IDs must not overwrite another portfolio project."""
import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from bot import content_store
from bot.handlers import admin


class CaseIdentityTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.directory = Path(self.tmp.name)
        (self.directory / 'portfolio.json').write_text(json.dumps({'cases': []}))
        for patcher in (
            patch.object(content_store, 'DATA_DIR', self.directory),
            patch.object(content_store, '_upstash_enabled', return_value=False),
            patch.object(content_store.config, 'DESIGNER_CHAT_ID', '777'),
            patch.object(content_store, '_locks', {k: asyncio.Lock() for k in content_store.DATA_FILENAMES}),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    async def add(self, title):
        return await content_store.add_case(777, case_id='case_1', title=title,
            type_id='landing', cover='https://example.invalid/image.jpg', task='Task', related_service=None)

    async def test_concurrent_saves_get_distinct_ids_and_keep_both_contents(self):
        first, second = await asyncio.gather(self.add('First'), self.add('Second'))
        self.assertNotEqual(first['id'], second['id'])
        cases = await content_store.list_cases()
        self.assertEqual(len(cases), 2)
        self.assertEqual({c['title'] for c in cases}, {'First', 'Second'})

    async def test_wizard_attaches_source_to_the_saved_case_not_stale_id(self):
        await self.add('Existing')
        state = SimpleNamespace(get_data=AsyncMock(return_value={
            'case_id': 'case_1', 'title': 'New', 'type_id': 'landing',
            'cover': 'https://example.invalid/new.jpg',
            'external_url': 'https://example.invalid/project', 'source_type': 'behance',
        }))
        message = SimpleNamespace(chat=SimpleNamespace(id=777), text='New task')
        with patch.object(content_store, 'default_related_service_for_type', AsyncMock(return_value=None)), \
             patch.object(admin.flow, 'step_from_text', AsyncMock()) as step, \
             patch.object(admin.flow, 'finish_flow', AsyncMock()):
            await admin.cases_add_description(message, state)
        cases = await content_store.list_cases()
        existing = next(c for c in cases if c['title'] == 'Existing')
        new = next(c for c in cases if c['title'] == 'New')
        self.assertNotIn('external_url', existing)
        self.assertEqual(new['external_url'], 'https://example.invalid/project')
        callbacks = [b.callback_data for row in step.call_args.args[3].inline_keyboard for b in row]
        self.assertIn(f"adminsetcasestatus:published:{new['id']}", callbacks)
