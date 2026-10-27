import importlib.util
import io
import json
import os
import sys
import tempfile
import threading
import types
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "root", "usr", "lib"))

# fcntl есть только на Linux; для локального запуска тестов на Windows.
if "fcntl" not in sys.modules:
    try:
        import fcntl  # noqa: F401
    except ImportError:
        stub = types.ModuleType("fcntl")
        stub.LOCK_EX = 1
        stub.LOCK_NB = 2
        stub.LOCK_UN = 4
        stub.flock = lambda *a, **kw: None
        sys.modules["fcntl"] = stub

from subparser import config  # noqa: E402
from subparser import podkop  # noqa: E402
from subparser import telegram  # noqa: E402
from subparser import system  # noqa: E402
from subparser import subs  # noqa: E402

_spec = importlib.util.spec_from_file_location(
    "subparser_main", os.path.join(ROOT, "root", "usr", "bin", "subparser.py"))
subparser_main = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(subparser_main)


class TestRunLog(unittest.TestCase):
    def test_trim_keeps_tail(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "sync.log")
            with open(path, "w", encoding="utf-8") as f:
                for i in range(300):
                    f.write(f"old {i}\n")
            with mock.patch.object(subparser_main, "SYNC_LOG_FILE", path), \
                    mock.patch.object(subparser_main, "RUN_LOG_KEEP_LINES", 200):
                subparser_main._trim_run_log()
            with open(path, encoding="utf-8") as f:
                lines = f.read().splitlines()
            self.assertEqual(len(lines), 200)
            self.assertEqual(lines[0], "old 100")

    def test_tee_writes_to_console_and_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "sync.log")
            console = io.StringIO()
            with open(path, "w", encoding="utf-8") as log_fp:
                tee = subparser_main._TeeStream(console, log_fp, threading.Lock())
                tee.write("строка лога\n")
                tee.flush()
            self.assertEqual(console.getvalue(), "строка лога\n")
            with open(path, encoding="utf-8") as f:
                self.assertEqual(f.read(), "строка лога\n")

    def test_launch_source_from_env(self):
        with mock.patch.dict(os.environ, {"SUBPARSER_TRIGGER": "cron"}):
            self.assertEqual(subparser_main._detect_launch_source(), "cron")


class TestRunParserProgress(unittest.TestCase):
    def test_progress_written_before_start(self):
        progress = {"token": "t", "chat_id": 1, "msg_id": 2, "started": 123}
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "progress.json")
            with mock.patch.object(podkop, "PROGRESS_FILE", path), \
                    mock.patch.object(podkop, "is_parser_running", return_value=False), \
                    mock.patch.object(podkop.subprocess, "Popen") as popen:
                ok, _ = podkop.run_parser_process(progress=progress)
            self.assertTrue(ok)
            with open(path, encoding="utf-8") as f:
                self.assertEqual(json.load(f), progress)
            self.assertEqual(popen.call_args.kwargs["env"]["SUBPARSER_TRIGGER"], "telegram")

    def test_stale_progress_removed_without_progress(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "progress.json")
            with open(path, "w", encoding="utf-8") as f:
                f.write("{}")
            with mock.patch.object(podkop, "PROGRESS_FILE", path), \
                    mock.patch.object(podkop, "is_parser_running", return_value=False), \
                    mock.patch.object(podkop.subprocess, "Popen"):
                ok, _ = podkop.run_parser_process()
            self.assertTrue(ok)
            self.assertFalse(os.path.exists(path))

    def test_progress_not_touched_when_already_running(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "progress.json")
            with open(path, "w", encoding="utf-8") as f:
                f.write('{"old": true}')
            with mock.patch.object(podkop, "PROGRESS_FILE", path), \
                    mock.patch.object(podkop, "is_parser_running", return_value=True), \
                    mock.patch.object(podkop.subprocess, "Popen") as popen:
                ok, _ = podkop.run_parser_process(progress={"token": "new"})
            self.assertFalse(ok)
            popen.assert_not_called()
            with open(path, encoding="utf-8") as f:
                self.assertEqual(f.read(), '{"old": true}')


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


class TestPodkopSections(unittest.TestCase):
    """Podkop хранит секции как `config section` — их нельзя искать по `=podkop`."""

    def test_section_type_is_detected(self):
        raw = (
            "podkop.settings=settings\n"
            "podkop.settings.dns_type='udp'\n"
            "podkop.main=section\n"
            "podkop.main.urltest_proxy_links='vless://a'\n"
            "podkop.second=section\n"
            "podkop.second.connection_type='proxy'\n"
        )
        self.assertEqual(podkop.parse_podkop_sections(raw), ["main", "second"])

    def test_legacy_podkop_type_is_detected(self):
        raw = (
            "podkop.main=podkop\n"
            "podkop.main.urltest_proxy_links='vless://a'\n"
            "podkop.second=podkop\n"
        )
        self.assertEqual(podkop.parse_podkop_sections(raw), ["main", "second"])

    def test_settings_section_is_not_a_podkop_section(self):
        raw = "podkop.settings=settings\npodkop.settings.dns_type='udp'\n"
        self.assertEqual(podkop.parse_podkop_sections(raw), [])

    def test_section_without_links_detected_by_type(self):
        raw = "podkop.main=section\npodkop.extra=section\n"
        self.assertEqual(podkop.parse_podkop_sections(raw), ["main", "extra"])

    def test_anonymous_sections(self):
        raw = (
            "podkop.@section[0]=section\n"
            "podkop.@section[0].urltest_proxy_links='vless://a'\n"
            "podkop.@section[1]=section\n"
        )
        self.assertEqual(podkop.parse_podkop_sections(raw),
                         ["@section[0]", "@section[1]"])


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
