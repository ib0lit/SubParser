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
ACTION_RETRY = 60


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


def _target_urls(target):
    return [url for url in (target.get("urls") or []) if url]


def _aggregate_target(target, delays_by_url):
    """Сводит задержки по адресам в один результат проверки сервиса."""
    urls = _target_urls(target)
    delays = [value for value in delays_by_url.values()
              if isinstance(value, int) and value > 0]
    if target.get("require_all"):
        ok = bool(urls) and len(delays) == len(urls)
        delay = max(delays) if ok else None
    else:
        ok = bool(delays)
        delay = min(delays) if delays else None
    return ok, delay


def probe_target(tag, target, timeout_ms):
    """Проверяет все URL сервиса через конкретный узел."""
    urls = _target_urls(target)
    results = []
    delays = {}
    with ThreadPoolExecutor(max_workers=max(1, min(4, len(urls)))) as pool:
        futures = [pool.submit(probe, tag, url, timeout_ms) for url in urls]
        for url, future in zip(urls, futures):
            try:
                value = future.result()
            except Exception:
                value = None
            results.append({"url": url, "delay": value})
            delays[url] = value
    ok, delay = _aggregate_target(target, delays)
    return {"ok": ok, "delay": delay, "urls": results}


def scan_nodes(nodes, target, timeout_ms, max_workers=10):
    """Перебирает узлы по всем адресам сервиса общим пулом запросов."""
    urls = _target_urls(target)
    if not nodes or not urls:
        return {}
    by_node = {name: {} for name in nodes}
    tasks = [(name, url) for name in nodes for url in urls]
    with ThreadPoolExecutor(
            max_workers=max(1, min(max_workers, len(tasks)))) as pool:
        futures = {pool.submit(probe, name, url, timeout_ms): (name, url)
                   for name, url in tasks}
        for future, (name, url) in futures.items():
            try:
                value = future.result()
            except Exception:
                value = None
            by_node[name][url] = value
    result = {}
    for name in nodes:
        ok, delay = _aggregate_target(target, by_node[name])
        result[name] = {"ok": ok, "delay": delay, "urls": by_node[name]}
    return result


def probe_group_nodes(group_tag, target, timeout_ms):
    """Проверяет всю URLTest-группу по каждому адресу сервиса.

    sing-box сам параллельно тестирует узлы группы, поэтому один запрос
    на адрес дешевле, чем отдельные запросы по всем узлам.
    """
    urls = _target_urls(target)
    if not group_tag or not urls:
        return {}
    maps = {}
    with ThreadPoolExecutor(max_workers=max(1, len(urls))) as pool:
        futures = {}
        for url in urls:
            path = ("/group/%s/delay?url=%s&timeout=%d"
                    % (quote(group_tag), quote(url), timeout_ms))
            futures[pool.submit(api, path, timeout_ms / 1000.0 + 5)] = url
        for future, url in futures.items():
            try:
                status, data = future.result()
            except Exception:
                status, data = None, None
            if status in (200, 204) and isinstance(data, dict):
                maps[url] = data
    if not maps:
        return {}
    names = set()
    for data in maps.values():
        names.update(data.keys())
    result = {}
    for name in names:
        delays = {}
        for url in urls:
            value = maps.get(url, {}).get(name)
            try:
                value = int(value)
            except (TypeError, ValueError):
                value = None
            delays[url] = value if value and value > 0 else None
        ok, delay = _aggregate_target(target, delays)
        result[name] = {"ok": ok, "delay": delay}
    return result


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


def _concrete_node(proxies, group):
    """Возвращает фактически выбранный узел группы, если он известен."""
    if not isinstance(group, dict):
        return None
    name = group.get("now")
    if name and name in proxies:
        data = proxies.get(name)
        if isinstance(data, dict) and str(data.get("type", "")).lower() \
                not in ("selector", "urltest", "fallback", "loadbalance"):
            return name
    return None


