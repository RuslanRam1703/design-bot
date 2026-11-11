import unittest
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, patch
from aiogram.exceptions import TelegramBadRequest
from aiogram.methods import DeleteMessage
from bot import flow
from bot.handlers import admin


class AttachmentNavigationTests(unittest.IsolatedAsyncioTestCase):
    def setup_panel(self):
        self.events = []
        async def document(*args, **kwargs): self.events.append('file')
        async def answer(*args, **kwargs):
            self.events.append('panel')
            return NS(message_id=20, chat=NS(id=777))
        async def delete(): self.events.append('delete_old')
        self.message = NS(chat=NS(id=777), message_id=10, answer_document=AsyncMock(side_effect=document),
                          answer=AsyncMock(side_effect=answer), delete=AsyncMock(side_effect=delete),
                          edit_text=AsyncMock(), edit_reply_markup=AsyncMock())
        self.cb = NS(message=self.message, answer=AsyncMock())
        self.state = NS(update_data=AsyncMock())

    async def test_clean_start_file_then_confirmation_and_new_anchor(self):
        self.setup_panel()
        with patch.object(admin.owner_maintenance, 'prepare_clean_start', AsyncMock(return_value=(b'zip', 'digest', {'Кейсы': 2}))):
            await admin.clean_start_prepare(self.cb, self.state)
        self.assertEqual(self.events, ['file', 'panel', 'delete_old'])
        self.state.update_data.assert_any_await(_flow_msg_id=20, _flow_chat_id=777)
        self.state.update_data.assert_any_await(clean_start_digest='digest')
        self.message.edit_text.assert_not_awaited()
        self.assertIn('adminclean:confirm:digest', str(self.message.answer.await_args.kwargs['reply_markup']))

    async def test_export_also_moves_controls_below_file(self):
        self.setup_panel()
        with patch.object(admin.content_store, 'export_backup_bytes', AsyncMock(return_value=b'zip')):
            await admin.backup_export(self.cb, self.state)
        self.assertEqual(self.events, ['file', 'panel', 'delete_old'])
        self.state.update_data.assert_awaited_once_with(_flow_msg_id=20, _flow_chat_id=777)

    async def test_failed_new_panel_retains_old_controls(self):
        self.setup_panel()
        self.message.answer.side_effect = RuntimeError('network')
        with self.assertRaises(RuntimeError): await flow.panel_after_attachment(self.cb, self.state, 'Next')
        self.message.delete.assert_not_awaited()
        self.state.update_data.assert_not_awaited()

    async def test_undeletable_old_panel_loses_keyboard(self):
        self.setup_panel()
        self.message.delete.side_effect = TelegramBadRequest(method=DeleteMessage(chat_id=777, message_id=10), message="message can't be deleted")
        await flow.panel_after_attachment(self.cb, self.state, 'Next')
        self.message.edit_reply_markup.assert_awaited_once_with(reply_markup=None)
        self.state.update_data.assert_awaited_once_with(_flow_msg_id=20, _flow_chat_id=777)
