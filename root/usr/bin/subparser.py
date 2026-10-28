#!/usr/bin/env python3
# -*- coding: utf-8 -*-
import sys
import os
import time
import json
import socket
import ssl
import gzip
import base64
import gc
import fcntl
import html
import shutil
import subprocess
import tempfile
import threading
import traceback
import urllib.request
import urllib.parse
from collections import deque
from concurrent.futures import ThreadPoolExecutor, as_completed

sys.path.insert(0, "/usr/lib")
try:
    from subparser.config import (UA_MAP, make_ssl_context, get_uci_bool,
                                  read_json, write_json_atomic,
                                  PROTOCOL_CHOICES, normalize_protocol, get_uci_list)
except Exception:
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

    def make_ssl_context(insecure=False):
        ctx = ssl.create_default_context()
        if insecure:
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
        return ctx

    def get_uci_bool(opt, default=False):
        return False

    def read_json(path, default=None):
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return default if default is not None else {}

    def write_json_atomic(path, data):
        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False)
            return True
        except Exception:
            return False

    PROTOCOL_CHOICES = {
        "vless": "VLESS", "trojan": "Trojan", "hysteria2": "Hysteria2",
        "ss": "Shadowsocks", "socks4": "SOCKS4", "socks4a": "SOCKS4a",
        "socks5": "SOCKS5",
    }
    PROTOCOL_ALIASES = {"hy2": "hysteria2"}

    def normalize_protocol(name):
        value = str(name or "").strip().lower()
        return PROTOCOL_ALIASES.get(value, value)

    def get_uci_list(opt):
        return []

try:
    from subparser.podkop import (capture_selection, restore_selection,
                                  fetch_proxies,
                                  get_proxies as get_podkop_proxies)
except Exception:
    def get_podkop_proxies():
        return {}

    def fetch_proxies():
        return {}, None

    def capture_selection(section, links, proxies=None):
        return "unknown", None

    def restore_selection(section, mode, link, new_links):
        return "unchanged"

CONFIG_NAME = "subparser"
SINGBOX_BIN = "/usr/bin/sing-box"
TEST_URL = "https://www.gstatic.com/generate_204"
LOCK_FILE = "/var/run/subparser.lock"
METRICS_FILE = "/tmp/subparser_metrics.json"
LAST_SELECTED_FILE = "/tmp/subparser_last_selected.json"
PODKOP_BACKUP = "/tmp/subparser_podkop.bak"
SECTIONS_SEEN_FILE = "/etc/subparser/sections_seen.json"
DEFAULT_HWID = "0123456789abcdef"
SYNC_LOG_FILE = "/tmp/subparser_sync.log"
RUN_LOG_KEEP_LINES = 200
_RUN_LOG = {"fp": None, "stdout": None, "stderr": None}


class _TeeStream:
    """Пишет в консоль и в файл одновременно."""

    def __init__(self, console, log_fp, lock):
        self._console = console
        self._log = log_fp
        self._lock = lock

    def write(self, data):
        with self._lock:
            for stream in (self._console, self._log):
                try:
                    stream.write(data)
                    stream.flush()
                except Exception:
                    pass
        return len(data)

    def flush(self):
        with self._lock:
            for stream in (self._console, self._log):
                try:
                    stream.flush()
                except Exception:
                    pass

    def isatty(self):
        try:
            return self._console.isatty()
        except Exception:
            return False

    def fileno(self):
        return self._console.fileno()

    @property
    def encoding(self):
        return getattr(self._console, "encoding", "utf-8")

    @property
    def errors(self):
        return getattr(self._console, "errors", "replace")


def _detect_launch_source():
    """Откуда запущен парсер: telegram, luci, watchdog, cron или console."""
    trigger = os.environ.get("SUBPARSER_TRIGGER", "").strip()
    if trigger:
        return trigger
    try:
        with open(f"/proc/{os.getppid()}/cmdline", "rb") as f:
            cmd = f.read().replace(b"\0", b" ").decode("utf-8", "ignore").lower()
    except Exception:
        return "console"
    if "subparser-bot" in cmd:
        return "telegram"
    if "subparser-helper" in cmd:
        return "luci"
    if "subparser-watchdog" in cmd:
        return "watchdog"
    if "crond" in cmd or "cron" in cmd:
        return "cron"
    return "console"


def _trim_run_log():
    """Оставляет хвост прошлого запуска, чтобы /tmp не разрастался."""
    try:
        if not os.path.exists(SYNC_LOG_FILE):
            return
        with open(SYNC_LOG_FILE, "r", encoding="utf-8", errors="ignore") as f:
            tail = list(deque(f, maxlen=RUN_LOG_KEEP_LINES))
        tmp = SYNC_LOG_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.writelines(tail)
        os.replace(tmp, SYNC_LOG_FILE)
    except Exception:
        pass


def _append_run_log(text):
    try:
        with open(SYNC_LOG_FILE, "a", encoding="utf-8") as f:
            f.write(text.rstrip() + "\n")
    except Exception:
        pass


def _setup_run_log():
    """Включает лог для любого способа запуска, а не только для LuCI."""
    _trim_run_log()
    try:
        log_fp = open(SYNC_LOG_FILE, "a", encoding="utf-8", buffering=1)
    except Exception:
        return None
    lock = threading.Lock()
    _RUN_LOG["fp"] = log_fp
    _RUN_LOG["stdout"] = sys.stdout
    _RUN_LOG["stderr"] = sys.stderr
    sys.stdout = _TeeStream(sys.stdout, log_fp, lock)
    sys.stderr = _TeeStream(sys.stderr, log_fp, lock)
    print(f"=== Запуск синхронизации: {time.strftime('%d.%m.%Y %H:%M:%S')} | "
          f"источник: {_detect_launch_source()} | pid {os.getpid()} ===")
    return log_fp


def _teardown_run_log():
    try:
        sys.stdout.flush()
        sys.stderr.flush()
    except Exception:
        pass
    log_fp = _RUN_LOG.get("fp")
    if _RUN_LOG.get("stdout") is not None:
        sys.stdout = _RUN_LOG["stdout"]
    if _RUN_LOG.get("stderr") is not None:
        sys.stderr = _RUN_LOG["stderr"]
    if log_fp:
        try:
            log_fp.close()
        except Exception:
            pass
    _RUN_LOG.update({"fp": None, "stdout": None, "stderr": None})

try:
    # 12 параллельных замеров — как в прежних версиях. Привязка к числу ядер
    # (2 ядра → 4 воркера) втрое увеличивала время анализа: замеры в основном
    # ждут сеть, а не CPU.
    MAX_WORKERS = 12
except Exception:
    MAX_WORKERS = 12

UA_ALIASES = {k.lower(): v for k, v in UA_MAP.items()}


PROGRESS_FILE = "/tmp/subparser_progress.json"
_PROGRESS_LAST = {"ts": 0.0, "text": ""}


