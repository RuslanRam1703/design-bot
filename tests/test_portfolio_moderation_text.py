"""Channel titles remain literal text in Telegram HTML moderation screens."""
import unittest
from unittest.mock import AsyncMock, patch
from bot.handlers import admin
from test_regression import make_callback


class ModerationTextTests(unittest.IsolatedAsyncioTestCase):
    async def test_initial_and_updated_moderation_escape_source_title(self):
        case = {"id": "demo", "title": "<b>A & B</b>", "status": "draft"}
        with patch.object(admin.content_store, "list_cases", AsyncMock(return_value=[case])):
            initial = make_callback("adminmoderatecase:demo", chat_id=777)
            await admin.cases_moderate_pick(initial)
            updated = make_callback("adminmoderatecase:demo", chat_id=777)
            await admin.cases_moderate_pick_for_id(updated, "demo")
        for callback in (initial, updated):
            text = callback.message.edit_text.await_args.args[0]
            self.assertIn("&lt;b&gt;A &amp; B&lt;/b&gt;", text)
            self.assertNotIn("<b>A", text)
