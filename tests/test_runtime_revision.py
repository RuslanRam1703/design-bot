"""Health stays cheap and exposes only a validated non-secret commit SHA."""
import unittest
from unittest.mock import patch
from bot.webserver import handle_health


class RuntimeRevisionTests(unittest.IsolatedAsyncioTestCase):
    async def test_valid_deployed_commit_is_exposed_without_changing_body(self):
        with patch.dict('os.environ', {'RENDER_GIT_COMMIT': 'a' * 40}):
            response = await handle_health(None)
        self.assertEqual(response.status, 200)
        self.assertEqual(response.text, 'ok')
        self.assertEqual(response.headers['X-App-Revision'], 'a' * 40)
        self.assertEqual(response.headers['Cache-Control'], 'no-store')

    async def test_missing_or_malformed_revision_is_not_reflected(self):
        for value in ('', 'not-a-commit', 'g' * 40, 'a' * 40 + '\r\nInjected: value'):
            with patch.dict('os.environ', {'RENDER_GIT_COMMIT': value}):
                self.assertNotIn('X-App-Revision', (await handle_health(None)).headers)
