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


class TestDeadScanAndDelete(unittest.TestCase):
    def _scan(self, sections, links_map, proxies, delays_map):
        def fake_links(section):
            return list(links_map.get(section, []))

        def fake_ping(links, section="main", force=False):
            return dict(delays_map.get(section, {}))

        with mock.patch.object(podkop, "get_all_podkop_sections", return_value=sections), \
                mock.patch.object(podkop, "fetch_proxies",
                                  return_value=(proxies, 200)), \
                mock.patch.object(podkop, "get_proxies", return_value=proxies), \
                mock.patch.object(podkop, "get_podkop_links", side_effect=fake_links), \
                mock.patch.object(podkop, "batch_ping_nodes", side_effect=fake_ping), \
                mock.patch.object(podkop, "delete_nodes_batch") as delete, \
                mock.patch.object(podkop, "restart_podkop") as restart:
            result = podkop.scan_dead_in_all_sections()
        return result, delete, restart

    def test_scan_builds_plan_without_changes(self):
        result, delete, restart = self._scan(
            ["main", "gemini", "empty"],
            {"main": ["a", "b"], "gemini": ["c", "d"], "empty": []},
            {"main-1-out": {}, "main-2-out": {},
             "gemini-1-out": {}, "gemini-2-out": {}},
            {"main": {0: 100, 1: -1}, "gemini": {0: -1, 1: 150}},
        )
        self.assertEqual(result["total"], 2)
        self.assertFalse(result["error"])
        main, gemini, empty = result["details"]
        self.assertEqual(main["status"], "dead")
        self.assertEqual(main["indices"], [1])
        self.assertEqual(gemini["indices"], [0])
        self.assertEqual(empty["status"], "empty")
        delete.assert_not_called()
        restart.assert_not_called()

    def test_scan_marks_fully_dead_section(self):
        result, delete, restart = self._scan(
            ["only"], {"only": ["a"]}, {"only-1-out": {}}, {"only": {0: -1}})
        self.assertEqual(result["total"], 0)
        self.assertEqual(result["details"][0]["status"], "all_dead")
        self.assertEqual(result["details"][0]["indices"], [])
        delete.assert_not_called()
        restart.assert_not_called()

    def test_delete_plan_deletes_and_restarts_once(self):
        details = [
            {"section": "main", "total": 2, "dead": 1, "skipped": 0,
             "status": "dead", "indices": [1]},
            {"section": "gemini", "total": 2, "dead": 1, "skipped": 0,
             "status": "dead", "indices": [0]},
        ]
        links = {"main": ["a", "b"], "gemini": ["c", "d"]}
        with mock.patch.object(podkop, "get_podkop_links",
                               side_effect=lambda s: list(links[s])), \
                mock.patch.object(podkop, "get_proxies", return_value={}), \
                mock.patch.object(podkop, "delete_nodes_batch",
                                  return_value=(True, 1)) as delete, \
                mock.patch.object(podkop, "restart_podkop",
                                  return_value=True) as restart:
            result = podkop.delete_dead_plan(details)
        self.assertEqual(result["total"], 2)
        self.assertTrue(result["restarted"])
        self.assertEqual(delete.call_count, 2)
        delete.assert_any_call([1], section="main", restart=False)
        delete.assert_any_call([0], section="gemini", restart=False)
        restart.assert_called_once()

    def test_delete_plan_refuses_to_empty_section(self):
        details = [{"section": "main", "total": 2, "dead": 2, "skipped": 0,
                    "status": "dead", "indices": [0, 1]}]
        with mock.patch.object(podkop, "get_podkop_links",
                               return_value=["a", "b"]), \
                mock.patch.object(podkop, "delete_nodes_batch") as delete, \
                mock.patch.object(podkop, "restart_podkop") as restart:
            result = podkop.delete_dead_plan(details)
        self.assertEqual(result["total"], 0)
        self.assertEqual(result["details"][0]["status"], "all_dead")
        delete.assert_not_called()
        restart.assert_not_called()

    def test_plan_is_skipped_when_section_changed(self):
        details = [{"section": "main", "total": 3, "dead": 1, "skipped": 0,
                    "status": "dead", "indices": [2]}]
        with mock.patch.object(podkop, "get_podkop_links",
                               return_value=["a", "b"]), \
                mock.patch.object(podkop, "delete_nodes_batch") as delete, \
                mock.patch.object(podkop, "restart_podkop") as restart:
            result = podkop.delete_dead_plan(details)
        self.assertEqual(result["total"], 0)
        self.assertFalse(result["restarted"])
        self.assertEqual(result["details"][0]["status"], "stale")
        delete.assert_not_called()
        restart.assert_not_called()

    def test_plan_is_skipped_when_links_changed(self):
        details = [{"section": "main", "total": 2, "dead": 1, "skipped": 0,
                    "status": "dead", "indices": [1], "links": ["a", "b"]}]
        with mock.patch.object(podkop, "get_podkop_links",
                               return_value=["a", "c"]), \
                mock.patch.object(podkop, "delete_nodes_batch") as delete, \
                mock.patch.object(podkop, "restart_podkop") as restart:
            result = podkop.delete_dead_plan(details)
        self.assertEqual(result["details"][0]["status"], "stale")
        delete.assert_not_called()
        restart.assert_not_called()

    def test_restart_failure_is_reported(self):
        details = [{"section": "main", "total": 2, "dead": 1, "skipped": 0,
                    "status": "dead", "indices": [0]}]
        with mock.patch.object(podkop, "get_podkop_links",
                               return_value=["a", "b"]), \
                mock.patch.object(podkop, "get_proxies", return_value={}), \
                mock.patch.object(podkop, "delete_nodes_batch",
                                  return_value=(True, 1)), \
                mock.patch.object(podkop, "restart_podkop",
                                  return_value=False) as restart:
            result = podkop.delete_dead_plan(details)
        self.assertEqual(result["total"], 1)
        self.assertFalse(result["restarted"])
        restart.assert_called_once()

    def _delete_with_selection(self, now, indices):
        details = [{"section": "main", "total": 4, "dead": len(indices),
                    "skipped": 0, "status": "dead", "indices": indices,
                    "links": ["a", "b", "c", "d"]}]
        proxies = {"main-out": {"type": "selector", "now": now}} \
            if now is not None else {}
        with mock.patch.object(podkop, "get_podkop_links",
                               return_value=["a", "b", "c", "d"]), \
                mock.patch.object(podkop, "get_proxies",
                                  return_value=proxies), \
                mock.patch.object(podkop, "fetch_proxies",
                                  return_value=({}, 200)), \
                mock.patch.object(podkop, "delete_nodes_batch",
                                  return_value=(True, len(indices))), \
                mock.patch.object(podkop, "restart_podkop",
                                  return_value=True), \
                mock.patch.object(podkop, "switch_active_node",
                                  return_value=(True, "tag")) as switch:
            result = podkop.delete_dead_plan(details)
        return result, switch

    def test_manual_selection_restored_by_link_after_reindex(self):
        # Был выбран main-3-out (ссылка c). Удаляем main-2-out (b):
        # c переезжает с индекса 2 на 1, селектор должен остаться на c.
        result, switch = self._delete_with_selection("main-3-out", [1])
        self.assertEqual(result["details"][0]["selection"], "restored")
        switch.assert_called_once_with("main", 1)

    def test_urltest_selection_is_not_touched(self):
        result, switch = self._delete_with_selection("main-urltest-out", [1])
        self.assertEqual(result["details"][0]["selection"], "auto")
        switch.assert_not_called()

    def test_deleted_selected_node_falls_back_to_urltest(self):
        # Выбран был main-2-out (b), его же и удаляем.
        result, switch = self._delete_with_selection("main-2-out", [1])
        self.assertEqual(result["details"][0]["selection"], "lost")
        switch.assert_not_called()

    def test_selection_not_restored_without_restart(self):
        details = [{"section": "main", "total": 3, "dead": 1, "skipped": 0,
                    "status": "dead", "indices": [0],
                    "links": ["a", "b", "c"]}]
        proxies = {"main-out": {"type": "selector", "now": "main-2-out"}}
        with mock.patch.object(podkop, "get_podkop_links",
                               return_value=["a", "b", "c"]), \
                mock.patch.object(podkop, "get_proxies",
                                  return_value=proxies), \
                mock.patch.object(podkop, "delete_nodes_batch",
                                  return_value=(True, 1)), \
                mock.patch.object(podkop, "restart_podkop",
                                  return_value=False), \
                mock.patch.object(podkop, "switch_active_node") as switch:
            result = podkop.delete_dead_plan(details)
        self.assertEqual(result["details"][0]["selection"], "not_restarted")
        switch.assert_not_called()

    def test_api_unavailable_returns_error(self):
        result, delete, restart = self._scan(
            ["main"], {"main": ["a"]}, {}, {"main": {0: -1}})
        self.assertIn("API", result["error"])
        self.assertEqual(result["details"], [])
        delete.assert_not_called()
        restart.assert_not_called()

    def test_unknown_nodes_are_skipped(self):
        result, delete, restart = self._scan(
            ["gemini"], {"gemini": ["a"]}, {"main-1-out": {}},
            {"gemini": {0: -1}})
        self.assertEqual(result["details"][0]["status"], "no_data")
        self.assertEqual(result["total"], 0)
        delete.assert_not_called()
        restart.assert_not_called()


