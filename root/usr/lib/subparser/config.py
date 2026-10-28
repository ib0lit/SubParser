# -*- coding: utf-8 -*-
import subprocess
import json
import os
import ssl
import tempfile
import time

CONFIG_NAME = "subparser"
LOCK_FILE = "/var/run/subparser.lock"
SINGBOX_BIN = "/usr/bin/sing-box"
LOG_FILE = "/tmp/subparser_sync.log"
STATE_FILE = "/tmp/subparser_bot_state.json"
CACHE_FILE = "/tmp/subparser_cache.json"
METRICS_FILE = "/tmp/subparser_metrics.json"
TEST_URL_GLOBAL = "https://www.gstatic.com/generate_204"

# Список блокировок должен переживать перезагрузку роутера.
PERSIST_DIR = "/etc/subparser"
BLOCKED_FILE = os.path.join(PERSIST_DIR, "blocked_macs.json")

# Единый справочник User-Agent для парсера, бота и LuCI (Python-часть).
UA_MAP = {
    "v2rayN": "v2rayN/6.42",
    "ClashMeta": "ClashMeta/1.18.0",
    "clashmeta": "ClashMeta/1.18.0",
    "SingBox": "sing-box/1.9.0",
    "singbox": "sing-box/1.9.0",
    "Shadowrocket": "Shadowrocket/2.2.0",
    "shadowrocket": "Shadowrocket/2.2.0",
    "NekoBox": "NekoBox/1.3.1",
    "flclash": "ClashMeta/1.18.0",
    "clash": "ClashforWindows/0.20.39",
    "incy": "Incy/3.7.0 (Android)",
    "happ": "Happ/1.0.0 (iOS)",
    "curl": "curl/7.88.1",
}

UA_CHOICES = {
    "v2rayN": "v2rayN/6.23",
    "flclash": "FlClashX / ClashMeta",
    "singbox": "Sing-Box",
    "happ": "Happ (iOS)",
    "incy": "Incy (Android)",
    "shadowrocket": "Shadowrocket",
    "clashmeta": "Clash.Meta (Mihomo)",
    "clash": "Clash for Windows",
    "NekoBox": "NekoBox",
    "curl": "curl (Generic)",
}

UA_LIST = list(UA_CHOICES.keys())


def _ensure_persist_dir():
    try:
        os.makedirs(PERSIST_DIR, exist_ok=True)
    except Exception:
        pass


