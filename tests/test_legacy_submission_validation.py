"""Legacy Telegram sendData must obey the HTTP submission input boundary."""
import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from bot.handlers import webapp


class LegacySubmissionValidationTests(unittest.IsolatedAsyncioTestCase):
    def message(self, payload):
        return SimpleNamespace(web_app_data=SimpleNamespace(data=json.dumps(payload)),
            answer=AsyncMock(), bot=SimpleNamespace(send_message=AsyncMock()),
            from_user=SimpleNamespace(id=42, username=None, first_name='Test', last_name=None))

    async def test_malformed_payloads_do_not_persist_or_notify(self):
        for payload in ([], None, 1, 'text',
                        {'action': 'submit_brief'},
                        {'action': 'submit_brief', 'service_name': 'Test', 'calc': []},
                        {'action': 'submit_brief', 'service_name': 'Test', 'calc': {'service_id': 'landing', 'options': [{'id': 'x', 'qty': 'bad'}]}}):
            with self.subTest(payload=payload):
                message = self.message(payload)
                with patch.object(webapp.content_store, 'add_lead', AsyncMock()) as save, \
                     patch.object(webapp, 'notify_designer_lead', AsyncMock()) as notify:
                    await webapp.handle_webapp_data(message)
                save.assert_not_awaited()
                notify.assert_not_awaited()
                message.answer.assert_awaited_once()

    async def test_foreign_draft_is_rejected_without_webhook_retry_or_notification(self):
        message = self.message({'action': 'submit_brief', 'service_name': 'Test', 'draft_id': 'foreign'})
        with patch.object(webapp.content_store, 'add_lead', AsyncMock(side_effect=webapp.content_store.NotLeadOwnerError())), \
             patch.object(webapp, 'notify_designer_lead', AsyncMock()) as notify:
            await webapp.handle_webapp_data(message)
        notify.assert_not_awaited()
        message.answer.assert_awaited_once()
