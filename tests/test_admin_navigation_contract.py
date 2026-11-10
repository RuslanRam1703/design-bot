"""Admin navigation contract: all root buttons must have reachable callbacks."""
import ast
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]
KEYBOARDS = (ROOT / "bot/admin_keyboards.py").read_text(encoding="utf-8")
HANDLERS = "\n".join(
    (ROOT / name).read_text(encoding="utf-8")
    for name in ("bot/handlers/admin.py", "bot/handlers/admin_access.py",
                 "bot/handlers/admin_projects.py")
)
START = (ROOT / "bot/handlers/start.py").read_text(encoding="utf-8")
MAIN = (ROOT / "bot/main.py").read_text(encoding="utf-8")


class AdminNavigationContractTests(unittest.TestCase):
    def test_root_navigation_callbacks_are_implemented(self):
        expected = (
            "adminmenu:leads", "adminmenu:projects", "adminmenu:content",
            "adminmenu:pricing", "access:root", "adminmenu:settings",
        )
        for callback in expected:
            with self.subTest(callback=callback):
                self.assertIn('callback_data="' + callback + '"', KEYBOARDS)
                self.assertIn('F.data == "' + callback + '"', HANDLERS)

    def test_admin_entry_points_exist(self):
        self.assertIn('@router.message(Command("admin"))', HANDLERS)
        self.assertIn('@router.message(F.text == texts.ADMIN_BUTTON)', HANDLERS)
        self.assertIn('await cmd_admin(message, state)', HANDLERS)
        self.assertIn('router.message.filter(_is_designer_message)', HANDLERS)

    def test_admin_handlers_precede_start_fallback(self):
        self.assertLess(MAIN.index('dp.include_router(admin.router)'),
                        MAIN.index('dp.include_router(start.router)'))

    def test_navigation_has_escape_routes(self):
        self.assertIn('@router.callback_query(F.data == "admincancel")', HANDLERS)
        self.assertIn('@router.message(F.text == texts.MAIN_MENU_BUTTON)', HANDLERS)
        self.assertIn('def main_menu_or_confirm(', START)


if __name__ == "__main__":
    unittest.main()