def get_uci_val(opt, default=""):
    try:
        res = subprocess.run(["uci", "-q", "get", f"{CONFIG_NAME}.settings.{opt}"],
                             stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
        out = res.stdout.strip().strip("'\"")
        return out if out else default
    except Exception:
        return default


def get_uci_bool(opt, default=False):
    val = str(get_uci_val(opt, "1" if default else "0")).strip().lower()
    return val in ("1", "true", "yes", "on")


# Протоколы, которые умеет разбирать парсер. Ключи используются в UCI-списке
# `exclude_protocols`, значения — в меню бота и LuCI.
PROTOCOL_CHOICES = {
    "vless": "VLESS",
    "trojan": "Trojan",
    "hysteria2": "Hysteria2",
    "ss": "Shadowsocks",
    "socks4": "SOCKS4",
    "socks4a": "SOCKS4a",
    "socks5": "SOCKS5",
}
PROTOCOL_ALIASES = {"hy2": "hysteria2"}


def normalize_protocol(name):
    """Приводит схему ссылки/значение UCI к каноническому имени протокола."""
    value = str(name or "").strip().lower()
    return PROTOCOL_ALIASES.get(value, value)


def get_uci_list(opt):
    """Читает UCI-список (значения через пробел) в виде списка строк."""
    raw = get_uci_val(opt, "")
    return [item for item in raw.replace(",", " ").split() if item]


def set_uci_list(opt, values):
    """Полностью заменяет UCI-список значений."""
    try:
        subprocess.run(["uci", "-q", "delete", f"{CONFIG_NAME}.settings.{opt}"], check=False,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        for value in values:
            subprocess.run(["uci", "add_list", f"{CONFIG_NAME}.settings.{opt}={value}"],
                           check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        subprocess.run(["uci", "commit", CONFIG_NAME], check=False,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return True
    except Exception:
        return False


def check_credentials():
    token = get_uci_val("tg_bot_token")
    admin_id = get_uci_val("tg_chat_id")
    if not token or not admin_id or token == "null" or admin_id == "null":
        return None, None
    return str(token).strip(), str(admin_id).strip()


def set_uci_val(opt, val):
    try:
        # Аргумент передаётся напрямую в execve, без shell-интерполяции.
        subprocess.run(["uci", "set", f"{CONFIG_NAME}.settings.{opt}={val}"], check=False,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        subprocess.run(["uci", "commit", CONFIG_NAME], check=False,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return True
    except Exception:
        return False


def make_ssl_context(insecure=None):
    """По умолчанию проверяем TLS. Отключение — только явной опцией UCI."""
    if insecure is None:
        insecure = get_uci_bool("allow_insecure_tls", False)
    ctx = ssl.create_default_context()
    if insecure:
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    return ctx


def get_ssl_ctx():
    return make_ssl_context()


# Обратная совместимость с модулями, которые импортируют SSL_CTX.
SSL_CTX = make_ssl_context(insecure=False)


def _load_json(path, default):
    if not os.path.exists(path):
        return default
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def _save_json_atomic(path, data):
    directory = os.path.dirname(path) or "/tmp"
    try:
        os.makedirs(directory, exist_ok=True)
        fd, tmp_path = tempfile.mkstemp(prefix=".subparser_", dir=directory)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
        os.replace(tmp_path, path)
        try:
            os.chmod(path, 0o600)
        except Exception:
            pass
        return True
    except Exception:
        return False


def _load_state():
    data = _load_json(STATE_FILE, {})
    if not isinstance(data, dict):
        data = {}
    if "users" not in data or not isinstance(data.get("users"), dict):
        # Миграция со старого формата: одно глобальное состояние.
        old = {}
        if data and not any(k in data for k in ("users", "offset")):
            old = data
        data = {"users": {}, "offset": int(data.get("offset", 0) or 0) if isinstance(data, dict) else 0}
        if old:
            # Старое состояние не привязываем к пользователю, чтобы не угадывать.
            data.setdefault("legacy", old)
    return data


def get_user_state(user_id=None):
    data = _load_state()
    if user_id is None:
        return data.get("legacy", {}) if isinstance(data.get("legacy"), dict) else {}
    users = data.get("users", {})
    state = users.get(str(user_id), {})
    return state if isinstance(state, dict) else {}


def set_user_state(user_id, state_dict):
    if user_id is None:
        return False
    data = _load_state()
    users = data.setdefault("users", {})
    if isinstance(state_dict, dict) and state_dict:
        users[str(user_id)] = state_dict
    else:
        users.pop(str(user_id), None)
    return _save_json_atomic(STATE_FILE, data)


def clear_user_state(user_id):
    return set_user_state(user_id, {})


def get_bot_offset():
    return int(_load_state().get("offset", 0) or 0)


def set_bot_offset(offset):
    data = _load_state()
    data["offset"] = int(offset or 0)
    return _save_json_atomic(STATE_FILE, data)


def read_json(path, default=None):
    return _load_json(path, default if default is not None else {})


def write_json_atomic(path, data):
    return _save_json_atomic(path, data)


def save_blocked_macs(mac_list):
    _ensure_persist_dir()
    return _save_json_atomic(BLOCKED_FILE, [str(m).upper() for m in mac_list])


def load_blocked_macs():
    values = _load_json(BLOCKED_FILE, [])
    if not isinstance(values, list):
        return []
    return sorted({str(m).upper() for m in values if m})


def now_ts():
    return int(time.time())