def resolve_active(proxies, section):
    selector_tag, selector = find_selector(proxies, section)
    if selector:
        now = selector.get("now")
        if now and now in proxies:
            node = proxies[now]
            ntype = str(node.get("type", "")).lower()
            auto = ntype in ("urltest", "fallback", "loadbalance") or \
                "urltest" in str(now).lower()
            if auto:
                # Проверяем конкретный узел, выбранный группой, а не саму
                # группу: так результат относится к реальному активному пути.
                concrete = _concrete_node(proxies, node)
                return {
                    "selector_tag": selector_tag,
                    "active_tag": concrete or now,
                    "group_tag": now,
                    "mode": "auto",
                    "label": concrete or str(node.get("now") or now),
                }
            return {
                "selector_tag": selector_tag,
                "active_tag": now,
                "group_tag": None,
                "mode": "manual",
                "label": now,
            }
    group_tag, group = find_group(proxies, section)
    if group_tag:
        concrete = _concrete_node(proxies, group)
        return {
            "selector_tag": selector_tag,
            "active_tag": concrete or group_tag,
            "group_tag": group_tag,
            "mode": "auto",
            "label": concrete or str(group.get("now") or group_tag),
        }
    return None


def find_nodes(proxies, section):
    """Все конкретные узлы секции, которые можно выбрать селектором."""
    nodes = []
    seen = set()

    def add(name):
        if name in seen:
            return
        data = proxies.get(name)
        if not isinstance(data, dict):
            return
        if str(data.get("type", "")).lower() in \
                ("selector", "urltest", "fallback", "loadbalance"):
            return
        seen.add(name)
        nodes.append(name)

    _, group = find_group(proxies, section)
    if group:
        for name in group.get("all") or []:
            add(name)

    # Добираем узлы секции, которых может не быть в URLTest-группе
    # (фильтры Podkop, ручной список и т.п.), иначе часть серверов
    # никогда не проверяется.
    prefix = section + "-"
    suffix = "-out"
    for name in proxies:
        if not (name.startswith(prefix) and name.endswith(suffix)):
            continue
        if name[len(prefix):-len(suffix)].isdigit():
            add(name)

    if nodes:
        return nodes

    _, selector = find_selector(proxies, section)
    if selector:
        for name in selector.get("all") or []:
            add(name)
    return nodes


def switch_selector(selector_tag, name):
    status, _ = api("/proxies/" + quote(selector_tag), method="PUT",
                    payload={"name": name}, timeout=4)
    return status in (200, 204)


def find_working_nodes(nodes, target, timeout_ms, group_tag=None,
                       precomputed=None):
    """Возвращает рабочие узлы, отсортированные по задержке."""
    checked = {}
    if precomputed:
        for name in nodes:
            if name in precomputed:
                checked[name] = precomputed[name]
    elif group_tag:
        # Быстрый путь: sing-box сам параллельно тестирует всю группу.
        group_results = probe_group_nodes(group_tag, target, timeout_ms)
        for name in nodes:
            if name in group_results:
                checked[name] = group_results[name]
    missing = [name for name in nodes if name not in checked]
    if missing:
        checked.update(scan_nodes(missing, target, timeout_ms))

    working = []
    for name in nodes:
        result = checked.get(name)
        if not result or not result.get("ok"):
            continue
        working.append({"node": name, "delay": result.get("delay")})
    working.sort(key=lambda item: (
        item["delay"] is None,
        item["delay"] if item["delay"] is not None else 0))
    return working


def switch_to_best(proxies, section, info, target, timeout_ms,
                   precomputed=None):
    """Перебирает все узлы по всем URL сервиса и выбирает лучший."""
    selector_tag = info.get("selector_tag")
    nodes = find_nodes(proxies, section)
    if not selector_tag or not nodes:
        return None

    selector = proxies.get(selector_tag)
    if isinstance(selector, dict):
        allowed = set(selector.get("all") or [])
        if allowed:
            nodes = [name for name in nodes if name in allowed] or nodes

    group_tag = info.get("group_tag")
    if not group_tag:
        group_tag, _ = find_group(proxies, section)
    candidates = find_working_nodes(nodes, target, timeout_ms,
                                    group_tag, precomputed)
    for candidate in candidates:
        if not switch_selector(selector_tag, candidate["node"]):
            continue
        time.sleep(1)
        return candidate
    return None


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