def update_live_progress(step_title, step_desc):
    try:
        if not os.path.exists(PROGRESS_FILE):
            return
        with open(PROGRESS_FILE, "r") as pf:
            pdata = json.load(pf)
        token = pdata.get("token")
        chat_id = pdata.get("chat_id")
        msg_id = pdata.get("msg_id")
        started = int(pdata.get("started", 0) or 0)
        if not token or not chat_id or not msg_id:
            return
        # Не редактируем «старое» окно от предыдущего ручного запуска.
        if started and (time.time() - started) > 3600:
            return
        text_key = f"{step_title}|{step_desc}"
        now = time.time()
        if text_key == _PROGRESS_LAST["text"] and (now - _PROGRESS_LAST["ts"]) < 2.5:
            return
        if (now - _PROGRESS_LAST["ts"]) < 2.0:
            return
        _PROGRESS_LAST["ts"] = now
        _PROGRESS_LAST["text"] = text_key

        url = f"https://api.telegram.org/bot{token}/editMessageText"
        body = (
            "▶️ <b>Синхронизация по текущей конфигурации</b>\n\n"
            f"<b>{step_title}</b>\n"
            f"<i>{step_desc}</i>"
        )
        # Кнопки принудительно скрыты: окно строго информационное
        payload = json.dumps({
            "chat_id": chat_id,
            "message_id": msg_id,
            "text": body,
            "parse_mode": "HTML",
            "reply_markup": {"inline_keyboard": []}
        }).encode("utf-8")

        req = urllib.request.Request(url, data=payload, headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=3, context=make_ssl_context()):
            pass
    except Exception:
        pass

def send_telegram_notify(text):
    try:
        token = subprocess.check_output(["uci", "-q", "get", f"{CONFIG_NAME}.settings.tg_bot_token"], text=True).strip()
        chat_id = subprocess.check_output(["uci", "-q", "get", f"{CONFIG_NAME}.settings.tg_chat_id"], text=True).strip()
        if not token or not chat_id:
            return

        ctx = make_ssl_context()

        url = f"https://api.telegram.org/bot{token}/sendMessage"
        data = urllib.parse.urlencode({"chat_id": chat_id, "text": text, "parse_mode": "HTML"}).encode("utf-8")

        print("[*] Ожидание готовности сети для отправки отчета в Telegram...")
        time.sleep(4)

        max_attempts = 20
        retry_delay = 10

        for attempt in range(1, max_attempts + 1):
            try:
                req = urllib.request.Request(url, data=data, headers={"User-Agent": "OpenWrt-SubParser"})
                with urllib.request.urlopen(req, timeout=6, context=ctx) as resp:
                    if resp.status == 200:
                        print(f"[OK] Уведомление успешно доставлено в Telegram (попытка {attempt}/{max_attempts}).")
                        return
            except Exception as net_err:
                if attempt < max_attempts:
                    print(f"  [!] Нет связи с Telegram ({net_err}). Повтор через {retry_delay}с...")
                    time.sleep(retry_delay)

        print("[!] Превышен лимит ожидания. Уведомление не доставлено.")
    except Exception as e:
        print(f"[!] Ошибка отправки: {e}")

def find_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(('127.0.0.1', 0))
        return s.getsockname()[1]

def get_uci(option, default=""):
    try:
        out = subprocess.check_output(["uci", "-q", "get", f"{CONFIG_NAME}.settings.{option}"], text=True).strip()
        return out if out else default
    except Exception:
        return default


def link_protocol(link):
    """Каноническое имя протокола ссылки (hy2 -> hysteria2)."""
    try:
        return normalize_protocol(urllib.parse.urlsplit(link).scheme)
    except Exception:
        return ""


def get_excluded_protocols():
    """Протоколы из UCI-списка exclude_protocols."""
    return {normalize_protocol(item) for item in get_uci_list("exclude_protocols") if item}


def filter_excluded_protocols(links, excluded):
    """Убирает ссылки исключённых протоколов до дедупликации и скрининга."""
    if not excluded:
        return list(links), 0
    kept = []
    dropped = 0
    for link in links:
        if link_protocol(link) in excluded:
            dropped += 1
        else:
            kept.append(link)
    return kept, dropped

def get_real_podkop_sections() -> list:
    # Единая реализация с ботом и LuCI: подкоп хранит секции как `config section`,
    # поэтому определяем их по типу секции/опциям, а не по подстроке `=podkop`.
    try:
        sys.path.insert(0, "/usr/lib")
        from subparser.podkop import get_all_podkop_sections
        return get_all_podkop_sections()
    except Exception:
        pass
    try:
        raw_secs = subprocess.check_output(["uci", "-q", "show", "podkop"], text=True,
                                           stderr=subprocess.DEVNULL)
        detected = []
        for line in raw_secs.splitlines():
            line = line.strip()
            if not line.startswith("podkop.") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            parts = key.split(".")
            if len(parts) == 2:
                if value.strip().strip("'\"") in ("section", "podkop") and parts[1] != "settings":
                    detected.append(parts[1])
            elif len(parts) >= 3 and parts[2] in ("urltest_proxy_links", "connection_type", "proxy_string"):
                detected.append(parts[1])
        return list(dict.fromkeys(detected)) if detected else ["main"]
    except Exception:
        return ["main"]