class TestLastNodeProtection(unittest.TestCase):
    def test_write_links_refuses_empty_list(self):
        with mock.patch.object(podkop.subprocess, "Popen") as popen, \
                mock.patch.object(podkop.subprocess, "run") as run:
            self.assertFalse(podkop._write_links("main", []))
        popen.assert_not_called()
        run.assert_not_called()

    def test_batch_delete_refuses_all_nodes(self):
        with mock.patch.object(podkop, "get_podkop_links",
                               return_value=["a", "b"]), \
                mock.patch.object(podkop, "_write_links") as write:
            ok, deleted = podkop.delete_nodes_batch([0, 1])
        self.assertFalse(ok)
        self.assertEqual(deleted, 0)
        write.assert_not_called()

    def test_single_delete_refuses_last_node(self):
        with mock.patch.object(podkop, "get_podkop_links",
                               return_value=["a"]), \
                mock.patch.object(podkop, "_write_links") as write:
            ok, msg = podkop.delete_node(0)
        self.assertFalse(ok)
        self.assertIn("последний", msg)
        write.assert_not_called()


class TestRestartPodkop(unittest.TestCase):
    def test_returns_true_when_singbox_pid_changes(self):
        pids = iter(["100", "100", "200"])
        with mock.patch.object(podkop, "_singbox_pid",
                               side_effect=lambda: next(pids)), \
                mock.patch.object(podkop.subprocess, "Popen") as popen, \
                mock.patch.object(podkop.time, "sleep"):
            self.assertTrue(podkop.restart_podkop(timeout=10))
        popen.assert_called_once()
        self.assertEqual(popen.call_args.args[0], [podkop.PODKOP_INIT, "restart"])
        popen.return_value.wait.assert_called_once()

    def test_returns_false_when_service_does_not_restart(self):
        with mock.patch.object(podkop, "_singbox_pid", return_value="100"), \
                mock.patch.object(podkop.subprocess, "Popen"), \
                mock.patch.object(podkop.time, "sleep"), \
                mock.patch.object(podkop.time, "time",
                                  side_effect=[0, 1, 2, 3, 100]):
            self.assertFalse(podkop.restart_podkop(timeout=5))


