"""Malformed Mini App credentials must fail closed without HTTP 500s."""
import hashlib
import hmac
import json
import time
import unittest
from urllib.parse import urlencode

from bot.telegram_auth import diagnose_init_data, validate_init_data

TOKEN = '123456:test-token-not-real'


def signed(**overrides):
    fields = {'auth_date': str(int(time.time())), 'user': json.dumps({'id': 42})}
    fields.update(overrides)
    fields = {k: v for k, v in fields.items() if v is not None}
    key = hmac.new(b'WebAppData', TOKEN.encode(), hashlib.sha256).digest()
    check = '\n'.join(f'{k}={v}' for k, v in sorted(fields.items()))
    fields['hash'] = hmac.new(key, check.encode(), hashlib.sha256).hexdigest()
    return urlencode(fields)


class AuthBoundaryTests(unittest.TestCase):
    def test_unicode_hash_is_rejected_by_validation_and_diagnostics(self):
        raw = urlencode({'hash': 'я' * 64, 'auth_date': '123'})
        self.assertIsNone(validate_init_data(raw, TOKEN))
        self.assertFalse(diagnose_init_data(raw, TOKEN).hmac_valid)

    def test_duplicate_fields_are_rejected(self):
        raw = signed() + '&auth_date=' + str(int(time.time()))
        self.assertIsNone(validate_init_data(raw, TOKEN))
        self.assertFalse(diagnose_init_data(raw, TOKEN).parse_ok)

    def test_missing_and_future_dates_are_rejected(self):
        for date in (None, str(int(time.time()) + 3600)):
            with self.subTest(date=date):
                self.assertIsNone(validate_init_data(signed(auth_date=date), TOKEN))

    def test_user_id_must_be_positive_integer(self):
        for user_id in (True, 0, -1, '42', None, [], {}):
            with self.subTest(user_id=user_id):
                raw = signed(user=json.dumps({'id': user_id}))
                self.assertIsNone(validate_init_data(raw, TOKEN))
                self.assertFalse(diagnose_init_data(raw, TOKEN).user_json_ok)

    def test_small_clock_skew_is_accepted(self):
        self.assertEqual(validate_init_data(signed(auth_date=str(int(time.time()) + 30)), TOKEN)['id'], 42)