def _tg_call(method, payload, timeout=8):
    """Вызов Telegram Bot API.

    Возвращает (result, error). error = None при успехе, "gone" — если
    сообщение уже нельзя редактировать, "transient" — если это временная
    ошибка (таймаут, 429, сеть). Временную ошибку нельзя превращать в
    новое сообщение: иначе при каждом сбое сети появляется дубль.
    """
    token = cfg("tg_bot_token", "")
    chat_id = cfg("tg_chat_id", "")
    if not token or not chat_id:
        return None, "disabled"
    data = dict(payload)
    data["chat_id"] = chat_id
    data["parse_mode"] = "HTML"
    body = urllib.parse.urlencode(data).encode("utf-8")
    req = urllib.request.Request(
        "https://api.telegram.org/bot%s/%s" % (token, method), data=body)
    try:
        raw = urllib.request.urlopen(req, timeout=timeout).read()
        result = json.loads(raw.decode("utf-8"))
        if result.get("ok"):
            return result.get("result") or {}, None
        return None, "transient"
    except urllib.error.HTTPError as exc:
        description = ""
        try:
            result = json.loads(exc.read().decode("utf-8"))
            description = str(result.get("description", "")).lower()
        except Exception:
            pass
        # Повторное редактирование без изменений Telegram считает ошибкой.
        if "not modified" in description:
            return {}, None
        if method == "editMessageText" and (
                "message to edit not found" in description or
                "message can't be edited" in description or
                "message_id_invalid" in description):
            return None, "gone"
        return None, "transient"
    except Exception:
        return None, "transient"


def notify(st, message, cooldown):
    """Уведомление с окном обновления одного сообщения.

    Пока не истёк кулдаун, новое событие редактирует прошлое сообщение.
    После окна отправляется отдельное сообщение. Переключения узлов
    этим окном не ограничиваются.
    """
    if not cfg_bool("watchdog_service_notify", True):
        return
    now = int(time.time())
    message_id = st.get("tg_message_id")
    sent_at = int(st.get("tg_sent_at", 0) or 0)
    if message_id and now - sent_at < cooldown:
        edited = message + "\n\n🕒 Обновлено: %s" % \
            time.strftime("%d.%m %H:%M:%S", time.localtime(now))
        result, error = _tg_call("editMessageText", {
            "message_id": message_id, "text": edited})
        if error != "gone":
            # Успех или временная ошибка: id сообщения сохраняем, новое
            # не отправляем. При временной ошибке повторим на следующем
            # цикле, когда сеть/Telegram придут в себя.
            if error is None:
                st["tg_edited_at"] = now
            return
        # Сообщение удалено или слишком старое — отправим новое.
    result, error = _tg_call("sendMessage", {"text": message})
    if error is None and isinstance(result, dict) and result.get("message_id"):
        st["tg_message_id"] = result["message_id"]
        st["tg_sent_at"] = now
        st["tg_edited_at"] = 0


def notify_failure(st, section, target, reason, cooldown):
    st["notified"] = True
    st["last_reason"] = reason
    notify(st, "⚠️ <b>Сервис недоступен</b>\n"
           "Секция <b>%s</b>: <code>%s</code>\n%s"
           % (html.escape(section), html.escape(target["label"]), reason),
           cooldown)


def maybe_restore_auto(proxies, section, info, target, st, timeout_ms,
                       cooldown):
    """Возвращает селектор в URLTest, когда группа уже выбрала рабочий узел.

    После закрепления на конкретном узле URLTest продолжает работать сам.
    Как только его выбор снова станет рабочим, возвращаем секцию в авто-
    режим, чтобы закрепление не оставалось навсегда.
    """
    group_tag = st.get("pinned_group")
    pinned = st.get("pinned_node")
    if not group_tag or not pinned:
        return proxies, info
    if info.get("mode") != "manual" or info.get("active_tag") != pinned:
        return proxies, info

    now = int(time.time())
    if now - int(st.get("pinned_check", 0) or 0) < ACTION_RETRY:
        return proxies, info
    st["pinned_check"] = now

    _, group = find_group(proxies, section)
    candidate = _concrete_node(proxies, group) if group else None
    if not candidate or candidate == pinned or \
            candidate == st.get("pinned_bad"):
        return proxies, info
    checked = probe_target(candidate, target, timeout_ms)
    if not checked.get("ok"):
        return proxies, info

    selector_tag = info.get("selector_tag")
    if not selector_tag or not switch_selector(selector_tag, group_tag):
        return proxies, info
    for key in ("pinned_group", "pinned_node", "pinned_bad", "pinned_check"):
        st.pop(key, None)
    log("RESTORE URLTEST %s / %s -> %s"
        % (section, target["label"], group_tag))
    notify(st, "🔁 <b>Возврат в URLTest</b>\n"
           "Секция <b>%s</b>: <code>%s</code>\n"
           "Группа выбрала рабочий узел: <code>%s</code>"
           % (html.escape(section), html.escape(target["label"]),
              html.escape(str(candidate))), cooldown)
    status, data = api("/proxies", timeout=5)
    if status in (200, 204) and isinstance(data, dict):
        fresh = data.get("proxies", {})
        return fresh, resolve_active(fresh, section) or info
    return proxies, info


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
def _section_list(value):
    return [s for s in str(value or "").split() if s]


