"""Exercise real callback routing, not just direct handler invocation."""
import unittest
from unittest.mock import AsyncMock, patch
from aiogram.dispatcher.event.bases import UNHANDLED
from bot.handlers import admin
from bot.states import AdminStates
from test_regression import make_callback, make_state


class CaseAdminRoutes(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.state=make_state(777)
        self.cases=[{'id':'a','title':'Test','type':'site','status':'draft'}, {'id':'b','title':'Trash','type':'site','status':'trashed'}]
        for p in [patch.object(admin.config,'DESIGNER_CHAT_ID','777'),patch.object(admin.content_store,'list_cases',AsyncMock(side_effect=lambda:self.cases))]:
            p.start();self.addCleanup(p.stop)

    async def route(self, data):
        cb=make_callback(data,777)
        result=await admin.router.propagate_event('callback_query',cb,state=self.state,raw_state=await self.state.get_state())
        self.assertIsNot(result,UNHANDLED,data)
        return cb

    async def test_publication_list_opens_from_stale_edit_state_and_card_links_editor(self):
        await self.state.set_state(AdminStates.add_case_title)
        cb=await self.route('admincasesaction:moderate')
        self.assertIsNone(await self.state.get_state())
        markup=str(cb.message.edit_text.await_args.kwargs['reply_markup'])
        self.assertIn('adminmoderatecase:a',markup)
        self.assertNotIn('adminmoderatecase:b',markup)
        cb=await self.route('adminmoderatecase:a')
        self.assertIn('admincaseedit:a',str(cb.message.edit_text.await_args.kwargs['reply_markup']))
        with patch.object(admin.content_store,'begin_case_edit',AsyncMock(return_value=True)):
            await self.route('admincaseedit:a')
        self.assertEqual(await self.state.get_state(),AdminStates.edit_case_field_pick.state)

    async def test_card_trash_uses_existing_confirm_and_cancel_does_not_write(self):
        with patch.object(admin.content_store,'set_case_status',AsyncMock()) as write:
            cb=await self.route('admincardtrash:a')
            self.assertIn('admindelcaseconfirm:yes',str(cb.message.edit_text.await_args.kwargs['reply_markup']))
            write.assert_not_awaited()
            await self.route('admindelcaseconfirm:no')
            write.assert_not_awaited()

    async def test_same_trash_card_from_both_entries(self):
        await self.route('admincasesaction:trash')
        a=await self.route('admintrashcase:b')
        b=await self.route('adminmoderatecase:b')
        self.assertEqual(a.message.edit_text.await_args,b.message.edit_text.await_args)
        markup=str(a.message.edit_text.await_args.kwargs['reply_markup'])
        self.assertIn('adminpurgeask:b',markup)
        self.assertNotIn('adminsetcasestatus:published',markup)

    async def test_edit_list_excludes_trash_and_stale_card_offers_menu(self):
        cb=await self.route('admincasesaction:edit')
        self.assertNotIn('admineditcase:b',str(cb.message.edit_text.await_args.kwargs['reply_markup']))
        cb=await self.route('adminmoderatecase:missing')
        self.assertIn('Кейс уже удалён',cb.message.edit_text.await_args.args[0])