class TestPodkopApiAuth(unittest.TestCase):
    """YACD включает secret у Clash API — SubParser должен его использовать."""

    def test_headers_fall_back_to_yacd_secret(self):
        def fake_uci(opt, default=""):
            return default

        def fake_raw(path, default=""):
            if path == "podkop.settings.yacd_secret_key":
                return "yacd-secret"
            return default

        with mock.patch.object(podkop, "get_uci_val", side_effect=fake_uci), \
                mock.patch.object(podkop, "_uci_get", side_effect=fake_raw):
            headers = podkop.get_api_headers()
        self.assertEqual(headers["Authorization"], "Bearer yacd-secret")

    def test_explicit_secret_wins_over_yacd(self):
        def fake_uci(opt, default=""):
            return {"podkop_api_secret": "user-secret",
                    "clash_api_secret": ""}.get(opt, default)

        with mock.patch.object(podkop, "get_uci_val", side_effect=fake_uci), \
                mock.patch.object(podkop, "_uci_get", return_value="yacd-secret"):
            headers = podkop.get_api_headers()
        self.assertEqual(headers["Authorization"], "Bearer user-secret")

    def test_api_base_uses_podkop_listen_address(self):
        def fake_uci(opt, default=""):
            return default

        def fake_raw(path, default=""):
            if path == "podkop.settings.service_listen_address":
                return "10.0.0.5/24"
            return default

        with mock.patch.object(podkop, "get_uci_val", side_effect=fake_uci), \
                mock.patch.object(podkop, "_uci_get", side_effect=fake_raw):
            self.assertEqual(podkop.get_api_base(), "http://10.0.0.5:9090")

    def test_api_base_ignores_wildcard_listen_address(self):
        def fake_raw(path, default=""):
            return "0.0.0.0"

        with mock.patch.object(podkop, "get_uci_val", return_value=""), \
                mock.patch.object(podkop, "_uci_get", side_effect=fake_raw), \
                mock.patch.object(podkop, "get_lan_ip", return_value="192.168.1.1"):
            self.assertEqual(podkop.get_api_base(), "http://192.168.1.1:9090")

    def test_http_401_is_returned_as_status(self):
        import urllib.error
        err = urllib.error.HTTPError(
            "http://router:9090/proxies", 401, "Unauthorized", {},
            io.BytesIO(b'{"message":"Unauthorized"}'))
        with mock.patch.object(podkop, "get_api_base",
                               return_value="http://router:9090"), \
                mock.patch.object(podkop, "get_api_headers", return_value={}), \
                mock.patch.object(podkop.urllib.request, "urlopen",
                                  side_effect=err):
            status, data = podkop._api_request("/proxies")
        self.assertEqual(status, 401)
        self.assertEqual(data["message"], "Unauthorized")

    def test_scan_reports_auth_error_instead_of_unreachable(self):
        with mock.patch.object(podkop, "get_all_podkop_sections",
                               return_value=["main"]), \
                mock.patch.object(podkop, "fetch_proxies",
                                  return_value=({}, 401)):
            result = podkop.scan_dead_in_all_sections()
        self.assertEqual(result["total"], 0)
        self.assertIn("авторизацию", result["error"])
        self.assertIn("YACD", result["error"])


