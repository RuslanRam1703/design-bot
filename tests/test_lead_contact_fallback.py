"""Admin lead contact actions for customers without public usernames."""
import unittest
from bot.admin_keyboards import lead_detail_keyboard


class LeadContactTests(unittest.TestCase):
    def _buttons(self, username=None):
        lead = {
            "id": 9, "status": "NEW", "materials": [],
            "telegram": {"user_id": 123456789, "username": username},
        }
        keyboard = lead_detail_keyboard(lead)
        return [button for row in keyboard.inline_keyboard for button in row]

    def test_no_username_keeps_bot_reply_and_id_link(self):
        buttons = self._buttons()
        self.assertTrue(any(b.callback_data == "adminleadaction:reply" for b in buttons))
        self.assertTrue(any(b.url == "tg://user?id=123456789" for b in buttons))

    def test_username_prefers_public_profile(self):
        buttons = self._buttons("example_user")
        self.assertTrue(any(b.url == "https://t.me/example_user" for b in buttons))
        self.assertFalse(any(b.url and b.url.startswith("tg://user?") for b in buttons))


if __name__ == "__main__":
    unittest.main()
