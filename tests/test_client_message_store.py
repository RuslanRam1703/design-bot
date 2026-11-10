"""Storage invariants for client messages on an active lead."""
import unittest
from unittest.mock import AsyncMock, patch
from bot import content_store


class ClientMessageStoreTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.leads = [
            {"id": 7, "status": "NEW", "telegram": {"user_id": 17}},
            {"id": 8, "status": "NEW", "telegram": {"user_id": 18}},
            {"id": 9, "status": "DONE", "telegram": {"user_id": 17}},
        ]
        self.writes = []
        async def read():
            return self.leads
        async def write(leads):
            self.writes.append(leads)
        self.read_patch = patch.object(content_store, "_read_leads", new=read)
        self.write_patch = patch.object(content_store, "_write_leads", new=write)
        self.read_patch.start()
        self.write_patch.start()

    async def asyncTearDown(self):
        self.read_patch.stop()
        self.write_patch.stop()

    async def test_idempotent_message(self):
        self.assertIsNotNone(await content_store.add_client_message(17, 7, 42, "text", "Hello"))
        self.assertIsNotNone(await content_store.add_client_message(17, 7, 42, "text", "Hello"))
        self.assertEqual(len(self.leads[0]["client_messages"]), 1)
        self.assertEqual(len(self.writes), 1)

    async def test_cannot_write_to_other_users_lead(self):
        self.assertIsNone(await content_store.add_client_message(17, 8, 43, "text", "wrong"))
        self.assertEqual(self.writes, [])

    async def test_cannot_write_to_closed_lead(self):
        self.assertIsNone(await content_store.add_client_message(17, 9, 44, "text", "closed"))
        self.assertEqual(self.writes, [])

    async def test_invalid_message_id_rejected(self):
        self.assertIsNone(await content_store.add_client_message(17, 7, 0, "media"))
        self.assertEqual(self.writes, [])


if __name__ == "__main__":
    unittest.main()
