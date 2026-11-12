"""Real router coverage for inventory-first owner navigation."""
from unittest.mock import AsyncMock, patch
import test_case_admin_routes
from bot.handlers import admin
from bot.states import AdminStates


class ContentLists(test_case_admin_routes.CaseAdminRoutes):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.categories = [{"id": f"cat{i}", "label": f"Category {i}"} for i in range(19)]
        for name, value in [
            ("list_portfolio_types", self.categories), ("list_services", [{"id": "s1", "name": "Landing"}]),
            ("list_faq", [{"id": 1, "question": "Question?", "answer": "Answer"}]),
            ("get_service", {"id": "s1", "name": "Landing"}), ("count_cases_with_type", 0), ("begin_pricing_edit", True),
        ]:
            p = patch.object(admin.content_store, name, AsyncMock(return_value=value))
            p.start()
            self.addCleanup(p.stop)

    def markup(self, cb):
        return cb.message.edit_text.await_args.kwargs["reply_markup"]

    async def test_categories_all_reachable_with_pagination(self):
        seen = []
        for data in ["adminmenu:categories", "adminlist:categories:1", "adminlist:categories:2"]:
            cb = await self.route(data)
            seen.extend(b.callback_data for row in self.markup(cb).inline_keyboard for b in row if b.callback_data.startswith("admincategory:"))
        self.assertEqual(seen, [f"admincategory:cat{i}" for i in range(19)])
        cb = await self.route("adminlist:categories:999")
        self.assertIn("admincategory:cat18", str(self.markup(cb)))

    async def test_selected_category_actions_keep_target(self):
        await self.state.set_state(AdminStates.add_case_title)
        cb = await self.route("admincategory:cat3")
        self.assertIsNone(await self.state.get_state())
        self.assertIn("admincategoryrename:cat3", str(self.markup(cb)))
        await self.route("admincategoryrename:cat3")
        self.assertEqual((await self.state.get_data())["type_id"], "cat3")
        self.assertEqual(await self.state.get_state(), AdminStates.rename_category_value.state)
        await self.route("admincategoryservice:cat3")
        self.assertEqual(await self.state.get_state(), AdminStates.category_related_service_pick.state)
        with patch.object(admin.content_store, "update_portfolio_type_related_service", AsyncMock()) as save:
            await self.route("admincatrelservice:s1")
            save.assert_awaited_once_with(777, "cat3", "s1")
        with patch.object(admin.content_store, "delete_portfolio_type", AsyncMock()) as delete:
            await self.route("admincategorydelete:cat3")
            delete.assert_not_awaited()
            await self.route("admindelcatconfirm:no")
            delete.assert_not_awaited()

    async def test_other_sections_show_records_and_open_editors(self):
        for section, target in [("cases", "adminmoderatecase:a"), ("faq", "adminfaqopen:1"), ("pricing", "adminserviceopen:s1")]:
            cb = await self.route(f"adminmenu:{section}")
            self.assertIn(target, str(self.markup(cb)))
        await self.route("adminfaqopen:1")
        self.assertEqual((await self.state.get_data())["cancel_to"], "faq")
        await self.route("admineditfaqfield:done")
        await self.route("adminserviceopen:s1")
        self.assertEqual((await self.state.get_data())["service_id"], "s1")
        cb = await self.route("admineditservicefield:done")
        self.assertIn("adminserviceopen:s1", str(self.markup(cb)))

    async def test_empty_and_deleted_category(self):
        self.categories.clear()
        cb = await self.route("adminmenu:categories")
        self.assertIn("Пока ничего нет", cb.message.edit_text.await_args.args[0])
        cb = await self.route("admincategory:cat3")
        self.assertIn("admincataction:add", str(self.markup(cb)))