def _save_target_sections(sections) -> bool:
    try:
        subprocess.run(["uci", "-q", "delete", f"{CONFIG_NAME}.settings.target_section"],
                       check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        for s in sections:
            subprocess.run(["uci", "-q", "add_list", f"{CONFIG_NAME}.settings.target_section={s}"],
                           check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        subprocess.run(["uci", "commit", CONFIG_NAME], check=False,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return True
    except Exception:
        return False

def get_target_sections() -> list:
    real = get_real_podkop_sections()
    try:
        raw = subprocess.check_output(["uci", "-q", "get", f"{CONFIG_NAME}.settings.target_section"], text=True).strip()
        sections = [s.strip() for s in raw.split() if s.strip()]
        # Не добавляем автоматически все секции: пользователь мог осознанно
        # оставить только часть. Пустой список трактуем как «все».
        valid = [s for s in sections if s in real]
    except Exception:
        valid = []
        sections = []

    # Новые секции Podkop (появившиеся после последнего запуска) подхватываем
    # сами, иначе вторая секция остаётся без узлов. Ранее удалённые вручную
    # секции помним в sections_seen.json и повторно не навязываем.
    try:
        seen_data = read_json(SECTIONS_SEEN_FILE, {})
        seen = seen_data.get("sections", []) if isinstance(seen_data, dict) else []
        if not isinstance(seen, list):
            seen = []
        seen = [str(s) for s in seen]
        new_sections = [s for s in real if s not in seen and s not in valid]
        if valid and new_sections:
            valid = valid + new_sections
            _save_target_sections(valid)
            print(f"[i] Новые секции Podkop добавлены в target_section: {', '.join(new_sections)}")
        merged_seen = sorted(set(seen) | set(real))
        if merged_seen != sorted(set(seen)):
            write_json_atomic(SECTIONS_SEEN_FILE, {"sections": merged_seen})
    except Exception:
        pass

    return valid if valid else real

def get_subscriptions():
    try:
        raw = subprocess.check_output(["uci", "-q", "export", CONFIG_NAME], text=True)
    except Exception:
        return []
    
    subs = []
    current = None
    for line in raw.splitlines():
        line = line.strip()
        if line.startswith("config subscription"):
            if current and current.get("url"):
                subs.append(current)
            current = {"enabled": "1", "user_agent": "v2rayN", "name": "Подписка"}
        elif current is not None:
            if line.startswith("option url "):
                current["url"] = line.split("option url ", 1)[1].strip("'\" ")
            elif line.startswith("option user_agent "):
                current["user_agent"] = line.split("option user_agent ", 1)[1].strip("'\" ")
            elif line.startswith("option enabled "):
                current["enabled"] = line.split("option enabled ", 1)[1].strip("'\" ")
            elif line.startswith("option custom_ua "):
                current["custom_ua"] = line.split("option custom_ua ", 1)[1].strip("'\" ")
            elif line.startswith("option device_id "):
                current["device_id"] = line.split("option device_id ", 1)[1].strip("'\" ")
            elif line.startswith("option device_model "):
                current["device_model"] = line.split("option device_model ", 1)[1].strip("'\" ")
            elif line.startswith("option device_os "):
                current["device_os"] = line.split("option device_os ", 1)[1].strip("'\" ")
            elif line.startswith("option os_version "):
                current["os_version"] = line.split("option os_version ", 1)[1].strip("'\" ")
            elif line.startswith("option name "):
                current["name"] = line.split("option name ", 1)[1].strip("'\" ")
    if current and current.get("url"):
        subs.append(current)
    return subs

def is_ru_node(name: str) -> bool:
    nl = name.lower()
    return "🇷🇺" in name or "\U0001F1F7\U0001F1FA" in name or "обход" in nl or any(x in nl for x in [" ru ", "russia", "россия"])

GLOBAL_SUB_USERINFO = {}

def format_bytes(size):
    for unit in ['B', 'KB', 'MB', 'GB', 'TB']:
        if abs(size) < 1024.0:
            return f"{size:.1f} {unit}"
        size /= 1024.0
    return f"{size:.1f} PB"

def parse_userinfo_header(info_str):
    res = {}
    for part in info_str.split(';'):
        if '=' in part:
            k, v = part.strip().split('=', 1)
            try: res[k.lower()] = int(v)
            except Exception: pass
    return res

def fetch_single_sub(sub_info: dict) -> list:
    url = sub_info.get("url", "").strip()
    if not url:
        return []

    ua_key = sub_info.get("user_agent", "v2rayN")
    custom_ua = sub_info.get("custom_ua", "").strip()
    if ua_key == "custom" and custom_ua:
        ua = custom_ua
    else:
        ua = UA_MAP.get(ua_key) or UA_ALIASES.get(str(ua_key).lower()) or UA_MAP["v2rayN"]

    headers = {
        "User-Agent": ua,
        "Accept": "*/*",
        "Accept-Encoding": "gzip, deflate",
        "Accept-Language": "ru-RU,ru;q=0.9"
    }

    # Если включена кастомная эмуляция конкретного устройства
    dev_model = ""
    if ua_key == "custom":
        hwid = sub_info.get("device_id", "").strip()
        dev_model = sub_info.get("device_model", "").strip()
        dev_os = sub_info.get("device_os", "").strip() or "Android"
        dev_ver = sub_info.get("os_version", "").strip() or "15"

        if hwid:
            headers["x-hwid"] = hwid
            headers["hwid"] = hwid
            headers["device-id"] = hwid
        if dev_model:
            headers["x-device-model"] = dev_model
            headers["device-model"] = dev_model
        if dev_os:
            headers["x-device-os"] = dev_os
        if dev_ver:
            headers["x-ver-os"] = dev_ver

    name = sub_info.get("name", "Подписка")
    update_live_progress("⏳ [1/4] Загрузка подписок...", f"Получение: {name}")
    ua_label = f"Свой ({dev_model})" if (ua_key == "custom" and dev_model) else (custom_ua[:15] if ua_key == "custom" else ua_key)
    print(f"[{name}] Запрос подписки через {ua_label}...")

    raw_data = None
    try:
        ctx = make_ssl_context()
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=15, context=ctx) as resp:
            raw_data = resp.read()
            u_info = resp.headers.get("subscription-userinfo") or resp.headers.get("Subscription-Userinfo")
            if u_info:
                GLOBAL_SUB_USERINFO[name] = parse_userinfo_header(u_info)
            if resp.info().get("Content-Encoding") == "gzip" or raw_data[:2] == b"\x1f\x8b":
                try:
                    raw_data = gzip.decompress(raw_data)
                except Exception:
                    pass
    except Exception as e:
        print(f"  [!] urllib error ({e}), пробуем curl...")

    if not raw_data:
        header_file = None
        try:
            fd, header_file = tempfile.mkstemp(prefix="subparser_hdr_", dir="/tmp")
            os.close(fd)
            curl_cmd = [
                "curl", "-sL", "-m", "15", "-D", header_file,
                "-H", f"User-Agent: {ua}"
            ]
            if not get_uci_bool("allow_insecure_tls", False):
                curl_cmd.insert(1, "--fail")
            else:
                curl_cmd.insert(1, "-k")
            if ua_key == "custom":
                hwid = sub_info.get("device_id", "").strip()
                dev_model_c = sub_info.get("device_model", "").strip()
                dev_os_c = sub_info.get("device_os", "").strip() or "Android"
                dev_ver_c = sub_info.get("os_version", "").strip()
                if hwid:
                    curl_cmd.extend(["-H", f"device-id: {hwid}", "-H", f"x-hwid: {hwid}", "-H", f"hwid: {hwid}"])
                if dev_model_c:
                    curl_cmd.extend(["-H", f"x-device-model: {dev_model_c}", "-H", f"device-model: {dev_model_c}"])
                curl_cmd.extend(["-H", f"x-device-os: {dev_os_c}"])
                if dev_ver_c:
                    curl_cmd.extend(["-H", f"x-ver-os: {dev_ver_c}"])
            curl_cmd.extend(["--compressed", url])
            res = subprocess.run(curl_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            if res.returncode == 0 and res.stdout:
                raw_data = res.stdout
                if header_file and os.path.exists(header_file):
                    try:
                        with open(header_file, "r", errors="ignore") as hf:
                            for hline in hf:
                                if "subscription-userinfo:" in hline.lower():
                                    u_val = hline.split(":", 1)[1].strip()
                                    GLOBAL_SUB_USERINFO[name] = parse_userinfo_header(u_val)
                                    break
                    except Exception:
                        pass
        except Exception:
            pass
        finally:
            if header_file:
                try:
                    os.remove(header_file)
                except Exception:
                    pass

    if not raw_data:
        print(f"  [!] Не удалось загрузить подписку {name}")
        return []

    raw_text = raw_data.decode('utf-8', errors='ignore')
    if "://" in raw_text or "proxies:" in raw_text:
        text = raw_text
    else:
        try:
            padded = raw_data + b"=" * ((4 - len(raw_data) % 4) % 4)
            decoded = base64.b64decode(padded).decode('utf-8', errors='ignore')
            text = decoded if ("://" in decoded or "proxies:" in decoded) else raw_text
        except Exception:
            text = raw_text

    t_low = text.lower()
    if "не поддерживается" in t_low or "update your client" in t_low:
        print(f"  [!] Сервер вернул заглушку для {name}.")
        return []

    found = []
    for l in text.splitlines():
        l_clean = l.strip()
        if "://" in l_clean and "0.0.0.0" not in l_clean:
            # Оставляем ссылку нетронутой, включая хэш #имя
            found.append(l_clean)

    print(f"  [OK] Из подписки '{name}' получено {len(found)} боевых узлов.")
    return found

def query_node_delay(api_port: int, tag: str, timeout_ms: int = 3500) -> int:
    encoded_tag = urllib.parse.quote(tag)
    encoded_url = urllib.parse.quote(TEST_URL, safe="")
    api_url = f"http://127.0.0.1:{api_port}/proxies/{encoded_tag}/delay?url={encoded_url}&timeout={timeout_ms}"
    try:
        req = urllib.request.Request(api_url, headers={"Connection": "close"})
        with urllib.request.urlopen(req, timeout=(timeout_ms / 1000.0) + 1.0) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            return int(data.get("delay", -1))
    except Exception:
        return -1

def query_group_delays(api_port: int, group_tag: str, timeout_ms: int = 3000) -> dict:
    """Один запрос: sing-box сам параллельно опрашивает все узлы группы."""
    encoded_tag = urllib.parse.quote(group_tag)
    encoded_url = urllib.parse.quote(TEST_URL, safe="")
    api_url = f"http://127.0.0.1:{api_port}/group/{encoded_tag}/delay?url={encoded_url}&timeout={timeout_ms}"
    try:
        req = urllib.request.Request(api_url)
        with urllib.request.urlopen(req, timeout=(timeout_ms / 1000.0) + 25.0) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            if isinstance(data, dict):
                return {str(k): int(v) for k, v in data.items() if isinstance(v, (int, float))}
    except Exception:
        pass
    return {}


VALID_FPS = {"chrome", "firefox", "safari", "ios", "android", "edge", "360", "qq", "random", "randomized"}

def sanitize_link_fp(link: str, target_fp: str) -> str:
    try:
        parsed = urllib.parse.urlparse(link)
        query = urllib.parse.parse_qs(parsed.query)
        query["fp"] = [target_fp]
        new_query = urllib.parse.urlencode(query, doseq=True)
        return urllib.parse.urlunparse(parsed._replace(query=new_query))
    except Exception:
        return link

# Методы Shadowsocks, которые понимает sing-box. Неизвестный метод ломает
# `sing-box check` целиком, поэтому список проверяем заранее.
SS_METHODS = {
    "none",
    "aes-128-gcm", "aes-192-gcm", "aes-256-gcm",
    "chacha20-ietf-poly1305", "xchacha20-ietf-poly1305",
    "2022-blake3-aes-128-gcm", "2022-blake3-aes-256-gcm",
    "2022-blake3-chacha20-poly1305",
    "aes-128-ctr", "aes-192-ctr", "aes-256-ctr",
    "rc4-md5",
    "aes-128-cfb", "aes-192-cfb", "aes-256-cfb",
    "chacha20-ietf", "xchacha20",
}

# Схемы SOCKS и соответствующие им значения `version` в sing-box.
SOCKS_SCHEMES = {"socks4": "4", "socks4a": "4a", "socks5": "5"}


def _split_host_port(host_port: str, default_port: int = 443):
    host_port = (host_port or "").strip()
    if host_port.startswith("["):
        host, _, rest = host_port.partition("]")
        port_str = rest[1:] if rest.startswith(":") else ""
        return host[1:], int(port_str) if port_str.isdigit() else default_port
    if ":" in host_port:
        host, _, port_str = host_port.rpartition(":")
        if port_str.isdigit():
            return host, int(port_str)
        return host_port, default_port
    return host_port, default_port


def _b64_decode_text(value: str) -> str:
    value = urllib.parse.unquote(value).strip()
    value = value.replace("-", "+").replace("_", "/")
    value += "=" * ((4 - len(value) % 4) % 4)
    try:
        return base64.b64decode(value).decode("utf-8")
    except Exception:
        raise ValueError("invalid base64")


def _ss_credentials(parsed):
    """Разбирает ss-ссылку в (method, password, host, port).

    Поддержаны SIP002 (`ss://userinfo@host:port`, userinfo — base64 или
    `method:password`) и legacy (`ss://base64(method:password@host:port)`).
    """
    netloc = parsed.netloc

    if "@" in netloc:
        userinfo, host_port = netloc.rsplit("@", 1)
        userinfo = urllib.parse.unquote(userinfo)
        if ":" in userinfo:
            method, password = userinfo.split(":", 1)
        else:
            decoded_userinfo = _b64_decode_text(userinfo)
            if ":" not in decoded_userinfo:
                raise ValueError("invalid ss userinfo")
            method, password = decoded_userinfo.split(":", 1)
        server, port = _split_host_port(host_port)
    else:
        token = netloc + parsed.path
        try:
            decoded = _b64_decode_text(token)
        except ValueError:
            decoded = ""
        if "@" not in decoded:
            token = token.rstrip("/")
            decoded = _b64_decode_text(token) if token else ""
        if "@" not in decoded:
            raise ValueError("invalid ss link")
        userinfo, host_port = decoded.rsplit("@", 1)
        method, password = userinfo.split(":", 1)
        server, port = _split_host_port(host_port)

    return (method or "").strip().lower(), password or "", server, port


# Символы, которые Podkop разбирает в userinfo без base64.
_SS_PLAIN_CHARS = set("abcdefghijklmnopqrstuvwxyz"
                      "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
                      "0123456789._~-:")


def _normalize_ss_link(parsed):
    """Приводит ss-ссылку к виду SIP002, который понимает Podkop.

    Podkop умеет только `ss://userinfo@host:port`, поэтому legacy-ссылки
    (`ss://base64(method:password@host:port)`) нужно переписать, иначе он
    примет base64-строку за хост. `+` в base64 экранируем: Podkop сначала
    делает url-decode и иначе превратит его в пробел.
    """
    method, password, server, port = _ss_credentials(parsed)
    userinfo = f"{method}:{password}"
    if all(c in _SS_PLAIN_CHARS for c in userinfo):
        encoded = userinfo
    else:
        encoded = base64.b64encode(userinfo.encode("utf-8")).decode("ascii")
        encoded = encoded.replace("+", "%2B")
    host = f"[{server}]" if ":" in server else server
    return f"ss://{encoded}@{host}:{port}"


def _parse_ss_outbound(parsed, query, tag: str) -> dict:
    """Shadowsocks: SIP002 (`ss://userinfo@host:port`) и legacy (`ss://base64`)."""
    method, password, server, port = _ss_credentials(parsed)
    password = password or ""
    if not server or not password:
        raise ValueError("invalid ss link")
    if method not in SS_METHODS:
        raise ValueError(f"unsupported shadowsocks method: {method}")

    plugin = query.get("plugin", [""])[0]
    if plugin:
        # Podkop не умеет пробрасывать plugin/plugin_opts в outbound, поэтому
        # такой узел после записи в Podkop просто не заработает.
        raise ValueError(f"ss plugin is not supported: {plugin.split(';')[0]}")

    if method.startswith("2022-blake3"):
        try:
            key = base64.b64decode(password + "=" * (-len(password) % 4))
        except Exception:
            key = b""
        if len(key) not in (16, 32):
            raise ValueError("invalid 2022-blake3 key")

    return {
        "type": "shadowsocks",
        "tag": tag,
        "server": server,
        "server_port": port,
        "method": method,
        "password": password,
    }


def _parse_socks_outbound(parsed, tag: str) -> dict:
    scheme = parsed.scheme.lower()
    netloc = parsed.netloc
    userinfo = ""
    if "@" in netloc:
        userinfo, host_port = netloc.rsplit("@", 1)
        userinfo = urllib.parse.unquote(userinfo)
    else:
        host_port = netloc
    server, port = _split_host_port(host_port, default_port=1080)
    if not server:
        raise ValueError("invalid socks link")

    outbound = {
        "type": "socks",
        "tag": tag,
        "server": server,
        "server_port": port,
        "version": SOCKS_SCHEMES[scheme],
    }
    if SOCKS_SCHEMES[scheme] == "5" and userinfo:
        username, _, password = userinfo.partition(":")
        outbound["username"] = username
        if password:
            outbound["password"] = password
    return outbound


def parse_link_to_singbox_outbound(link: str, tag: str) -> dict:
    parsed = urllib.parse.urlsplit(link)
    scheme = parsed.scheme.lower()
    query = urllib.parse.parse_qs(parsed.query)

    if scheme == "ss":
        return _parse_ss_outbound(parsed, query, tag)
    if scheme in SOCKS_SCHEMES:
        return _parse_socks_outbound(parsed, tag)

    netloc = parsed.netloc
    auth, host_port = "", netloc
    if "@" in netloc:
        auth, host_port = netloc.split("@", 1)

    if host_port.startswith("["):
        parts = host_port.split("]")
        server = parts[0][1:]
        port = int(parts[1][1:]) if len(parts) > 1 and parts[1].startswith(":") else 443
    elif ":" in host_port:
        parts = host_port.split(":")
        server = parts[0]
        port = int(parts[1]) if parts[1].isdigit() else 443
    else:
        server = host_port
        port = 443

    if scheme not in ("vless", "trojan", "hysteria2", "hy2"):
        raise ValueError(f"unsupported scheme: {scheme}")

    outbound = {
        "type": "vless" if scheme == "vless" else ("trojan" if scheme == "trojan" else "hysteria2"),
        "tag": tag,
        "server": server,
        "server_port": port
    }

    sni = query.get("sni", [query.get("peer", [server])[0]])[0] or server
    alpn_raw = query.get("alpn", [""])[0]
    alpn = [p.strip() for p in alpn_raw.split(",") if p.strip()] if alpn_raw else []
    insecure = query.get("allowInsecure", ["0"])[0] in ("1", "true")

    override_fp = get_uci("override_fp", "as_is").lower().strip()
    valid_fps = ("chrome", "firefox", "safari", "ios", "android", "edge", "360", "qq", "random", "randomized")

    if override_fp in valid_fps:
        fp = override_fp
    else:
        raw_fp = query.get("fp", [""])[0].lower().strip()
        fp = raw_fp if raw_fp in valid_fps else "chrome"

    transport_type = query.get("type", [query.get("net", ["tcp"])[0]])[0].lower()
    transport_obj = None

    if transport_type == "ws":
        transport_obj = {"type": "ws", "path": query.get("path", ["/"])[0], "headers": {"Host": query.get("host", [sni])[0]}}
    elif transport_type in ("grpc", "gun"):
        transport_obj = {"type": "grpc", "service_name": query.get("serviceName", [query.get("path", [""])[0]])[0]}
    elif transport_type in ("http", "tcp") and query.get("headerType", ["none"])[0].lower() == "http":
        transport_obj = {"type": "http", "host": [query.get("host", [sni])[0]], "path": query.get("path", ["/"])[0]}
    elif transport_type in ("httpupgrade", "upgrade"):
        transport_obj = {"type": "httpupgrade", "host": query.get("host", [sni])[0], "path": query.get("path", ["/"])[0]}
    elif transport_type in ("xhttp", "splithttp"):
        mode = query.get("mode", ["auto"])[0]
        transport_obj = {"type": "xhttp", "host": query.get("host", [sni])[0], "path": query.get("path", ["/"])[0], "mode": mode, "x_padding_bytes": query.get("x_padding_bytes", query.get("padding", ["100-500"]))[0]}

    if scheme == "vless":
        outbound["uuid"] = auth
        raw_flow = query.get("flow", [""])[0]
        if isinstance(raw_flow, str):
            flow = raw_flow.replace("-udp443", "")
            if flow:
                outbound["flow"] = flow
        sec = query.get("security", [""])[0].lower()
        if sec == "reality":
            outbound["tls"] = {
                "enabled": True, "server_name": sni,
                "reality": {"enabled": True, "public_key": query.get("pbk", [query.get("publicKey", [""])[0]])[0], "short_id": query.get("sid", [query.get("shortId", [""])[0]])[0]},
                "utls": {"enabled": True, "fingerprint": fp}
            }
        elif sec in ("tls", "ssl"):
            tls_cfg = {"enabled": True, "server_name": sni, "insecure": insecure, "utls": {"enabled": True, "fingerprint": fp}}
            if alpn: tls_cfg["alpn"] = alpn
            outbound["tls"] = tls_cfg
        if transport_obj: outbound["transport"] = transport_obj

    elif scheme == "trojan":
        outbound["password"] = auth
        tls_cfg = {"enabled": True, "server_name": sni, "insecure": insecure, "utls": {"enabled": True, "fingerprint": fp}}
        if alpn: tls_cfg["alpn"] = alpn
        outbound["tls"] = tls_cfg
        if transport_obj: outbound["transport"] = transport_obj

    elif scheme in ("hysteria2", "hy2"):
        outbound["password"] = auth
        hy2_sni = sni if sni else server
        tls_cfg = {"enabled": True, "server_name": hy2_sni, "insecure": insecure, "alpn": alpn if alpn else ["h3"]}
        outbound["tls"] = tls_cfg
        obfs_type = query.get("obfs", [""])[0]
        if obfs_type: outbound["obfs"] = {"type": obfs_type, "password": query.get("obfs-password", [""])[0]}

    return outbound

def link_fingerprint(link: str) -> str:
    p = urllib.parse.urlsplit(link)
    return f"{p.scheme}://{p.netloc}{p.path}?{p.query}"

def sanitize_podkop_link(link: str) -> str:
    try:
        parsed = urllib.parse.urlsplit(link)
        scheme = parsed.scheme.lower()
        query = urllib.parse.parse_qs(parsed.query, keep_blank_values=False)

        # Shadowsocks приводим к SIP002 (иначе Podkop примет base64 за хост),
        # SOCKS не трогаем: им не нужны type/security.
        if scheme == "ss":
            try:
                return _normalize_ss_link(parsed)
            except Exception:
                return link
        if scheme in SOCKS_SCHEMES:
            return link

        if scheme in ("hysteria2", "hy2"):
            # Для Hysteria 2 нельзя ставить security=none или type=tcp!
            if "security" in query and query["security"][0].lower() in ("", "none"):
                del query["security"]
            clean_query = urllib.parse.urlencode(query, doseq=True)
            return urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, parsed.path, clean_query, parsed.fragment))

        # 1. Исправление type / transport для VLESS / Trojan
        transport = query.get("type", query.get("net", [""]))[0].lower().strip()
        if not transport or transport == "none":
            transport = "tcp"
        query["type"] = [transport]
        if "net" in query:
            del query["net"]

        # 2. Исправление security
        security = query.get("security", [""])[0].lower().strip()
        if not security:
            query["security"] = ["none"]
        else:
            query["security"] = [security]

        # 3. Сборка валидного query string без пустых ключей
        clean_query = urllib.parse.urlencode(query, doseq=True)
        return urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, parsed.path, clean_query, parsed.fragment))
    except Exception:
        return link


