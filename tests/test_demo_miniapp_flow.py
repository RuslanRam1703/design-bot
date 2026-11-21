"""Acceptance scenarios with real HTTP handlers, demo content and isolated storage."""
import asyncio
import json
from pathlib import Path
import shutil
import tempfile
import time
import unittest
from unittest.mock import AsyncMock, patch

from aiohttp.test_utils import TestClient, TestServer

from bot import config, content_store, webserver
from tests.test_regression import _sign_init_data


ROOT = Path(__file__).resolve().parents[1]
TOKEN = "123456:test-token-not-real"
OWNER = 777
CLIENT = 42


class DemoMiniAppFlowTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.data_dir = Path(temp.name)
        for name in content_store.DATA_FILENAMES:
            shutil.copy(ROOT / "data" / name, self.data_dir / name)
        (self.data_dir / "leads.json").write_text('{"leads":[]}', encoding="utf-8")
        for change in (
            patch.object(content_store, "DATA_DIR", self.data_dir),
            patch.multiple(
                config, BOT_TOKEN=TOKEN, DESIGNER_CHAT_ID=str(OWNER),
                UPSTASH_REDIS_REST_URL="", UPSTASH_REDIS_REST_TOKEN="", PRIVATE_MINIAPP_CONTENT=True,
            ),
        ):
            change.start()
            self.addCleanup(change.stop)
        self.bot = AsyncMock()
        self.app = webserver.create_app(self.bot)
        self.http = await self.enterAsyncContext(TestClient(TestServer(self.app)))

    @staticmethod
    def init_data(user):
        return _sign_init_data({
            "auth_date": str(int(time.time())),
            "user": json.dumps({"id": user, "first_name": "Demo"}),
        }, TOKEN)

    def headers(self, user):
        return {"X-Telegram-Init-Data": self.init_data(user)}

    async def get_json(self, path, user, status=200):
        async with self.http.get(path, headers=self.headers(user)) as response:
            self.assertEqual(response.status, status)
            return await response.json()

    async def post_json(self, path, user, payload, status=200):
        async with self.http.post(path, headers=self.headers(user), json=payload) as response:
            self.assertEqual(response.status, status, await response.text())
            return await response.json()

    async def test_demo_case_to_client_calculation_and_request_history(self):
        node = shutil.which("node")
        if not node:
            self.skipTest("Node.js is required for the Mini App client scenario")
        process = await asyncio.create_subprocess_exec(
            node, str(ROOT / "tests" / "demo_miniapp_client.cjs"),
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        args = {"origin": str(self.http.make_url("/")), "initData": self.init_data(CLIENT)}
        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(json.dumps(args).encode()), 30)
        finally:
            if process.returncode is None:
                process.kill()
                await process.wait()
        self.assertEqual(process.returncode, 0, stderr.decode())
        data = json.loads(stdout)
        lead_id = data["result"]["lead_id"]
        [lead] = await self.get_json("/api/my-leads", CLIENT)
        self.assertEqual(lead["id"], lead_id)
        self.assertEqual(lead["payload"]["source_case_id"], "case_1")
        self.assertEqual(lead["calc_summary"]["term_from"], data["estimate"]["termFrom"])
        self.assertEqual(lead["calc_summary"]["term_to"], data["estimate"]["termTo"])
        self.assertEqual(await self.get_json("/api/my-leads", CLIENT + 1), [])
        supplement = {"mode": "supplement", "lead_id": lead_id, "request_id": "demo-retry",
                      "fields": {"comment": "Добавить меню завтраков"}}
        saved = await self.post_json("/api/leads", CLIENT, supplement)
        self.assertEqual(await self.post_json("/api/leads", CLIENT, supplement), saved)
        self.assertTrue(await content_store.update_lead_status(OWNER, lead_id, "IN_PROGRESS"))
        [updated] = await self.get_json("/api/my-leads", CLIENT)
        self.assertEqual(updated["status"], "IN_PROGRESS")
        self.assertEqual(len(updated["supplements"]), 1)
        await asyncio.gather(*self.app[webserver.LEAD_NOTICES])
        recipient_ids = {str(call.kwargs["chat_id"]) for call in self.bot.send_message.await_args_list}
        self.assertEqual(recipient_ids, {str(OWNER), str(CLIENT)})

    async def test_demo_gallery_draft_to_published_start_case(self):
        await content_store.ensure_gallery_demo_case()
        path = "/data/portfolio.json"
        preview_path = path + "?preview_case=gallery_demo"
        await self.get_json(preview_path, CLIENT, status=403)
        preview = await self.get_json(preview_path, OWNER)
        [case] = preview["cases"]
        newest_cover = case["images"][2]
        saved = await self.post_json(webserver.CASE_GALLERY_PATH, OWNER, {
            "case_id": case["id"], "revision": preview["revision"], "order": [2, 0, 1], "cover": newest_cover,
        })
        self.assertEqual(saved["case"]["images"][0], newest_cover)
        before = await self.get_json(path, CLIENT)
        self.assertEqual(next(c for c in before["cases"] if c["id"] == case["id"])["cover"], case["cover"])
        self.assertTrue(await content_store.finish_case_edit(OWNER, case["id"], saved["revision"], publish=True))
        self.assertTrue(await content_store.set_portfolio_start_case(OWNER, case["id"]))
        after = await self.get_json(path, CLIENT)
        self.assertEqual(after["start_case_id"], case["id"])
        published = next(c for c in after["cases"] if c["id"] == case["id"])
        self.assertEqual(published["cover"], newest_cover)
        self.assertEqual(published["images"], saved["case"]["images"])
