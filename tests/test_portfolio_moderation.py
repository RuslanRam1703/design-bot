"""Regression tests for moderation and Telegram import idempotency."""
import copy
import unittest
from unittest.mock import AsyncMock, patch

from bot import content_store


class PortfolioModerationTests(unittest.IsolatedAsyncioTestCase):
    async def test_import_starts_as_draft_and_repeated_edit_preserves_curated_content(self):
        data = {"cases": [], "types": [{"id": "graphics", "label": "Графика"}]}
        async def read(_):
            return copy.deepcopy(data)
        async def write(_, updated):
            data.clear()
            data.update(copy.deepcopy(updated))
        with patch.object(content_store, "_upstash_enabled", return_value=False), \
             patch.object(content_store, "_read", side_effect=read), \
             patch.object(content_store, "_write", side_effect=write):
            case, created = await content_store.upsert_channel_case(channel_username="portfolio_rrr17", message_id=6, title="Original", cover="old.jpg")
            self.assertTrue(created)
            self.assertEqual(case["status"], "draft")
            data["cases"][0]["title"] = "Manually edited"
            data["cases"][0]["status"] = "rejected"
            case, created = await content_store.upsert_channel_case(channel_username="portfolio_rrr17", message_id=6, title="Telegram edit", cover="new.jpg")
            self.assertFalse(created)
            self.assertEqual(len(data["cases"]), 1)
            self.assertEqual(case["title"], "Manually edited")
            self.assertEqual(case["cover"], "old.jpg")
            self.assertEqual(case["status"], "rejected")

    async def test_status_changes_preserve_content_and_allow_restore(self):
        data = {"cases": [{"id": "case_1", "title": "Work", "cover": "image.jpg"}], "types": []}
        async def read(_):
            return copy.deepcopy(data)
        async def write(_, updated):
            data.clear()
            data.update(copy.deepcopy(updated))
        with patch.object(content_store, "_upstash_enabled", return_value=False), \
             patch.object(content_store, "_require_designer"), \
             patch.object(content_store, "_read", side_effect=read), \
             patch.object(content_store, "_write", side_effect=write):
            self.assertTrue(await content_store.set_case_status(1, "case_1", "trashed"))
            self.assertEqual(data["cases"][0]["cover"], "image.jpg")
            self.assertTrue(await content_store.set_case_status(1, "case_1", "published"))
            self.assertEqual(data["cases"][0]["status"], "published")
            with self.assertRaises(ValueError):
                await content_store.set_case_status(1, "case_1", "unknown")


if __name__ == "__main__":
    unittest.main()