def _acquire_lock():
    try:
        lock_fd = open(LOCK_FILE, "a+")
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        lock_fd.seek(0)
        lock_fd.truncate()
        lock_fd.write(str(os.getpid()))
        lock_fd.flush()
        return lock_fd
    except Exception:
        return None


def _release_lock(lock_fd):
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
        lock_fd.close()
    except Exception:
        pass


def _restore_podkop_backup():
    try:
        subprocess.run(["uci", "-q", "revert", "podkop"], check=False,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if os.path.exists(PODKOP_BACKUP):
            shutil.copy2(PODKOP_BACKUP, "/etc/config/podkop")
        subprocess.run(["uci", "commit", "podkop"], check=False,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        subprocess.run(["/etc/init.d/podkop", "restart"], check=False,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return True
    except Exception as e:
        print(f"[!] Ошибка отката Podkop: {e}")
        return False


def _wait_podkop_ready(timeout=240):
    """Ждёт не только процесс sing-box, но и готовый Clash API.

    При большом числе правил процесс появляется раньше, чем API начинает
    отвечать. Без этой проверки восстановление выбора и отчёт уходят слишком
    рано, и получается ложное «не удалось вернуть активный узел».
    """
    deadline = time.time() + timeout
    process_seen = False
    while time.time() < deadline:
        try:
            status = subprocess.run(["/etc/init.d/podkop", "status"],
                                    stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
            pid = subprocess.run(["pidof", "sing-box"], stdout=subprocess.PIPE,
                                 stderr=subprocess.DEVNULL, text=True).stdout.strip()
            if status.returncode == 0 and pid:
                process_seen = True
                proxies, api_status = fetch_proxies()
                if api_status in (200, 204) and proxies:
                    return True
        except Exception:
            pass
        time.sleep(2)
    # Podkop запущен, но Clash API так и не ответил (например, отключён).
    # Конфигурацию из-за этого не откатываем — она рабочая.
    return process_seen


def _escape_uci_batch_value(value):
    return (str(value).replace("\\", "\\\\")
            .replace('"', '\\"')
            .replace("\n", " ").replace("\r", " "))


def _write_section_links(section, links):
    commands = [f"delete podkop.{section}.urltest_proxy_links"]
    for link in links:
        commands.append(
            f'add_list podkop.{section}.urltest_proxy_links="{_escape_uci_batch_value(link)}"'
        )
    try:
        proc = subprocess.Popen(["uci", "-q", "batch"], stdin=subprocess.PIPE,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        out, err = proc.communicate(input="\n".join(commands) + "\n")
        if proc.returncode != 0:
            print(f"[!] uci batch ошибка для секции {section}: {err.strip()}")
            return False
        return True
    except Exception as e:
        print(f"[!] Не удалось записать секцию {section}: {e}")
        return False


def _prepare_link(link, results, idx):
    if "#" in link:
        base_url, raw_name = link.split("#", 1)
    else:
        base_url = link
        raw_name = results.get(link, {}).get("name", "")
    unquoted_name = urllib.parse.unquote(raw_name).strip()
    if not unquoted_name or unquoted_name.startswith("n_"):
        parsed = urllib.parse.urlsplit(base_url)
        host = parsed.hostname or f"Node-{idx+1}"
        unquoted_name = f"{host} | {parsed.scheme.upper()}"
    safe_name = urllib.parse.quote(unquoted_name, safe="")
    return f"{sanitize_podkop_link(base_url)}#{safe_name}"


def _link_set(links):
    return {link_fingerprint(x) for x in links}


def _run():
    enabled = get_uci("enabled", "0")
    if enabled != "1" and "--force" not in sys.argv:
        print("Сервис отключен в настройках.")
        return

    subs = get_subscriptions()
    active_subs = [s for s in subs if s.get("enabled", "1") == "1" and s.get("url")]
    if not active_subs:
        msg = "🔴 <b>SubParser Alert</b>\nНет активных подписок для парсинга. Конфигурация Podkop не изменена."
        print("Ошибка: Нет активных подписок для парсинга.")
        send_telegram_notify(msg)
        return

    threshold = int(get_uci("ping_threshold", "350"))
    max_best = int(get_uci("max_best_nodes", "0"))
    max_jitter = int(get_uci("max_jitter", "150"))
    filter_ru = get_uci("filter_ru", "1") == "1"
    per_section_config = get_uci("per_section_config", "0") == "1"
    excluded_protocols = get_excluded_protocols()

    section_targets = {}
    if per_section_config:
        detected = set(get_real_podkop_sections())

        for s in detected:
            s_en = get_uci(f"sec_en_{s}", "1") == "1"
            s_mode = get_uci(f"sec_mode_{s}", "replace")
            if s_en:
                section_targets[s] = s_mode
    else:
        g_mode = get_uci("update_mode", "replace")
        for s in get_target_sections():
            section_targets[s] = g_mode

    if not section_targets:
        section_targets["main"] = "replace"

    start_time = time.time()
    all_links = []
    seen = set()

    update_live_progress("⏳ [1/4] Загрузка подписок...", f"Источников: {len(active_subs)}")
    sub_results = {}
    with ThreadPoolExecutor(max_workers=min(4, len(active_subs))) as pool:
        futures = {pool.submit(fetch_single_sub, s): s for s in active_subs}
        for future in as_completed(futures):
            try:
                sub_results[futures[future].get("name", "Подписка")] = future.result()
            except Exception as e:
                print(f"[!] Подписка не загружена: {e}")

    excluded_total = 0
    for s in active_subs:
        for l in sub_results.get(s.get("name", "Подписка"), []):
            if link_protocol(l) in excluded_protocols:
                excluded_total += 1
                continue
            fp = link_fingerprint(l)
            if fp not in seen:
                seen.add(fp)
                all_links.append(l)

    if excluded_total:
        print(f"[i] Исключено фильтром протоколов: {excluded_total}")

    if not all_links:
        extra = f" Исключено фильтром протоколов: {excluded_total}." if excluded_total else ""
        msg = ("🔴 <b>SubParser Alert</b>\nБоевые узлы не найдены ни в одной подписке."
               f"{extra} Конфигурация Podkop не изменена.")
        print(f"Ошибка: Боевые узлы не найдены ни в одной подписке.{extra}")
        send_telegram_notify(msg)
        return

    print(f"\nВсего уникальных узлов для тестирования: {len(all_links)}")

    api_port = find_free_port()
    outbounds = []
    tag_map = {}
    for i, link in enumerate(all_links):
        tag = f"n_{i+1}"
        try:
            ob = parse_link_to_singbox_outbound(link, tag)
            outbounds.append(ob)
            tag_map[tag] = link
        except Exception as e:
            print(f"[SKIP] Неподдерживаемая или битая ссылка: {e}")
            continue

    if not tag_map:
        msg = "🔴 <b>SubParser Alert</b>\nНи одну ссылку не удалось разобрать. Конфигурация Podkop не изменена."
        print("Ошибка: не удалось разобрать ни одну ссылку.")
        send_telegram_notify(msg)
        return

    results = {}

    proc = None
    tmp_cfg = None
    gc.collect()
    outbounds.append({"type": "direct", "tag": "direct"})
    cfg = {
        "log": {"level": "error"},
        "experimental": {"clash_api": {"external_controller": f"127.0.0.1:{api_port}"}},
        "route": {"auto_detect_interface": True},
        "outbounds": outbounds,
    }

    tmp_cfg = f"/tmp/subparser_test_{api_port}.json"
    with open(tmp_cfg, "w") as f:
        json.dump(cfg, f, indent=2)

    check = subprocess.run([SINGBOX_BIN, "check", "-c", tmp_cfg],
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    if check.returncode != 0:
        print(f"[!] sing-box check: {check.stderr.strip()[:300]}")
        # Не прерываемся: некоторые сборки sing-box не поддерживают check.

    env_sb = os.environ.copy()
    env_sb["ENABLE_DEPRECATED_MISSING_DOMAIN_RESOLVER"] = "true"
    env_sb["ENABLE_DEPRECATED_OUTBOUND_DNS_RULE_ITEM"] = "true"
    proc = subprocess.Popen([SINGBOX_BIN, "run", "-c", tmp_cfg],
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env_sb)
    time.sleep(2.5)
    if proc.poll() is not None:
        _, err = proc.communicate()
        print(f"\n[ОШИБКА] sing-box не запустился на порту {api_port}:\n{err}")
        if tmp_cfg and os.path.exists(tmp_cfg):
            os.remove(tmp_cfg)
        send_telegram_notify(
            f"🔴 <b>SubParser Alert</b>\nНе удалось запустить тестовое ядро sing-box.\n"
            f"<code>{html.escape(str(err)[:400])}</code>"
        )
        return

    try:
        update_live_progress("🔍 [2/4] Скрининг узлов...",
                             f"Проверка отклика {len(tag_map)} серверов...")
        print(f"\n[Этап 1/2] Быстрый скрининг {len(tag_map)} узлов...")
        stage1_candidates = []

        def quick_probe(item):
            tag, link = item
            delays = []
            for i in range(2):
                d = query_node_delay(api_port, tag, timeout_ms=3000)
                if d > 0:
                    delays.append(d)
                if i == 0:
                    time.sleep(0.1)
            return tag, link, delays

        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
            futures = [executor.submit(quick_probe, item) for item in tag_map.items()]
            for future in as_completed(futures):
                tag, link, delays = future.result()
                name = urllib.parse.unquote(urllib.parse.urlsplit(link).fragment) or tag
                if delays:
                    min_d = min(delays)
                    if not (filter_ru and is_ru_node(name)):
                        stage1_candidates.append((tag, link, min_d))
                    results[link] = {"delay": min_d, "jitter": 0, "is_stable": False,
                                     "success_count": len(delays), "name": name}
                    print(f"[ALIVE] {min_d:4d} ms | {name}")
                else:
                    print(f"[DEAD ]         | {name}")

        print(f"\nОтобрано кандидатов для теста стабильности: {len(stage1_candidates)}")
        if stage1_candidates:
            stress_top = int(get_uci("stress_top", "40") or 0)
            stage1_candidates.sort(key=lambda c: c[2])
            if stress_top > 0:
                stage1_candidates = stage1_candidates[:stress_top]

            update_live_progress("📊 [3/4] Тест стабильности...",
                                 f"Анализ джиттера ({len(stage1_candidates)} канд.)...")
            print(f"\n[Этап 2/2] Анализ стабильности (5 замеров, медиана, топ-{len(stage1_candidates)})...")

            def detailed_probe(cand):
                tag, link, _ = cand
                delays = []
                for _i in range(5):
                    d = query_node_delay(api_port, tag, timeout_ms=3000)
                    if d > 0:
                        delays.append(d)
                    if _i < 4:
                        time.sleep(0.15)
                name = urllib.parse.unquote(urllib.parse.urlsplit(link).fragment) or tag
                succ = len(delays)
                if succ < 4:
                    return link, name, -1, -1, False, succ
                delays.sort()
                med = delays[succ // 2]
                trimmed = delays[:-1]
                eff_j = max(trimmed) - min(trimmed)
                worst = delays[-1] - med
                is_st = (eff_j <= max(max_jitter, int(med * 0.50)) and
                         worst <= max(350, int(med * 2.0)))
                return link, name, med, eff_j, is_st, succ

            with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
                futures = [executor.submit(detailed_probe, cand) for cand in stage1_candidates]
                for future in as_completed(futures):
                    link, name, med_d, diff, is_stable, succ = future.result()
                    if med_d > 0:
                        results[link] = {"delay": med_d, "jitter": diff, "is_stable": is_stable,
                                         "success_count": succ, "name": name}
                        label = f"[STABLE {succ}/5]" if is_stable else f"[UNSTABLE {succ}/5]"
                        print(f"{label} {med_d:4d} ms (±{diff:3d}) | {name}")
                    else:
                        print(f"[DROPPED {succ}/5] | {name}")
    finally:
        if proc is not None:
            proc.terminate()
            try:
                proc.wait(1.5)
            except Exception:
                proc.kill()
        if tmp_cfg and os.path.exists(tmp_cfg):
            os.remove(tmp_cfg)

    # Отбор: стабильные, затем расширенный порог, и только по явному разрешению — живые.
    stable = [l for l, i in results.items()
              if i.get("is_stable") and i.get("delay", 0) > 0 and not (filter_ru and is_ru_node(i.get("name", "")))]
    best = [l for l in stable if results[l]["delay"] < threshold]
    if not best and stable:
        relaxed_limit = int(threshold * 1.5)
        best = [l for l in stable if results[l]["delay"] < relaxed_limit]
        if best:
            print(f"[i] Порог {threshold} ms не дал результата, применяем расширенный порог {relaxed_limit} ms.")
    if not best and get_uci_bool("allow_unstable_fallback", False):
        best = [l for l, i in results.items()
                if i.get("delay", 0) > 0 and not (filter_ru and is_ru_node(i.get("name", "")))]
        if best:
            print("[!] Применён fallback на живые (нестабильные) узлы: включена опция allow_unstable_fallback.")

    previous = read_json(LAST_SELECTED_FILE, {}).get("links", [])
    previous_set = {link_fingerprint(x) for x in previous} if isinstance(previous, list) else set()
    hysteresis = max(0.0, min(50.0, float(get_uci("hysteresis_pct", "10") or 0))) / 100.0

    def node_score(link):
        info = results.get(link, {})
        score = float(info.get("delay", 999999)) + float(info.get("jitter", 0)) * 0.3
        if link_fingerprint(link) in previous_set:
            score *= (1.0 - hysteresis)
        return score

    best.sort(key=lambda l: (node_score(l), results.get(l, {}).get("delay", 999999)))
    if max_best > 0 and len(best) > max_best:
        print(f"Применен лимит узлов: оставляем Топ-{max_best} из {len(best)}")
        best = best[:max_best]

    elapsed = round(time.time() - start_time, 1)
    MIN_REQUIRED_NODES = 2

    write_json_atomic(METRICS_FILE, {
        "ts": int(time.time()),
        "elapsed": elapsed,
        "total_links": len(all_links),
        "parsed": len(tag_map),
        "alive": len([1 for i in results.values() if i.get("delay", 0) > 0]),
        "stable": len(stable),
        "selected": len(best),
        "threshold": threshold,
    })

    if not best or len(best) < MIN_REQUIRED_NODES:
        count = len(best) if best else 0
        print(f"\n[!] Предупреждение: найдено всего {count} узлов (требуется минимум {MIN_REQUIRED_NODES}).")
        print("[!] Конфигурация Podkop сохранена без изменений.")
        msg = (f"🔴 <b>SubParser Alert</b>!\n"
               f"Найдено всего {count} узлов. Конфигурация Podkop не изменена.")
        send_telegram_notify(msg)
        return

    print(f"\nВыбрано лучших узлов: {len(best)} шт. (Время: {elapsed}с)")

    if os.path.exists("/etc/config/podkop"):
        try:
            shutil.copy2("/etc/config/podkop", PODKOP_BACKUP)
        except Exception as e:
            print(f"[!] Не удалось сделать бэкап Podkop: {e}")

    changed_sections = []
    section_reports = []
    selection_plan = []
    selection_notes = []
    selection_proxies = None
    write_failed = False
    for sec, mode in section_targets.items():
        existing_links = []
        try:
            out = subprocess.check_output(["uci", "-q", "get", f"podkop.{sec}.urltest_proxy_links"],
                                          text=True, stderr=subprocess.DEVNULL).strip()
            existing_links = [l.strip() for l in out.split() if l.strip()]
        except Exception:
            existing_links = []

        if mode == "append":
            existing_fps = {link_fingerprint(x) for x in existing_links}
            merged = list(existing_links)
            added = 0
            for idx_b, b in enumerate(best):
                if link_fingerprint(b) not in existing_fps:
                    merged.append(_prepare_link(b, results, idx_b))
                    existing_fps.add(link_fingerprint(b))
                    added += 1
            target_links = merged
            before_cnt = len(existing_links)
            after_cnt = len(target_links)
            report = f"  ▫️ <b>{html.escape(sec)}</b>: добавлено +{added} новых (было {before_cnt} → стало {after_cnt})"
        else:
            target_links = [_prepare_link(b, results, idx_b) for idx_b, b in enumerate(best)]
            before_cnt = len(existing_links)
            after_cnt = len(target_links)
            report = f"  ▫️ <b>{html.escape(sec)}</b>: полная замена (было {before_cnt} -> стало {after_cnt})"

        if _link_set(target_links) == _link_set(existing_links):
            print(f"Секция '{sec}': без изменений ({after_cnt} узлов).")
            section_reports.append(f"  ▫️ <b>{html.escape(sec)}</b>: без изменений ({after_cnt})")
            continue

        print(f"Секция '{sec}': запись {after_cnt} узлов (было {before_cnt}).")
        # Запоминаем ручной выбор до перезаписи: после замены списка теги
        # сдвинутся, и старый `sec-N-out` будет указывать на другой сервер.
        if selection_proxies is None:
            selection_proxies = get_podkop_proxies()
        sel_mode, sel_link = capture_selection(sec, existing_links,
                                               selection_proxies)
        if not _write_section_links(sec, target_links):
            write_failed = True
            break
        changed_sections.append(sec)
        selection_plan.append((sec, sel_mode, sel_link, list(target_links)))
        section_reports.append(report)

    if write_failed:
        _restore_podkop_backup()
        send_telegram_notify("🔴 <b>SubParser Alert</b>\nНе удалось записать конфигурацию Podkop. "
                             "Выполнен откат к предыдущей версии.")
        return

    podkop_restarted = False
    if changed_sections:
        update_live_progress("♻️ [4/4] Обновление Podkop...", "Запись серверов в UCI и перезапуск службы...")
        commit = subprocess.run(["uci", "commit", "podkop"], stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, text=True)
        if commit.returncode != 0:
            print(f"[!] uci commit podkop: {commit.stderr.strip()}")
            _restore_podkop_backup()
            send_telegram_notify("🔴 <b>SubParser Alert</b>\nОшибка commit Podkop. Выполнен откат.")
            return
        subprocess.run(["/etc/init.d/podkop", "restart"],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
        podkop_restarted = True
        if not _wait_podkop_ready(timeout=240):
            print("[!] Podkop не поднялся после обновления — откатываем конфигурацию.")
            _restore_podkop_backup()
            send_telegram_notify(
                "🔴 <b>SubParser Alert</b>\nPodkop не поднялся после обновления. "
                "Выполнен автоматический откат к предыдущему списку узлов."
            )
            return
        # Возвращаем ручной выбор на тот же сервер, если он остался в новом
        # списке. Если был urltest — секция остаётся на urltest.
        for sec, mode, link, new_links in selection_plan:
            status = restore_selection(sec, mode, link, new_links)
            if status == "restored":
                selection_notes.append(f"  ▫️ <b>{html.escape(sec)}</b>: активный узел сохранён")
            elif status == "lost":
                selection_notes.append(f"  ▫️ <b>{html.escape(sec)}</b>: выбранный узел не попал в новый список, авто")
            elif status == "not_ready":
                selection_notes.append(f"  ▫️ <b>{html.escape(sec)}</b>: Clash API не ответил, выбор не проверен")
            elif status == "failed":
                selection_notes.append(f"  ▫️ <b>{html.escape(sec)}</b>: не удалось вернуть активный узел")
        print("Podkop успешно обновлен!")
        write_json_atomic(LAST_SELECTED_FILE, {"ts": int(time.time()), "links": best})
    else:
        print("Состав узлов не изменился — Podkop не перезапускался.")
        write_json_atomic(LAST_SELECTED_FILE, {"ts": int(time.time()), "links": best})

    proto_counts = {}
    for l in best:
        pr = urllib.parse.urlsplit(l).scheme.upper()
        proto_counts[pr] = proto_counts.get(pr, 0) + 1
    proto_str = " | ".join([f"{p}: {cnt}" for p, cnt in sorted(proto_counts.items())])

    medals = ["🥇", "🥈", "🥉"]
    top_lines = []
    for i, l in enumerate(best[:3]):
        info = results.get(l, {})
        d = info.get("delay", 0)
        jit = info.get("jitter", 0)
        nm = html.escape(info.get("name", "Proxy"))
        m = medals[i] if i < len(medals) else "•"
        top_lines.append(f"{m} <code>{d} ms</code> (±{jit}) — {nm}")
    top_text = "\n".join(top_lines)
    sections_block = "\n".join(section_reports + selection_notes)
    status_line = "🟢 <b>SubParser: Podkop обновлен</b>" if podkop_restarted else "⚪ <b>SubParser: состав узлов без изменений</b>"

    msg = (
        f"{status_line}\n\n"
        f"📊 <b>Статистика:</b>\n"
        f"• Отобрано узлов: <b>{len(best)}</b> из {len(all_links)}\n"
        f"• Порог задержки: &lt; {threshold} ms\n"
        f"• Время анализа: <code>{elapsed}с</code>\n"
        f"📁 <b>Секции Podkop:</b>\n{sections_block}\n\n"
        f"⚡ <b>Топ-3 быстрых узла:</b>\n{top_text}\n\n"
        f"🏷 <b>Протоколы:</b> <code>{proto_str}</code>"
    )
    if GLOBAL_SUB_USERINFO:
        try:
            write_json_atomic("/tmp/subparser_subinfo.json", GLOBAL_SUB_USERINFO)
        except Exception:
            pass
        sub_lines = []
        for sname, sdata in GLOBAL_SUB_USERINFO.items():
            used = sdata.get("upload", 0) + sdata.get("download", 0)
            tot = sdata.get("total", 0)
            exp = sdata.get("expire", 0)
            exp_str = time.strftime('%d.%m.%Y', time.localtime(exp)) if exp else "бессрочно"
            if tot > 0:
                sub_lines.append(f"• <b>{html.escape(str(sname))}</b>: {format_bytes(used)} / {format_bytes(tot)} (до {exp_str})")
        if sub_lines:
            msg += "\n\n💳 <b>Лимиты подписок:</b>\n" + "\n".join(sub_lines)
    send_telegram_notify(msg)


def main():
    lock_fd = _acquire_lock()
    if lock_fd is None:
        msg = ("[!] Запуск отклонён: парсер уже выполняется другим процессом "
               f"(источник: {_detect_launch_source()}, pid {os.getpid()}).")
        print(msg)
        _append_run_log(f"{time.strftime('%d.%m.%Y %H:%M:%S')} {msg}")
        return
    _setup_run_log()
    try:
        _run()
    except Exception as e:
        print(f"[FATAL] {e}")
        traceback.print_exc()
        try:
            send_telegram_notify(
                f"🔴 <b>SubParser: критическая ошибка</b>\n<code>{html.escape(str(e)[:400])}</code>"
            )
        except Exception:
            pass
    finally:
        _teardown_run_log()
        _release_lock(lock_fd)


if __name__ == "__main__":
    main()
