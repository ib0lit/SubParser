import os
import sys
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "root", "usr", "lib"))

from subparser import config  # noqa: E402
from subparser import telegram  # noqa: E402
from subparser import system  # noqa: E402
from subparser import subs  # noqa: E402


class TestUAAndConfig(unittest.TestCase):
    def test_ua_map_has_expected_keys(self):
        for key in ("v2rayN", "singbox", "happ", "incy", "curl"):
            self.assertIn(key, config.UA_MAP)
            self.assertTrue(config.UA_MAP[key])

    def test_uci_bool_parsing(self):
        for raw in ("1", "true", "Yes", "on"):
            with mock.patch.object(config, "get_uci_val", return_value=raw):
                self.assertTrue(config.get_uci_bool("x"))
        for raw in ("0", "false", "", "no"):
            with mock.patch.object(config, "get_uci_val", return_value=raw):
                self.assertFalse(config.get_uci_bool("x"))

    def test_ssl_context_verifies_by_default(self):
        ctx = config.make_ssl_context(insecure=False)
        import ssl
        self.assertEqual(ctx.verify_mode, ssl.CERT_REQUIRED)


class TestTelegramSplit(unittest.TestCase):
    def test_short_text_single_chunk(self):
        self.assertEqual(telegram._split_text("hello"), ["hello"])

    def test_long_text_chunks_fit_limit(self):
        text = ("line\n" * 3000)
        chunks = telegram._split_text(text)
        self.assertTrue(chunks)
        for chunk in chunks:
            self.assertLessEqual(len(chunk), telegram.TG_MAX_LEN)


class TestDiagGuard(unittest.TestCase):
    def test_loopback_blocked_by_default(self):
        with mock.patch.object(system, "get_uci_bool", return_value=False):
            with self.assertRaises(ValueError):
                system._validate_diag_target("http://127.0.0.1:8080/")

    def test_loopback_allowed_with_opt_in(self):
        with mock.patch.object(system, "get_uci_bool", return_value=True):
            url, host = system._validate_diag_target("http://127.0.0.1:8080/")
            self.assertEqual(host, "127.0.0.1")
            self.assertTrue(url.startswith("http://"))

    def test_non_http_scheme_rejected(self):
        with self.assertRaises(ValueError):
            system._validate_diag_target("file:///etc/passwd")


class TestSubscriptionValidation(unittest.TestCase):
    def test_non_http_url_rejected_without_touching_uci(self):
        ok, msg = subs.add_subscription_to_uci("ftp://example.com/sub")
        self.assertFalse(ok)
        self.assertIn("http", msg)


if __name__ == "__main__":
    unittest.main()
