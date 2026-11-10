"""Archive setup command must be private and reachable."""
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class ArchiveSetupContractTests(unittest.TestCase):
    def test_registered_before_catch_all(self):
        main = (ROOT / "bot/main.py").read_text(encoding="utf-8")
        self.assertLess(main.index("dp.include_router(archive_setup.router)"),
                        main.index("dp.include_router(start.router)"))

    def test_owner_and_private_group_checks(self):
        handler = (ROOT / "bot/handlers/archive_setup.py").read_text(encoding="utf-8")
        self.assertIn('Command("archiveid")', handler)
        self.assertIn("message.from_user.id", handler)
        self.assertIn("config.DESIGNER_CHAT_ID", handler)
        self.assertIn('("group", "supergroup")', handler)
        self.assertIn("CLIENT_MATERIALS_CHANNEL", handler)


if __name__ == "__main__":
    unittest.main()