class TestSelectionRestore(unittest.TestCase):
    def test_manual_selection_found_by_link_ignoring_name(self):
        links = ["a#old-name", "c#new-name", "d"]
        with mock.patch.object(podkop, "fetch_proxies",
                               return_value=({}, 200)), \
                mock.patch.object(podkop, "switch_active_node",
                                  return_value=(True, "main-2-out")) as switch:
            status = podkop.restore_selection("main", "manual",
                                              "c#old-name", links)
        self.assertEqual(status, "restored")
        switch.assert_called_once_with("main", 1)

    def test_already_selected_link_is_not_switched(self):
        # Режим добавления: индекс не сдвинулся, Podkop уже на нужном сервере.
        links = ["a", "c"]
        proxies = {"main-out": {"type": "selector", "now": "main-2-out"}}
        with mock.patch.object(podkop, "switch_active_node") as switch:
            status = podkop.restore_selection("main", "manual", "c#renamed",
                                              links, proxies=proxies)
        self.assertEqual(status, "restored")
        switch.assert_not_called()

    def test_urltest_selection_is_left_alone(self):
        with mock.patch.object(podkop, "switch_active_node") as switch:
            status = podkop.restore_selection("main", "auto", None, ["a"])
        self.assertEqual(status, "auto")
        switch.assert_not_called()

    def test_missing_selected_link_falls_back_to_urltest(self):
        with mock.patch.object(podkop, "switch_active_node") as switch:
            status = podkop.restore_selection("main", "manual", "gone",
                                              ["a", "b"], proxies={})
        self.assertEqual(status, "lost")
        switch.assert_not_called()

    def test_restore_failure_is_reported(self):
        with mock.patch.object(podkop, "fetch_proxies",
                               return_value=({}, 200)), \
                mock.patch.object(podkop, "switch_active_node",
                                  return_value=(False, "main-1-out")), \
                mock.patch.object(podkop.time, "sleep"):
            status = podkop.restore_selection("main", "manual", "a", ["a"])
        self.assertEqual(status, "failed")

    def test_unreachable_api_is_not_reported_as_failure(self):
        with mock.patch.object(podkop, "fetch_proxies",
                               return_value=({}, None)), \
                mock.patch.object(podkop, "switch_active_node",
                                  return_value=(False, "main-1-out")), \
                mock.patch.object(podkop.time, "sleep"):
            status = podkop.restore_selection("main", "manual", "a", ["a"])
        self.assertEqual(status, "not_ready")


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

    def test_uci_list_parsing_accepts_spaces_and_commas(self):
        with mock.patch.object(config, "get_uci_val", return_value="vless, trojan  ss"):
            self.assertEqual(config.get_uci_list("exclude_protocols"),
                             ["vless", "trojan", "ss"])

    def test_protocol_alias_normalization(self):
        self.assertEqual(config.normalize_protocol("HY2"), "hysteria2")
        self.assertEqual(config.normalize_protocol("VLESS"), "vless")


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


class TestManagedPodkopSections(unittest.TestCase):
    def _managed(self, all_sections, per_section, sec_en, target):
        values = {f"sec_en_{s}": v for s, v in sec_en.items()}

        def fake_bool(opt, default=False):
            if opt == "per_section_config":
                return per_section
            return values.get(opt, default)

        with mock.patch.object(podkop, "get_all_podkop_sections",
                               return_value=all_sections), \
                mock.patch.object(podkop, "get_uci_bool", side_effect=fake_bool), \
                mock.patch.object(podkop, "get_uci_val", return_value=target):
            return podkop.get_managed_podkop_sections()

    def test_per_section_mode_excludes_disabled(self):
        result = self._managed(["main", "gemini"], True,
                               {"main": True, "gemini": False}, "")
        self.assertEqual(result, ["main"])

    def test_target_list_excludes_removed_section(self):
        result = self._managed(["main", "gemini"], False, {}, "main")
        self.assertEqual(result, ["main"])

    def test_empty_target_means_all_sections(self):
        result = self._managed(["main", "gemini"], False, {}, "")
        self.assertEqual(result, ["main", "gemini"])

    def test_all_sections_disabled_falls_back_to_main(self):
        result = self._managed(["main", "gemini"], True,
                               {"main": False, "gemini": False}, "")
        self.assertEqual(result, ["main"])


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
