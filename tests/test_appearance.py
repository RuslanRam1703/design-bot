import copy
import unittest
from unittest.mock import AsyncMock, patch
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from bot import config, content_store as store, webserver, flow
from bot.handlers import admin, start
from bot.states import AdminStates
from test_regression import make_state, make_flow_message_factory, make_callback


class AppearanceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.data = {"menu": {"portfolio": True}}
        async def read(_): return copy.deepcopy(self.data)
        async def write(_, data): self.data = copy.deepcopy(data)
        for p in [patch.object(store, "_read", side_effect=read),
                  patch.object(store, "_write", side_effect=write),
                  patch.object(store, "_upstash_enabled", return_value=False)]:
            p.start(); self.addCleanup(p.stop)
        self.actor = config.DESIGNER_CHAT_ID

    async def digest(self):
        return store.editor_digest(await store.appearance_settings(draft=True))[:24]

    async def test_draft_apply_discard_and_stale_preview(self):
        await store.stage_appearance(self.actor, "hero_title", "My work")
        self.assertNotEqual((await store.appearance_settings())["hero_title"], "My work")
        digest = await self.digest()
        await store.stage_appearance(self.actor, "hero_title", "Changed")
        with self.assertRaises(ValueError):
            await store.finish_appearance(self.actor, apply=True, expected_digest=digest)
        await store.finish_appearance(self.actor, apply=True, expected_digest=await self.digest())
        self.assertEqual((await store.appearance_settings())["hero_title"], "Changed")
        await store.stage_appearance(self.actor, "hero_visible", False)
        await store.finish_appearance(self.actor, apply=False, expected_digest=await self.digest())
        self.assertTrue((await store.appearance_settings())["hero_visible"])
        self.assertEqual(self.data["menu"], {"portfolio": True})

    async def test_limits_and_authorization(self):
        for key, value in [("hero_title", ""), ("hero_button", "x"*33), ("hero_visible", "false"), ("unknown", "text")]:
            with self.assertRaises(ValueError): await store.stage_appearance(self.actor, key, value)
        with self.assertRaises(store.NotDesignerError):
            await store.stage_appearance("other", "hero_title", "No")

    async def test_public_endpoint_hides_draft_and_preview_requires_owner(self):
        await store.stage_appearance(self.actor, "hero_title", "Private draft")
        app = web.Application(); app.router.add_get("/data/{filename}", webserver.handle_public_data)
        with patch.object(config, "PRIVATE_MINIAPP_CONTENT", False):
            async with TestClient(TestServer(app)) as client:
                response = await client.get("/data/ui_config.json")
                self.assertNotIn("Private draft", await response.text())
                response = await client.get("/data/ui_config.json?preview_appearance=1")
                self.assertEqual(response.status, 403)
                for user_id, expected in [(int(self.actor), 200), (999999, 403)]:
                    with patch.object(webserver, "validate_init_data", return_value={"id": user_id}), patch.object(webserver.access_control, "is_blocked", AsyncMock(return_value=False)):
                        response = await client.get("/data/ui_config.json?preview_appearance=1", headers={"X-Telegram-Init-Data": "test"})
                        self.assertEqual(response.status, expected)
                        if expected == 200:
                            self.assertEqual((await response.json())["appearance"]["hero_title"], "Private draft")

    async def test_preview_callback_fits_telegram_and_cancel_returns_to_appearance(self):
        cb = make_callback("appearance:preview", int(self.actor))
        await admin.appearance_action(cb, make_state(int(self.actor)))
        markup = cb.message.edit_text.await_args.kwargs["reply_markup"]
        for row in markup.inline_keyboard:
            for button in row:
                if button.callback_data: self.assertLessEqual(len(button.callback_data.encode()), 64)
        text, _, _, _ = await admin._resolve_cancel({"cancel_to": "appearance"})
        self.assertIn("Оформление", text)


class MenuRecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_recovery_resends_keyboard_and_clears_stale_admin_state(self):
        state = make_state(777)
        await state.set_state(AdminStates.add_case_title)
        await state.update_data(**{flow._NAV_ANCHOR_MSG_KEY: 111, flow._NAV_ANCHOR_CHAT_KEY: 777})
        msg = make_flow_message_factory(chat_id=777, start_id=900)(text="/menu")
        await start.recover_menu(msg, state)
        self.assertIsNone(await state.get_state())
        keyboard = msg.answer.await_args_list[0].kwargs["reply_markup"]
        self.assertTrue(keyboard.is_persistent)
        msg.bot.delete_message.assert_any_await(chat_id=777, message_id=111)
        self.assertEqual((await state.get_data())[flow._NAV_ANCHOR_MSG_KEY], 901)

    async def test_recovery_router_accepts_start_during_admin_input(self):
        from aiogram.types import Message
        from aiogram.dispatcher.event.bases import UNHANDLED
        state = make_state(777)
        await state.set_state(AdminStates.add_case_title)
        for command in ("/start", "/menu"):
            msg = Message.model_validate({"message_id": 1, "date": 0,
                "chat": {"id": 777, "type": "private"},
                "from": {"id": 777, "is_bot": False, "first_name": "Owner"},
                "text": command, "entities": [{"type": "bot_command", "offset": 0, "length": len(command)}]})
            with patch.object(flow, "refresh_nav_keyboard", AsyncMock()) as refresh, patch.object(start, "cmd_start", AsyncMock()) as reset:
                result = await start.recovery_router.propagate_event("message", msg, bot=AsyncMock(), state=state, raw_state=await state.get_state())
                self.assertIsNot(result, UNHANDLED)
                refresh.assert_awaited_once()
                reset.assert_awaited_once()
