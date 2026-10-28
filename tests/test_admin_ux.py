"""Admin navigation UX: correct grouping without changing storage or API."""
import unittest

from bot import admin_keyboards as kb, flow
from bot.handlers import admin
from test_regression import make_callback, make_state


class AdminNavigationUXTests(unittest.IsolatedAsyncioTestCase):
    def test_root_prioritizes_inbox_and_groups_content(self):
        root = kb.admin_root_keyboard()
        callbacks = [[button.callback_data for button in row] for row in root.inline_keyboard]
        self.assertEqual(callbacks[0], ["adminmenu:leads"])
        self.assertIn("adminmenu:content", callbacks[1])
        self.assertIn("adminmenu:pricing", callbacks[1])
        self.assertEqual(callbacks[-1], ["adminmenu:settings"])
        content = [b.callback_data for row in kb.admin_content_keyboard().inline_keyboard for b in row]
        self.assertTrue({"adminmenu:cases", "adminmenu:faq", "adminmenu:about", "adminmenu:categories"}.issubset(content))
        settings = [b.callback_data for row in kb.admin_settings_keyboard().inline_keyboard for b in row]
        self.assertIn("adminmenu:nav", settings)
        self.assertIn("adminmenu:backup", settings)

    def test_section_back_buttons_return_to_parent(self):
        for keyboard, expected in (
            (kb.admin_cases_menu_keyboard(), "adminmenu:content"),
            (kb.admin_faq_menu_keyboard(), "adminmenu:content"),
            (kb.categories_menu_keyboard(), "adminmenu:content"),
            (kb.backup_menu_keyboard(), "adminmenu:settings"),
            (kb.nav_menu_keyboard({"menu": {}}), "adminmenu:settings"),
        ):
            self.assertEqual(keyboard.inline_keyboard[-1][0].callback_data, expected)

    async def test_group_navigation_edits_one_screen_and_preserves_nav(self):
        for handler, data in ((admin.menu_content, "adminmenu:content"),
                              (admin.menu_settings, "adminmenu:settings")):
            with self.subTest(data=data):
                state = make_state(777)
                await state.update_data(**{
                    flow._NAV_ANCHOR_MSG_KEY: 111,
                    flow._NAV_ANCHOR_CHAT_KEY: 777,
                })
                cb = make_callback(data, chat_id=777, message_id=555)
                await handler(cb, state)
                cb.message.edit_text.assert_awaited_once()
                self.assertEqual((await state.get_data())[flow._NAV_ANCHOR_MSG_KEY], 111)
                self.assertEqual((await state.get_data())[flow._ANCHOR_MSG_KEY], 555)
                cb.answer.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
