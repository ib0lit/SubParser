#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Мониторинг выбранных сервисов через активный путь Podkop.

Работает как лёгкий демон (procd): раз в несколько секунд смотрит счётчики
WAN и проверяет сервисы только при активном трафике. Проверяется реальный
активный узел секции, а не всегда URLTest-группа, поэтому режим ручного
выбора тоже поддерживается.

Диагностический запуск:
    subparser-service-monitor.py --check
    subparser-service-monitor.py --check --json
"""

import argparse
import glob
import html
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor

sys.path.insert(0, "/usr/lib")
try:
    from subparser.podkop import get_api_base, get_api_headers, \
        get_managed_podkop_sections
    from subparser.service_presets import SERVICE_PRESETS, PRESET_ORDER
except Exception:  # pragma: no cover - на роутере модули всегда на месте
    SERVICE_PRESETS = {}
    PRESET_ORDER = []
    get_api_base = None
    get_api_headers = None
    get_managed_podkop_sections = None

STATE_FILE = "/tmp/subparser_service_monitor.json"
LOG_FILE = "/tmp/subparser_service_monitor.log"
LOG_MAX = 256 * 1024
SAMPLE_INTERVAL = 5
ACTIVE_HOLD = 30


# --- UCI ----------------------------------------------------------------------
def uci_get(path, default=""):
    try:
        out = subprocess.check_output(
            ["uci", "-q", "get", path], text=True,
            stderr=subprocess.DEVNULL).strip()
    except Exception:
        return default
    return out if out else default


def cfg(name, default=""):
    return uci_get("subparser.settings." + name, default)


def cfg_bool(name, default=False):
    value = cfg(name, "1" if default else "0").strip().lower()
    return value in ("1", "true", "yes", "on")


def cfg_num(name, default, minimum, maximum):
    try:
        value = int(str(cfg(name, str(default))).strip())
    except Exception:
        value = default
    return max(minimum, min(maximum, value))


# --- API ----------------------------------------------------------------------
def api(path, timeout=3.0, method="GET", payload=None):
    if not get_api_base:
        return None, None
    headers = dict(get_api_headers() or {})
    data = None
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(
        get_api_base().rstrip("/") + path,
        data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read()
            return resp.status, (json.loads(body.decode("utf-8")) if body else {})
    except urllib.error.HTTPError as exc:
        body = None
        try:
            raw = exc.read()
            body = json.loads(raw.decode("utf-8")) if raw else None
        except Exception:
            body = None
        return exc.code, body
    except Exception:
        return None, None


def quote(value):
    return urllib.parse.quote(str(value), safe="")


def probe(tag, url, timeout_ms):
    path = ("/proxies/%s/delay?url=%s&timeout=%d"
            % (quote(tag), quote(url), timeout_ms))
    status, data = api(path, timeout=timeout_ms / 1000.0 + 2)
    if status in (200, 204) and isinstance(data, dict):
        delay = data.get("delay")
        if isinstance(delay, int) and delay > 0:
            return delay
    return None


def probe_target(tag, target, timeout_ms):
    """Проверяет все URL сервиса. Успех — хотя бы один ответ."""
    urls = target.get("urls") or []
    results = []
    delays = []
    with ThreadPoolExecutor(max_workers=max(1, min(4, len(urls)))) as pool:
        futures = [pool.submit(probe, tag, url, timeout_ms) for url in urls]
        for url, future in zip(urls, futures):
            try:
                value = future.result()
            except Exception:
                value = None
            results.append({"url": url, "delay": value})
            if value is not None:
                delays.append(value)
    require_all = bool(target.get("require_all"))
    if require_all:
        ok = bool(urls) and len(delays) == len(urls)
        delay = max(delays) if ok else None
    else:
        ok = bool(delays)
        delay = min(delays) if delays else None
    return {"ok": ok, "delay": delay, "urls": results}


# --- Podkop/прокси ------------------------------------------------------------
def find_selector(proxies, section):
    for tag in (section + "-out", section):
        data = proxies.get(tag)
        if isinstance(data, dict) and \
                str(data.get("type", "")).lower() == "selector":
            return tag, data
    for tag, data in proxies.items():
        if section in tag and isinstance(data, dict) and \
                str(data.get("type", "")).lower() == "selector":
            return tag, data
    return None, None


def find_group(proxies, section):
    tag = section + "-urltest-out"
    data = proxies.get(tag)
    if isinstance(data, dict) and \
            str(data.get("type", "")).lower() == "urltest":
        return tag, data
    for key, value in proxies.items():
        if section in key and isinstance(value, dict) and \
                str(value.get("type", "")).lower() == "urltest":
            return key, value
    return None, None


def resolve_active(proxies, section):
    selector_tag, selector = find_selector(proxies, section)
    if selector:
        now = selector.get("now")
        if now and now in proxies:
            node = proxies[now]
            ntype = str(node.get("type", "")).lower()
            mode = "auto" if ntype in ("urltest", "fallback", "loadbalance") \
                or "urltest" in str(now).lower() else "manual"
            return {
                "selector_tag": selector_tag,
                "active_tag": now,
                "mode": mode,
                "label": now,
            }
    group_tag, group = find_group(proxies, section)
    if group_tag:
        return {
            "selector_tag": selector_tag,
            "active_tag": group_tag,
            "mode": "auto",
            "label": group.get("now") or group_tag,
        }
    return None


def find_nodes(proxies, section):
    _, group = find_group(proxies, section)
    if group:
        nodes = []
        for name in group.get("all") or []:
            data = proxies.get(name)
            if not isinstance(data, dict):
                continue
            if str(data.get("type", "")).lower() in \
                    ("selector", "urltest", "fallback", "loadbalance"):
                continue
            nodes.append(name)
        if nodes:
            return nodes
    _, selector = find_selector(proxies, section)
    nodes = []
    if selector:
        for name in selector.get("all") or []:
            data = proxies.get(name)
            if not isinstance(data, dict):
                continue
            if str(data.get("type", "")).lower() in \
                    ("selector", "urltest", "fallback", "loadbalance"):
                continue
            nodes.append(name)
    return nodes


def force_group(group_tag, url, timeout_ms):
    path = ("/group/%s/delay?url=%s&timeout=%d"
            % (quote(group_tag), quote(url), timeout_ms))
    api(path, timeout=timeout_ms / 1000.0 + 2)


def switch_selector(selector_tag, name):
    status, _ = api("/proxies/" + quote(selector_tag), method="PUT",
                    payload={"name": name}, timeout=4)
    return status in (200, 204)


def switch_to_best(proxies, section, info, target, timeout_ms):
    """Перебирает узлы по всем URL сервиса и переключает ручной селектор."""
    selector_tag = info.get("selector_tag")
    nodes = find_nodes(proxies, section)
    if not selector_tag or not nodes:
        return None

    best = None
    best_delay = None
    deadline = time.time() + 45
    with ThreadPoolExecutor(max_workers=4) as pool:
        for start in range(0, len(nodes), 4):
            if time.time() >= deadline:
                break
            batch = nodes[start:start + 4]
            futures = {pool.submit(probe_target, node, target, timeout_ms): node
                       for node in batch}
            for future in futures:
                node = futures[future]
                try:
                    result = future.result()
                except Exception:
                    result = {"ok": False, "delay": None}
                if result["ok"] and (best_delay is None or
                                     result["delay"] < best_delay):
                    best, best_delay = node, result["delay"]

    if not best:
        return None
    if not switch_selector(selector_tag, best):
        return None
    time.sleep(1)
    return {"node": best, "delay": best_delay}


# --- Состояние, лог, уведомления ---------------------------------------------
def load_state():
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def save_state(state):
    try:
        tmp = STATE_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(state, fh, ensure_ascii=False)
        os.replace(tmp, STATE_FILE)
    except Exception:
        pass


def log(message):
    line = "%s %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), message)
    try:
        if os.path.exists(LOG_FILE) and os.path.getsize(LOG_FILE) > LOG_MAX:
            os.replace(LOG_FILE, LOG_FILE + ".1")
        with open(LOG_FILE, "a", encoding="utf-8") as fh:
            fh.write(line)
    except Exception:
        pass


def notify(message):
    if not cfg_bool("watchdog_service_notify", True):
        return
    token = cfg("tg_bot_token", "")
    chat_id = cfg("tg_chat_id", "")
    if not token or not chat_id:
        return
    data = urllib.parse.urlencode({
        "chat_id": chat_id,
        "text": message,
        "parse_mode": "HTML",
    }).encode("utf-8")
    req = urllib.request.Request(
        "https://api.telegram.org/bot%s/sendMessage" % token, data=data)
    try:
        urllib.request.urlopen(req, timeout=8).read()
    except Exception:
        pass


# --- Трафик WAN ---------------------------------------------------------------
def _int_file(path):
    try:
        with open(path, "r", encoding="ascii") as fh:
            return int(fh.read().strip() or 0)
    except Exception:
        return 0


def wan_counters():
    iface = ""
    try:
        out = subprocess.check_output(
            ["ip", "route", "show", "default"], text=True,
            stderr=subprocess.DEVNULL)
        parts = out.split()
        if "dev" in parts:
            idx = parts.index("dev")
            if idx + 1 < len(parts):
                iface = parts[idx + 1]
    except Exception:
        pass
    if not iface:
        iface = uci_get("network.wan.device", "") or \
            uci_get("network.wan.ifname", "")

    if iface:
        rx_file = "/sys/class/net/%s/statistics/rx_bytes" % iface
        tx_file = "/sys/class/net/%s/statistics/tx_bytes" % iface
        if os.path.exists(rx_file):
            return _int_file(rx_file), _int_file(tx_file)

    rx = tx = 0
    for rx_file in glob.glob("/sys/class/net/*/statistics/rx_bytes"):
        name = rx_file.split("/")[4]
        if name == "lo":
            continue
        tx_file = rx_file.replace("/rx_bytes", "/tx_bytes")
        rx += _int_file(rx_file)
        tx += _int_file(tx_file)
    return rx, tx


class TrafficTracker:
    def __init__(self):
        self.last_ts = 0.0
        self.last_bytes = 0
        self.active_until = 0.0

    def is_active(self, threshold_kb):
        now = time.time()
        rx, tx = wan_counters()
        total = rx + tx
        if self.last_ts > 0 and now > self.last_ts:
            elapsed = now - self.last_ts
            delta = total - self.last_bytes
            if delta >= 0 and (delta / 1024.0 / elapsed) >= threshold_kb:
                self.active_until = now + ACTIVE_HOLD
        self.last_ts = now
        self.last_bytes = total
        return now < self.active_until


# --- Цели проверки ------------------------------------------------------------
def load_targets():
    targets = []
    for preset_id in PRESET_ORDER:
        if not cfg_bool("watchdog_service_" + preset_id, False):
            continue
        preset = SERVICE_PRESETS.get(preset_id)
        if not preset:
            continue
        targets.append({
            "id": preset_id,
            "label": preset.get("label", preset_id),
            "urls": list(preset.get("urls") or []),
            "require_all": bool(preset.get("require_all")),
        })

    manual = str(cfg("watchdog_services", "")).split()
    for entry in manual:
        entry = entry.strip()
        if not entry:
            continue
        if "|" in entry:
            parts = [p.strip() for p in entry.split("|") if p.strip()]
            label = parts[0]
            urls = [p for p in parts[1:] if p.startswith("https://")]
        else:
            urls = [entry] if entry.startswith("https://") else []
            label = urllib.parse.urlsplit(urls[0]).hostname if urls else entry
        urls = [u for u in urls if u.startswith("https://")]
        if not urls:
            continue
        targets.append({
            "id": "url:" + "|".join(urls),
            "label": label or urls[0],
            "urls": urls,
        })
    return targets


def load_sections():
    if get_managed_podkop_sections:
        try:
            return get_managed_podkop_sections() or ["main"]
        except Exception:
            pass
    return ["main"]


# --- Основная проверка --------------------------------------------------------
def run_cycle(targets, state):
    timeout_ms = cfg_num("watchdog_service_timeout", 8000, 1000, 30000)
    threshold = cfg_num("watchdog_service_fail_threshold", 2, 1, 5)
    cooldown = cfg_num("watchdog_service_cooldown", 10, 1, 1440) * 60
    group_retry = cfg_num("watchdog_group_retry", 3, 0, 60)
    manual_switch = cfg_bool("watchdog_service_manual_switch", True)

    sections = load_sections()
    if not sections:
        return
    status, data = api("/proxies", timeout=5)
    if status not in (200, 204) or not isinstance(data, dict):
        log("Clash API недоступен (status=%s), проверка пропущена" % status)
        return
    proxies = data.get("proxies", {})

    for section in sections:
        info = resolve_active(proxies, section)
        if not info:
            continue
        for target in targets:
            key = "%s|%s" % (section, target["id"])
            st = state.setdefault(key, {})
            result = probe_target(info["active_tag"], target, timeout_ms)

            if result["ok"]:
                if st.get("fails", 0) >= threshold and st.get("notified"):
                    notify("✅ <b>Сервис снова доступен</b>\n"
                           "Секция <b>%s</b>: <code>%s</code>\n"
                           "Узел: <code>%s</code>"
                           % (html.escape(section),
                              html.escape(target["label"]),
                              html.escape(str(info["label"]))))
                st["fails"] = 0
                st["notified"] = False
                st["last_ok"] = int(time.time())
                continue

            fails = int(st.get("fails", 0) or 0) + 1
            st["fails"] = fails
            st["last_fail"] = int(time.time())
            log("FAIL %s / %s (узел %s, подряд %d)"
                % (section, target["label"], info["label"], fails))
            if fails < threshold:
                continue

            now = int(time.time())
            if now - int(st.get("last_action", 0) or 0) < cooldown:
                continue
            st["last_action"] = now

            if info["mode"] == "manual" and manual_switch:
                switched = switch_to_best(proxies, section, info, target,
                                          timeout_ms)
                if switched:
                    st["notified"] = True
                    log("MANUAL SWITCH %s / %s -> %s (%sms)"
                        % (section, target["label"], switched["node"],
                           switched["delay"]))
                    notify("🔁 <b>Сервис недоступен, узел переключён</b>\n"
                           "Секция <b>%s</b>: <code>%s</code>\n"
                           "Узел: <code>%s</code> → <code>%s</code> (%s ms)"
                           % (html.escape(section),
                              html.escape(target["label"]),
                              html.escape(str(info["label"])),
                              html.escape(str(switched["node"])),
                              switched["delay"]))
                    # Следующие цели должны проверяться уже через новый узел.
                    status3, data3 = api("/proxies", timeout=5)
                    if status3 in (200, 204) and isinstance(data3, dict):
                        fresh = data3.get("proxies", {})
                        info = resolve_active(fresh, section) or info
                        proxies = fresh
                else:
                    st["notified"] = True
                    notify("⚠️ <b>Сервис недоступен</b>\n"
                           "Секция <b>%s</b>: <code>%s</code>\n"
                           "Ни один узел не открыл адрес."
                           % (html.escape(section),
                              html.escape(target["label"])))
                continue

            group_tag, group = find_group(proxies, section)
            if not group_tag:
                st["notified"] = True
                notify("⚠️ <b>Сервис недоступен</b>\n"
                       "Секция <b>%s</b>: <code>%s</code>\n"
                       "Не найдена URLTest-группа для перепроверки."
                       % (html.escape(section),
                          html.escape(target["label"])))
                continue

            before = str(group.get("now") or info["label"])
            force_group(group_tag, (target.get("urls") or [""])[0], timeout_ms)
            if group_retry > 0:
                time.sleep(group_retry)
            status2, data2 = api("/proxies", timeout=5)
            if status2 in (200, 204) and isinstance(data2, dict):
                info2 = resolve_active(data2.get("proxies", {}), section)
                if info2:
                    checked = probe_target(info2["active_tag"], target,
                                           timeout_ms)
                    if checked["ok"]:
                        _, group2 = find_group(data2.get("proxies", {}), section)
                        after = str((group2 or {}).get("now") or info2["label"])
                        st["notified"] = True
                        notify("🔁 <b>URLTest переключил узел</b>\n"
                               "Секция <b>%s</b>: <code>%s</code>\n"
                               "Узел: <code>%s</code> → <code>%s</code>"
                               % (html.escape(section),
                                  html.escape(target["label"]),
                                  html.escape(before), html.escape(after)))
                        continue
            st["notified"] = True
            notify("⚠️ <b>Сервис недоступен</b>\n"
                   "Секция <b>%s</b>: <code>%s</code>\n"
                   "URLTest перепроверен, рабочий узел не найден."
                   % (html.escape(section), html.escape(target["label"])))


# --- Диагностика --------------------------------------------------------------
def check_once(as_json=False):
    targets = load_targets()
    sections = load_sections()
    if not targets:
        if as_json:
            print(json.dumps({"sections": [], "targets": 0}, ensure_ascii=False))
        else:
            print("Сервисы не выбраны. Включите пресеты или добавьте URL.")
        return 0

    status, data = api("/proxies", timeout=5)
    if status not in (200, 204) or not isinstance(data, dict):
        print("Clash API недоступен (status=%s)" % status)
        return 1
    proxies = data.get("proxies", {})
    timeout_ms = cfg_num("watchdog_service_timeout", 8000, 1000, 30000)
    report = {"generated": int(time.time()), "sections": []}

    for section in sections:
        info = resolve_active(proxies, section)
        if not info:
            report["sections"].append({
                "section": section, "error": "активный путь не найден",
                "targets": [],
            })
            continue
        section_data = {
            "section": section,
            "node": info["label"],
            "tag": info["active_tag"],
            "mode": info["mode"],
            "targets": [],
        }
        if not as_json:
            mode = "ручной" if info["mode"] == "manual" else "URLTest"
            print("Секция %s: узел %s (%s)" % (section, info["label"], mode))
        for target in targets:
            result = probe_target(info["active_tag"], target, timeout_ms)
            item = {
                "id": target["id"],
                "label": target["label"],
                "ok": result["ok"],
                "delay": result["delay"],
                "urls": result["urls"],
            }
            section_data["targets"].append(item)
            if not as_json:
                if result["ok"]:
                    print("  ✅ %s — %s ms" % (target["label"], result["delay"]))
                else:
                    print("  ❌ %s — нет ответа" % target["label"])
                for url_result in result["urls"]:
                    value = url_result["delay"]
                    print("      %s — %s" % (
                        url_result["url"],
                        ("%s ms" % value) if value is not None else "FAIL"))
        report["sections"].append(section_data)

    if as_json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


# --- Демон --------------------------------------------------------------------
def daemon():
    tracker = TrafficTracker()
    state = load_state()
    last_cycle = 0.0
    last_api_log = 0.0
    while True:
        try:
            if not cfg_bool("enabled", False) or \
                    not cfg_bool("watchdog_service_enabled", True):
                time.sleep(SAMPLE_INTERVAL)
                continue
            targets = load_targets()
            if not targets:
                time.sleep(SAMPLE_INTERVAL)
                continue
            threshold = cfg_num("watchdog_service_traffic_kb", 20, 1, 100000)
            interval = cfg_num("watchdog_service_active_interval", 30, 10, 300)
            active = tracker.is_active(threshold)
            if active and (time.time() - last_cycle) >= interval:
                run_cycle(targets, state)
                save_state(state)
                last_cycle = time.time()
        except Exception as exc:
            now = time.time()
            if now - last_api_log >= 60:
                log("Ошибка мониторинга: %s" % exc)
                last_api_log = now
        time.sleep(SAMPLE_INTERVAL)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true",
                        help="один диагностический прогон без изменений")
    parser.add_argument("--json", action="store_true",
                        help="вывод диагностики в JSON")
    args = parser.parse_args()
    if args.check:
        return check_once(args.json)
    daemon()
    return 0


if __name__ == "__main__":
    sys.exit(main())
