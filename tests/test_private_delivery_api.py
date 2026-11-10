"""Private archive routing identifiers never reach the customer API."""
import unittest
from unittest.mock import AsyncMock, patch
from aiohttp.test_utils import TestClient, TestServer
from bot import webserver


class PrivateDeliveryApiTests(unittest.IsolatedAsyncioTestCase):
    async def test_internal_delivery_metadata_is_removed(self):
        lead = {'id': 7, 'archive_deliveries': [{'key': 'private-routing-key', 'archive_chat_id': -100123}],
                'owner_messages': [{'id': 1, 'text': 'Visible answer', 'sent_at': '2026-11-11',
                    'delivery_status': 'sent', 'source_key': 'private-source', 'recipient_id': 17}]}
        with patch.object(webserver, 'validate_init_data', return_value={'id': 17}), \
             patch.object(webserver.access_control, 'is_blocked', AsyncMock(return_value=False)), \
             patch.object(webserver.content_store, 'list_leads_by_user', AsyncMock(return_value=[lead])):
            async with TestClient(TestServer(webserver.create_app(None))) as client:
                response = await client.get('/api/my-leads')
                body = await response.json()
        self.assertEqual(response.status, 200)
        self.assertNotIn('archive_deliveries', body[0])
        self.assertEqual(set(body[0]['owner_messages'][0]), {'id', 'text', 'sent_at', 'delivery_status'})
        self.assertEqual(body[0]['owner_messages'][0]['text'], 'Visible answer')
        self.assertIn('archive_deliveries', lead)  # response filtering never mutates stored data
