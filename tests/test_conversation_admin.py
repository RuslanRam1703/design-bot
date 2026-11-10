"""Owner conversation navigation and media reopening use the existing lead."""
import unittest
from unittest.mock import AsyncMock, patch
from bot.handlers import admin
from bot.states import AdminStates
from test_regression import make_callback, make_state


class ConversationAdminTests(unittest.IsolatedAsyncioTestCase):
    async def test_history_is_plain_text_with_return_and_reply_actions(self):
        state = make_state(777)
        await state.update_data(lead_id=7)
        await state.set_state(AdminStates.lead_detail)
        cb = make_callback('adminleadhistory:0', chat_id=777)
        lead = {'id': 7, 'status': 'NEW', 'telegram': {'user_id': 55},
                'client_messages': [{'text': '<b>Raw & text', 'received_at': '2026-11-11'}]}
        with patch.object(admin.content_store, 'get_lead', AsyncMock(return_value=lead)):
            await admin.lead_history(cb, state)
        args = cb.message.edit_text.await_args
        self.assertIsNone(args.kwargs['parse_mode'])
        self.assertIn('<b>Raw & text', args.args[0])
        buttons = [b.callback_data for row in args.kwargs['reply_markup'].inline_keyboard for b in row]
        self.assertIn('adminleadaction:reply', buttons)
        self.assertIn('adminleadhistory:back', buttons)
        self.assertEqual(await state.get_state(), AdminStates.lead_detail.state)

    async def test_history_page_out_of_range_is_safe(self):
        state = make_state(777)
        await state.update_data(lead_id=7)
        cb = make_callback('adminleadhistory:999', chat_id=777)
        with patch.object(admin.content_store, 'get_lead', AsyncMock(return_value={'id': 7})):
            await admin.lead_history(cb, state)
        cb.message.edit_text.assert_not_awaited()
        cb.answer.assert_awaited_once()

    async def test_free_media_reopens_only_to_owner(self):
        state = make_state(777)
        await state.update_data(lead_id=7)
        cb = make_callback('adminmaterialsend:0', chat_id=777)
        cb.bot.send_document = AsyncMock()
        lead = {'id': 7, 'client_messages': [{'media': {'kind': 'document', 'file_id': 'synthetic-file'}}]}
        with patch.object(admin.content_store, 'get_lead', AsyncMock(return_value=lead)), patch.object(admin.config, 'DESIGNER_CHAT_ID', '777'):
            await admin.lead_material_resend(cb, state)
        cb.bot.send_document.assert_awaited_once_with(chat_id='777', document='synthetic-file')
