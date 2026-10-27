import importlib.util
import os
import sys
import types
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LIB = os.path.join(ROOT, "root", "usr", "lib")
SCRIPT = os.path.join(ROOT, "root", "usr", "bin", "subparser.py")

# subparser.py импортирует fcntl, которого нет на Windows (локальный запуск тестов).
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

sys.path.insert(0, LIB)

spec = importlib.util.spec_from_file_location("subparser_main", SCRIPT)
parser = importlib.util.module_from_spec(spec)
spec.loader.exec_module(parser)


class TestLinkHelpers(unittest.TestCase):
    def test_fingerprint_ignores_fragment(self):
        a = "vless://uuid@example.com:443?type=tcp#Node%20A"
        b = "vless://uuid@example.com:443?type=tcp#Node%20B"
        self.assertEqual(parser.link_fingerprint(a), parser.link_fingerprint(b))

    def test_sanitize_hy2_removes_security_none(self):
        link = "hysteria2://pass@example.com:443?sni=example.com&security=none#HY2"
        cleaned = parser.sanitize_podkop_link(link)
        self.assertNotIn("security=none", cleaned)

    def test_prepare_link_encodes_fragment(self):
        link = "vless://uuid@example.com:443?type=tcp#My Node & Co"
        prepared = parser._prepare_link(link, {}, 0)
        self.assertIn("#My%20Node%20%26%20Co", prepared)

    def test_parse_vless_reality(self):
        link = ("vless://uuid@example.com:443?security=reality&sni=example.com"
                "&pbk=abc&sid=12&type=tcp&flow=xtls-rprx-vision-udp443#N")
        out = parser.parse_link_to_singbox_outbound(link, "n_1")
        self.assertEqual(out["type"], "vless")
        self.assertTrue(out["tls"]["reality"]["enabled"])
        self.assertNotIn("udp443", out.get("flow", ""))

    def test_parse_unsupported_scheme(self):
        with self.assertRaises(ValueError):
            parser.parse_link_to_singbox_outbound("ss://abc@example.com:443#x", "n_1")


if __name__ == "__main__":
    unittest.main()