def target_applies(target, section):
    selected = target.get("sections") or []
    return not selected or section in selected


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
            "sections": _section_list(
                cfg("watchdog_service_sections_" + preset_id, "")),
        })

    manual = str(cfg("watchdog_services", "")).split()
    for entry in manual:
        entry = entry.strip()
        if not entry:
            continue
        sections = []
        if "|" in entry:
            parts = [p.strip() for p in entry.split("|") if p.strip()]
            label = parts[0]
            urls = []
            for part in parts[1:]:
                low = part.lower()
                if low.startswith("sections="):
                    sections = [s.strip() for s in
                                part.split("=", 1)[1].split(",") if s.strip()]
                elif part.startswith("https://"):
                    urls.append(part)
        else:
            urls = [entry] if entry.startswith("https://") else []
            label = urllib.parse.urlsplit(urls[0]).hostname if urls else entry
        urls = [u for u in urls if u.startswith("https://")]
        if not urls:
            continue
        target_id = "url:" + "|".join(urls)
        if sections:
            target_id += "@" + ",".join(sections)
        targets.append({
            "id": target_id,
            "label": label or urls[0],
            "urls": urls,
            "sections": sections,
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
    cooldown = cfg_num("watchdog_service_cooldown", 30, 1, 1440) * 60
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
            if not target_applies(target, section):
                continue
            key = "%s|%s" % (section, target["id"])
            st = state.setdefault(key, {})
            result = probe_target(info["active_tag"], target, timeout_ms)

            if result["ok"]:
                st["fails"] = 0
                st["notified"] = False
                st["last_ok"] = int(time.time())
                proxies, info = maybe_restore_auto(
                    proxies, section, info, target, st, timeout_ms, cooldown)
                save_state(state)
                continue

            fails = int(st.get("fails", 0) or 0) + 1
            st["fails"] = fails
            st["last_fail"] = int(time.time())
            log("FAIL %s / %s (узел %s, подряд %d)"
                % (section, target["label"], info["label"], fails))
            if fails < threshold:
                continue

            # Тяжёлый перебор узлов не чаще раза в минуту: если сервис
            # лежит у всех провайдеров, не нужно сканировать 25 серверов
            # каждые 30 секунд. Сообщение при этом продолжаем обновлять.
            now = int(time.time())
            if now - int(st.get("last_action", 0) or 0) < ACTION_RETRY:
                notify_failure(st, section, target,
                               st.get("last_reason")
                               or "Повторная проверка отложена.", cooldown)
                save_state(state)
                continue
            st["last_action"] = now

            # Ручной выбор: перебираем узлы по адресам сервиса и
            # переключаем селектор на лучший.
            if info["mode"] == "manual":
                if not manual_switch:
                    notify_failure(st, section, target,
                                   "Узел не переключён: "
                                   "автопереключение отключено.", cooldown)
                    save_state(state)
                    continue
                switched = switch_to_best(proxies, section, info, target,
                                          timeout_ms)
                if switched:
                    for key in ("pinned_group", "pinned_node",
                                "pinned_bad", "pinned_check"):
                        st.pop(key, None)
                    st["notified"] = True
                    log("MANUAL SWITCH %s / %s -> %s (%sms)"
                        % (section, target["label"], switched["node"],
                           switched["delay"]))
                    notify(st, "🔁 <b>Сервис недоступен, узел переключён</b>\n"
                           "Секция <b>%s</b>: <code>%s</code>\n"
                           "Узел: <code>%s</code> → <code>%s</code> (%s ms)"
                           % (html.escape(section),
                              html.escape(target["label"]),
                              html.escape(str(info["label"])),
                              html.escape(str(switched["node"])),
                              switched["delay"]), cooldown)
                    save_state(state)
                    # Следующие цели должны проверяться уже через новый узел.
                    status3, data3 = api("/proxies", timeout=5)
                    if status3 in (200, 204) and isinstance(data3, dict):
                        fresh = data3.get("proxies", {})
                        info = resolve_active(fresh, section) or info
                        proxies = fresh
                else:
                    notify_failure(st, section, target,
                                   "Ни один узел не открыл адрес.",
                                   cooldown)
                    save_state(state)
                continue

            # URLTest: сначала даём группе перепроверить все узлы по
            # каждому адресу сервиса. Раньше тест шёл только по первому
            # URL, поэтому для Instagram с require_all группа могла
            # оставить узел, не открывающий второй адрес.
            group_tag = info.get("group_tag")
            if not group_tag:
                group_tag, _ = find_group(proxies, section)
            group_results = {}
            if group_tag:
                before = str(info.get("label") or group_tag)
                group_results = probe_group_nodes(group_tag, target,
                                                  timeout_ms)
                log("URLTEST RETRY %s / %s: проверено %d узлов"
                    % (section, target["label"], len(group_results)))
                if group_retry > 0:
                    time.sleep(group_retry)
                status2, data2 = api("/proxies", timeout=5)
                if status2 in (200, 204) and isinstance(data2, dict):
                    fresh = data2.get("proxies", {})
                    info2 = resolve_active(fresh, section) or info
                    checked = probe_target(info2["active_tag"], target,
                                           timeout_ms)
                    if checked["ok"]:
                        st["notified"] = True
                        after = str(info2.get("label") or info2["active_tag"])
                        log("URLTEST SWITCH %s / %s -> %s"
                            % (section, target["label"], after))
                        notify(st, "🔁 <b>URLTest переключил узел</b>\n"
                               "Секция <b>%s</b>: <code>%s</code>\n"
                               "Узел: <code>%s</code> → <code>%s</code>"
                               % (html.escape(section),
                                  html.escape(target["label"]),
                                  html.escape(before), html.escape(after)),
                               cooldown)
                        save_state(state)
                        proxies = fresh
                        info = info2
                        continue
                    proxies = fresh
                    info = info2

            # URLTest не справился: URLTest-группа продолжает держать узел,
            # который не открывает сервис (её собственный тест это не
            # видит). Закрепляем селектор на реально рабочем узле.
            if manual_switch:
                best = switch_to_best(proxies, section, info, target,
                                      timeout_ms, group_results)
                if best:
                    st["notified"] = True
                    st["pinned_group"] = group_tag or ""
                    st["pinned_node"] = best["node"]
                    st["pinned_bad"] = str(info.get("label") or "")
                    st["pinned_check"] = 0
                    log("PIN %s / %s -> %s (%sms)"
                        % (section, target["label"], best["node"],
                           best["delay"]))
                    notify(st, "🔁 <b>URLTest не справился, выбран "
                           "рабочий узел</b>\n"
                           "Секция <b>%s</b>: <code>%s</code>\n"
                           "Узел: <code>%s</code> → <code>%s</code> (%s ms)\n"
                           "Селектор закреплён за рабочим узлом."
                           % (html.escape(section),
                              html.escape(target["label"]),
                              html.escape(str(info["label"])),
                              html.escape(str(best["node"])),
                              best["delay"]), cooldown)
                    save_state(state)
                    status3, data3 = api("/proxies", timeout=5)
                    if status3 in (200, 204) and isinstance(data3, dict):
                        fresh = data3.get("proxies", {})
                        info = resolve_active(fresh, section) or info
                        proxies = fresh
                    continue

            if not manual_switch:
                reason = "Автопереключение отключено в настройках."
            elif group_tag:
                reason = ("URLTest перепроверен по всем адресам, "
                          "рабочий узел не найден.")
            else:
                reason = "Не найдена URLTest-группа для перепроверки."
            notify_failure(st, section, target, reason, cooldown)
            save_state(state)


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
            if not target_applies(target, section):
                continue
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
