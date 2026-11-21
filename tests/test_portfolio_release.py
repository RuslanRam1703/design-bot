import asyncio
import copy
import unittest
from unittest.mock import AsyncMock, patch

from bot import content_store, webserver


class PortfolioReleaseTests(unittest.IsolatedAsyncioTestCase):
    async def test_start_case_accepts_only_published_and_clears_on_unpublish(self):
        saved = {"cases": [{"id": "live"}, {"id": "draft", "status": "draft"}]}

        async def read(_):
            return copy.deepcopy(saved)

        async def write(_, data):
            saved.update(data)
            for key in saved.keys() - data.keys():
                del saved[key]

        with (
            patch.object(content_store, "_upstash_enabled", return_value=False),
            patch.object(content_store, "_read", side_effect=read),
            patch.object(content_store, "_write", side_effect=write),
        ):
            actor = content_store.config.DESIGNER_CHAT_ID
            self.assertFalse(await content_store.set_portfolio_start_case(actor, "draft"))
            self.assertTrue(await content_store.set_portfolio_start_case(actor, "live"))
            self.assertEqual(saved["start_case_id"], "live")
            await content_store.set_case_status(actor, "live", "unpublished")
            self.assertNotIn("start_case_id", saved)

    async def test_saved_lead_returns_before_telegram_responds(self):
        started = asyncio.Event()
        complete = asyncio.Event()
        tasks = set()

        async def notify(*_args, **_kwargs):
            started.set()
            await complete.wait()

        saved = {"id": 42, "created": True}
        with (
            patch.object(content_store, "add_lead", AsyncMock(return_value=saved)),
            patch.object(webserver, "notify_designer_lead", side_effect=notify),
            patch.object(webserver, "_notify_client", side_effect=notify),
        ):
            try:
                response = await asyncio.wait_for(
                    webserver._handle_lead_create({}, {"user_id": 10}, AsyncMock(), tasks), 1
                )
                await asyncio.wait_for(started.wait(), 1)
                self.assertEqual(response.status, 200)
                self.assertIn('"lead_id": 42', response.text)
                self.assertEqual(len(tasks), 2)
            finally:
                complete.set()
                await asyncio.gather(*tasks)
