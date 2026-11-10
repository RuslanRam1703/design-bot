"""Static runtime contract tests for the Render webhook migration."""
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]
MAIN = (ROOT / "bot" / "main.py").read_text(encoding="utf-8")


class WebhookRuntimeTests(unittest.TestCase):
    def test_webhook_is_registered_with_secret_and_keeps_updates(self):
        self.assertIn('app.router.add_post("/telegram/webhook", telegram_webhook)', MAIN)
        self.assertIn('X-Telegram-Bot-Api-Secret-Token', MAIN)
        self.assertIn('hmac.compare_digest(supplied, webhook_secret)', MAIN)
        self.assertIn('drop_pending_updates=False', MAIN)
        self.assertIn('allowed_updates=dp.resolve_used_update_types()', MAIN)

    def test_no_polling_or_webhook_deletion(self):
        self.assertNotIn('dp.start_polling(', MAIN)
        self.assertNotIn('bot.delete_webhook(', MAIN)

    def test_same_dispatcher_handles_webhook(self):
        self.assertIn('await dp.feed_update(bot, update)', MAIN)
        self.assertIn('await content_store.ensure_storage_initialized()', MAIN)


if __name__ == "__main__":
    unittest.main()
