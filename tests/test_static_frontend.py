"""CDN frontend integration: strict CORS, signed identity and isolated export."""
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch
from aiohttp.test_utils import TestClient, TestServer
from bot import config, content_store, webserver
from scripts.build_static import build, ROOT, ROUTES
from test_regression import _sign_init_data

FRONTEND = 'https://frontend.example'


class StaticFrontendApiTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.patches = [patch.object(config, 'MINIAPP_URL', FRONTEND),
            patch.object(config, 'WEBAPP_URL', 'https://backend.example'),
            patch.object(content_store, 'DATA_DIR', Path(self.tmp.name)),
            patch.object(content_store, '_upstash_enabled', return_value=False)]
        for p in self.patches:
            p.start()
        self.bot = AsyncMock()
        self.client = TestClient(TestServer(webserver.create_app(self.bot)))
        await self.client.start_server()

    async def asyncTearDown(self):
        await self.client.close()
        for p in reversed(self.patches):
            p.stop()
        self.tmp.cleanup()

    def signed(self, user_id=17):
        return _sign_init_data({'auth_date': str(int(time.time())), 'user': json.dumps({'id': user_id, 'first_name': 'Test'})}, config.BOT_TOKEN)

    async def test_preflight_allows_only_configured_origin_and_required_headers(self):
        response = await self.client.options('/api/leads', headers={'Origin': FRONTEND,
            'Access-Control-Request-Method': 'POST', 'Access-Control-Request-Headers': 'content-type,x-telegram-init-data'})
        self.assertEqual(response.status, 204)
        self.assertEqual(response.headers['Access-Control-Allow-Origin'], FRONTEND)
        self.assertNotIn('Access-Control-Allow-Credentials', response.headers)
        for origin in ('https://evil.example', 'null'):
            response = await self.client.options('/api/leads', headers={'Origin': origin, 'Access-Control-Request-Method': 'POST'})
            self.assertEqual(response.status, 403)
            self.assertNotIn('Access-Control-Allow-Origin', response.headers)
        response = await self.client.options('/api/leads', headers={'Origin': FRONTEND,
            'Access-Control-Request-Method': 'DELETE'})
        self.assertEqual(response.status, 403)

    async def test_real_signed_identity_from_cdn_sees_only_own_leads(self):
        own = await content_store.add_lead({'service_name': 'Own'}, {'user_id': 17})
        await content_store.add_lead({'service_name': 'Foreign'}, {'user_id': 18})
        response = await self.client.get('/api/my-leads', headers={'Origin': FRONTEND, 'X-Telegram-Init-Data': self.signed()})
        self.assertEqual(response.status, 200)
        self.assertEqual([lead['id'] for lead in await response.json()], [own['id']])
        self.assertEqual(response.headers['Access-Control-Allow-Origin'], FRONTEND)
        self.assertIn('no-store', response.headers['Cache-Control'])

    async def test_cross_origin_access_does_not_bypass_telegram_signature(self):
        response = await self.client.get('/api/my-leads', headers={'Origin': FRONTEND, 'X-Telegram-Init-Data': 'forged'})
        self.assertEqual(response.status, 401)
        self.assertEqual(response.headers['Access-Control-Allow-Origin'], FRONTEND)
        self.bot.send_message.assert_not_awaited()

    async def test_signed_submission_uses_existing_store_and_notifications_once(self):
        payload = {'service_name': 'Test', 'task_description': 'Synthetic task', 'draft_id': 'cdn-test'}
        headers = {'Origin': FRONTEND, 'X-Telegram-Init-Data': self.signed()}
        first = await self.client.post('/api/leads', json=payload, headers=headers)
        self.assertEqual(first.status, 200)
        second = await self.client.post('/api/leads', json=payload, headers=headers)
        self.assertEqual(second.status, 200)
        self.assertEqual((await first.json())['lead_id'], (await second.json())['lead_id'])
        self.assertEqual(len(await content_store.list_leads_by_user(17)), 1)

    async def test_private_content_policy_is_preserved_cross_origin(self):
        with patch.object(config, 'PRIVATE_MINIAPP_CONTENT', True):
            response = await self.client.get('/data/portfolio.json', headers={'Origin': FRONTEND})
        self.assertEqual(response.status, 401)
        self.assertEqual(response.headers['Access-Control-Allow-Origin'], FRONTEND)

    async def test_webhook_and_cdn_addresses_are_separate(self):
        self.assertEqual(config.miniapp_url(), FRONTEND)
        self.assertEqual(config.WEBAPP_URL, 'https://backend.example')
        with patch.object(config, 'MINIAPP_URL', ''):
            self.assertEqual(config.miniapp_url(), 'https://backend.example')

    async def test_draft_bridge_is_origin_bound_and_never_contains_stored_client_data(self):
        response = await self.client.get('/draft-bridge')
        self.assertEqual(response.status, 200)
        body = await response.text()
        self.assertIn('event.source !== parent', body)
        self.assertIn('event.origin !== allowed', body)
        self.assertIn(FRONTEND, body)
        self.assertIn(FRONTEND, response.headers['Content-Security-Policy'])
        self.assertEqual(response.headers['Cache-Control'], 'no-store')
        self.bot.send_message.assert_not_awaited()
        with patch.object(config, 'MINIAPP_URL', ''):
            self.assertEqual((await self.client.get('/draft-bridge')).status, 404)


class StaticExportTests(unittest.TestCase):
    def test_export_contains_only_web_assets_with_launch_routes_and_no_private_data(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / 'static-dist'
            build(output, 'https://backend.example', 'a' * 40)
            self.assertFalse((output / 'data').exists())
            self.assertFalse((output / '.env').exists())
            self.assertFalse((output / 'bot').exists())
            self.assertIn('https://backend.example', (output / 'js/runtime-config.js').read_text())
            html = (output / 'index.html').read_text()
            self.assertIn('boot-screen', html)
            self.assertIn('?v=' + 'a' * 40, html)
            for route in ROUTES:
                self.assertEqual((output / route / 'index.html').read_text(), html)
            self.assertEqual(json.loads((output / 'frontend-version.json').read_text())['revision'], 'a' * 40)

    def test_export_rejects_invalid_origins_and_dangerous_output_paths(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / 'static-dist'
            for origin in ('http://backend.example', 'https://backend.example/path', 'https://user:secret@backend.example'):
                with self.assertRaises(ValueError):
                    build(output, origin)
            with self.assertRaises(ValueError):
                build(ROOT, 'https://backend.example')
