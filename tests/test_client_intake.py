"""Native Telegram customer entry tests; isolated local storage and API mocks."""
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from bot import content_store, flow, texts
from bot.handlers import client_intake, start
from bot.keyboards import client_home_keyboard
from test_regression import make_state, make_flow_message, make_callback


def callback(action, chat_id=31):
    cb = make_callback(action, chat_id=chat_id, message_id=777)
    cb.from_user = SimpleNamespace(id=chat_id, username="testclient", first_name="Тестовый", last_name=None)
    return cb


class NativeIntakeTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old_dir = content_store.DATA_DIR
        content_store.DATA_DIR = Path(self.tmp.name)

    def tearDown(self):
        content_store.DATA_DIR = self.old_dir
        self.tmp.cleanup()

    async def test_native_menu_has_all_entry_points(self):
        kb = client_home_keyboard("https://example.invalid")
        buttons = [b for row in kb.inline_keyboard for b in row]
        self.assertIn("intake:question", [b.callback_data for b in buttons])
        self.assertIn("intake:quick", [b.callback_data for b in buttons])
        urls = [b.web_app.url for b in buttons if b.web_app]
        self.assertEqual(len(urls), 4)
        self.assertIn("https://example.invalid/brief", urls)
        self.assertIn("https://example.invalid/myleads", urls)

    async def test_start_retains_nav_and_creates_inline_home(self):
        state = make_state(31)
        msg = make_flow_message(chat_id=31, text="/start")
        await start.cmd_start(msg, state)
        self.assertEqual(msg.answer.await_count, 2)
        self.assertEqual(msg.answer.await_args.args[0], texts.CLIENT_HOME)
        data = await state.get_data()
        self.assertIn(flow._NAV_ANCHOR_MSG_KEY, data)
        self.assertIn(flow._ANCHOR_MSG_KEY, data)

    async def test_question_reuses_relay_without_creating_lead(self):
        state = make_state(31)
        with patch("bot.handlers.client_intake.config.DESIGNER_CHAT_ID", "777"), \
             patch("bot.handlers.start.relay_client_text_to_designer", new_callable=AsyncMock) as relay:
            await client_intake.question_start(callback("intake:question"), state)
            self.assertEqual(await state.get_state(), client_intake.QuestionStates.awaiting_question.state)
            msg = make_flow_message(chat_id=31, text="Можете сделать фирменный стиль?")
            await client_intake.question_receive(msg, state)
            relay.assert_awaited_once()
        self.assertIsNone(await state.get_state())
        self.assertEqual(await content_store.list_leads_by_user(31), [])

    async def test_quick_submit_creates_one_lead_even_on_repeat(self):
        state = make_state(31)
        await client_intake.quick_start(callback("intake:quick"), state)
        await client_intake.quick_task_receive(make_flow_message(chat_id=31, text="Нужен лендинг для студии дизайна"), state)
        self.assertEqual(await state.get_state(), client_intake.QuickLeadStates.confirmation.state)
        with patch("bot.handlers.client_intake.notify_designer_lead", new_callable=AsyncMock) as notify:
            cb = callback("intake:submit")
            await client_intake.quick_submit(cb, state)
            notify.assert_awaited_once()
            await client_intake.quick_submit(cb, state)
            self.assertIn("уже отправлена", cb.answer.await_args.args[0])
        stored = await content_store.list_leads_by_user(31)
        self.assertEqual(len(stored), 1)
        self.assertEqual(stored[0]["payload"]["source"], "quick")
        self.assertFalse(stored[0]["payload"]["attach_tz"])

    async def test_short_task_is_not_accepted(self):
        state = make_state(31)
        await client_intake.quick_start(callback("intake:quick"), state)
        await client_intake.quick_task_receive(make_flow_message(chat_id=31, text="мало"), state)
        self.assertEqual(await state.get_state(), client_intake.QuickLeadStates.awaiting_task.state)
        self.assertEqual(await content_store.list_leads_by_user(31), [])

    async def test_custom_contact_can_be_entered_and_cancelled(self):
        state = make_state(31)
        await client_intake.quick_start(callback("intake:quick"), state)
        await client_intake.quick_task_receive(make_flow_message(chat_id=31, text="Нужен сайт для студии"), state)
        await client_intake.quick_contact_start(callback("intake:contact"), state)
        await client_intake.quick_contact_receive(make_flow_message(chat_id=31, text="+7 999 000 00 00"), state)
        self.assertEqual((await state.get_data())["quick_contact"], "+7 999 000 00 00")
        await client_intake.intake_cancel(callback("intake:cancel"), state)
        self.assertIsNone(await state.get_state())
        self.assertEqual(await content_store.list_leads_by_user(31), [])

    async def test_relay_preserves_literal_angle_brackets_and_ampersands(self):
        from test_regression import make_message
        msg = make_message(chat_id=1)
        msg.text = "Можно дизайн <сайта> & фирменного стиля?"
        with patch("bot.handlers.start.config.DESIGNER_CHAT_ID", "777"):
            await start.relay_client_text_to_designer(msg, make_state(1))
        msg.bot.send_message.assert_awaited_once()
        kwargs = msg.bot.send_message.await_args.kwargs
        self.assertIn("<сайта> & фирменного стиля", kwargs["text"])
        self.assertIsNone(kwargs["parse_mode"])

    async def test_maximal_escaped_input_stays_under_telegram_limit(self):
        from bot.handlers.client_intake import _TASK_MAX, _CONTACT_MAX
        text = client_intake._preview("'" * _TASK_MAX, "'" * _CONTACT_MAX)
        self.assertLess(len(text), 4096)

    async def test_preview_escapes_user_html(self):
        text = client_intake._preview("Лендинг <b> & дизайн", "name<broken")
        self.assertIn("&lt;b&gt; &amp;", text)
        self.assertIn("&lt;broken", text)
        self.assertNotIn("<b>", text)


if __name__ == "__main__":
    unittest.main()
