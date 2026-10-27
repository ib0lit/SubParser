# -*- coding: utf-8 -*-
import fcntl
import json
import os
import subprocess
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor

from .config import (LOCK_FILE, TEST_URL_GLOBAL, get_uci_val, get_uci_bool,
                     set_uci_val)

CACHED_DELAYS = {}
PROGRESS_FILE = "/tmp/subparser_progress.json"


def get_lan_ip():
    ip = get_uci_val("lan_ip_override", "")
    if ip:
        return ip
    try:
        out = subprocess.check_output(["uci", "-q", "get", "network.lan.ipaddr"],
                                      text=True, stderr=subprocess.DEVNULL).strip()
        return out.split("/")[0] if out else "192.168.1.1"
    except Exception:
        return "192.168.1.1"


def get_api_base():
    """Clash API Podkop. Больше не привязан жёстко к 192.168.1.1."""
    override = get_uci_val("podkop_api", "").strip()
    if override:
        if not override.startswith("http"):
            override = "http://" + override
        return override.rstrip("/")
    port = (get_uci_val("podkop_api_port", "") or
            get_uci_val("clash_api_port", "") or "9090").strip()
    return f"http://{get_lan_ip()}:{port}"


def get_api_headers():
    secret = (get_uci_val("podkop_api_secret", "") or
              get_uci_val("clash_api_secret", "")).strip()
    headers = {"User-Agent": "SubParser"}
    if secret:
        headers["Authorization"] = f"Bearer {secret}"
    return headers


def _api_request(path, method="GET", payload=None, timeout=1.5):
    url = f"{get_api_base()}{path}"
    data = None
    headers = get_api_headers()
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read()
            return resp.status, (json.loads(body.decode("utf-8")) if body else {})
    except Exception:
        return None, None


# Podkop хранит секции как `config section 'main'`. В старых версиях тип был
# `podkop`. Тип нужно проверять явно: поиск подстроки `=podkop` находил только
# `podkop.settings=settings`, поэтому все секции, кроме main, терялись.
PODKOP_SECTION_TYPES = ("section", "podkop")
PODKOP_SECTION_OPTIONS = ("urltest_proxy_links", "connection_type", "proxy_string")


def parse_podkop_sections(raw_show: str) -> list:
    """Имена секций Podkop из вывода `uci show podkop`."""
    detected = []

    def add(name):
        if name and name != "settings" and name not in detected:
            detected.append(name)

    for line in (raw_show or "").splitlines():
        line = line.strip()
        if not line.startswith("podkop.") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        parts = key.split(".")
        if len(parts) == 2:
            if value.strip().strip("'\"") in PODKOP_SECTION_TYPES:
                add(parts[1])
        elif len(parts) >= 3 and parts[2] in PODKOP_SECTION_OPTIONS:
            # Секция без явного типа (или нестандартный тип) — определяем по опциям.
            add(parts[1])
    return detected


def get_all_podkop_sections():
    try:
        raw_secs = subprocess.check_output(["uci", "-q", "show", "podkop"],
                                           text=True, stderr=subprocess.DEVNULL)
    except Exception:
        return ["main"]
    return parse_podkop_sections(raw_secs) or ["main"]


def get_managed_podkop_sections():
    """Секции, которые SubParser реально обновляет.

    Watchdog должен проверять только их: выключенная в настройках секция не
    лечится парсером, поэтому алерт по ней был бы бесконечным.
    """
    all_sections = get_all_podkop_sections()
    try:
        if get_uci_bool("per_section_config", False):
            enabled = [s for s in all_sections
                       if get_uci_bool(f"sec_en_{s}", True)]
            # Парсер при пустом списке всё равно обновляет main.
            return enabled or ["main"]

        raw = get_uci_val("target_section", "")
        targets = [s for s in str(raw).split() if s in all_sections]
        # Пустой target_section означает «все секции».
        return targets or all_sections
    except Exception:
        return all_sections


def get_podkop_links(section="main"):
    try:
        raw = subprocess.check_output(["uci", "-q", "get", f"podkop.{section}.urltest_proxy_links"],
                                      text=True, stderr=subprocess.DEVNULL).strip()
        return [l.strip() for l in raw.split() if l.strip()] if raw else []
    except Exception:
        return []


def get_node_name(link, idx):
    try:
        frag = urllib.parse.urlsplit(link).fragment
        if frag:
            return urllib.parse.unquote(frag)
    except Exception:
        pass
    return f"Узел #{idx+1}"


def get_podkop_service_status():
    pid = ""
    try:
        pids = subprocess.check_output(["pidof", "sing-box"], text=True,
                                       stderr=subprocess.DEVNULL).strip().split()
        if pids:
            pid = pids[0]
    except Exception:
        pass
    service_ok = False
    try:
        res = subprocess.run(["/etc/init.d/podkop", "status"], stdout=subprocess.PIPE,
                             stderr=subprocess.PIPE, text=True)
        if res.returncode == 0 or "running" in res.stdout.lower() or pid:
            service_ok = True
    except Exception:
        if pid:
            service_ok = True

    ram_mb = ""
    if pid:
        try:
            with open(f"/proc/{pid}/status", "r") as f:
                for line in f:
                    if line.startswith("VmRSS:"):
                        rss_kb = int(line.split()[1])
                        ram_mb = f"{rss_kb / 1024:.1f} MB"
                        break
        except Exception:
            pass

    mode_str = "TPROXY / iptables"
    try:
        ip_out = subprocess.check_output(["ip", "-o", "link", "show"], text=True,
                                         stderr=subprocess.DEVNULL)
        for line in ip_out.splitlines():
            for ifn in ["tun-podkop", "sing-tun", "podkop", "tun0"]:
                if f" {ifn}:" in line or f" {ifn}@" in line:
                    mode_str = f"TUN ({ifn})"
                    break
    except Exception:
        pass
    return service_ok, pid, ram_mb, mode_str


def get_proxies():
    status, data = _api_request("/proxies", timeout=0.9)
    if status in (200, 204) and isinstance(data, dict):
        return data.get("proxies", {})
    return {}


def get_section_routing_state(section="main"):
    proxies = get_proxies()
    if not proxies:
        return "", True
    selector_data = proxies.get(f"{section}-out") or proxies.get(section)
    if not selector_data or selector_data.get("type", "").lower() != "selector":
        for k, v in proxies.items():
            if section in k and v.get("type", "").lower() == "selector":
                selector_data = v
                break
    if selector_data:
        now_val = selector_data.get("now", "")
        if "urltest" in now_val.lower():
            return "", True
        return now_val, False
    return "", True


def _find_selector(section):
    selector_name = f"{section}-out"
    proxies = get_proxies()
    if selector_name not in proxies:
        for k, v in proxies.items():
            if section in k and v.get("type", "").lower() == "selector":
                return k, v
    return selector_name, proxies.get(selector_name, {})


def switch_active_node(section, index):
    tag = f"{section}-{index+1}-out"
    selector_name, _ = _find_selector(section)
    status, _ = _api_request(f"/proxies/{urllib.parse.quote(selector_name)}",
                             method="PUT", payload={"name": tag}, timeout=1.5)
    if status in (200, 204):
        return True, tag
    return False, tag


def reset_to_auto_urltest(section="main"):
    target_urltest = f"{section}-urltest-out"
    selector_name, selector = _find_selector(section)
    for cand in selector.get("all", []) if isinstance(selector, dict) else []:
        if "urltest" in cand.lower():
            target_urltest = cand
            break
    status, _ = _api_request(f"/proxies/{urllib.parse.quote(selector_name)}",
                             method="PUT", payload={"name": target_urltest}, timeout=1.5)
    return status in (200, 204)


def _write_links(section, links, restart=False):
    """Атомарная перезапись списка ссылок. Бэкап делает вызывающая сторона."""
    batch = [f"delete podkop.{section}.urltest_proxy_links"]
    for link in links:
        escaped = str(link).replace("\\", "\\\\").replace('"', '\\"').replace("\n", "").replace("\r", "")
        batch.append(f'add_list podkop.{section}.urltest_proxy_links="{escaped}"')
    proc = subprocess.Popen(["uci", "-q", "batch"], stdin=subprocess.PIPE, text=True,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    proc.communicate(input="\n".join(batch) + "\n")
    commit = subprocess.run(["uci", "commit", "podkop"], stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL)
    if commit.returncode != 0:
        return False
    if restart:
        subprocess.Popen("nohup sh -c 'sleep 2 && /etc/init.d/podkop restart' >/dev/null 2>&1 &",
                         shell=True)
    return True


def delete_node(index, section="main"):
    links = get_podkop_links(section)
    if index < 0 or index >= len(links):
        return False, "Неверный индекс"
    removed = links.pop(index)
    if not _write_links(section, links, restart=True):
        return False, "Не удалось записать конфигурацию"
    return True, get_node_name(removed, index)


def delete_nodes_batch(indices, section="main", restart=False):
    links = get_podkop_links(section)
    if not links or not indices:
        return False, 0
    del_set = set(indices)
    new_links = [link for i, link in enumerate(links) if i not in del_set]
    removed_count = len(links) - len(new_links)
    if not _write_links(section, new_links, restart=restart):
        return False, 0
    return True, removed_count


def batch_ping_nodes(links, section="main", force=False):
    results = {}
    if not links:
        return results

    test_url = urllib.parse.quote(TEST_URL_GLOBAL, safe="")

    # 1. История ядра, если не запрошен принудительный замер.
    if not force:
        proxies = get_proxies()
        for i in range(len(links)):
            tag = f"{section}-{i+1}-out"
            if tag in proxies:
                hist = proxies[tag].get("history", [])
                if hist and hist[-1].get("delay", 0) > 0:
                    results[i] = hist[-1]["delay"]

    missing = [i for i in range(len(links)) if i not in results]
    if missing:
        def probe_node(idx):
            t = urllib.parse.quote(f"{section}-{idx+1}-out")
            status, data = _api_request(
                f"/proxies/{t}/delay?url={test_url}&timeout=1800", timeout=2.5)
            if status == 200 and isinstance(data, dict):
                return idx, int(data.get("delay", -1))
            return idx, -1

        with ThreadPoolExecutor(max_workers=min(10, len(missing))) as pool:
            for idx, delay in pool.map(probe_node, missing):
                results[idx] = delay

    CACHED_DELAYS[section] = results
    return results


def is_parser_running():
    if os.path.exists(LOCK_FILE):
        try:
            with open(LOCK_FILE, "a+") as lf:
                try:
                    fcntl.flock(lf, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    fcntl.flock(lf, fcntl.LOCK_UN)
                except OSError:
                    return True
        except Exception:
            pass
    ps = subprocess.run(["pgrep", "-f", "[s]ubparser.py"], stdout=subprocess.PIPE, text=True)
    return bool(ps.stdout.strip())


def run_parser_process(mode_override=None, force=False, progress=None):
    if is_parser_running():
        return False, "Синхронизация уже запущена!"
    if mode_override:
        set_uci_val("update_mode", mode_override)
    # progress передаёт бот: файл создаётся только после проверки, что парсер
    # действительно стартует. Иначе старый файл мог перехватить чужое сообщение.
    try:
        if progress:
            with open(PROGRESS_FILE, "w", encoding="utf-8") as pf:
                json.dump(progress, pf)
        else:
            os.remove(PROGRESS_FILE)
    except Exception:
        pass
    env = dict(os.environ)
    env["SUBPARSER_TRIGGER"] = "telegram"
    subprocess.Popen(["/usr/bin/python3", "/usr/bin/subparser.py", "--force"],
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=env)
    return True, "🚀 <b>Синхронизация запущена!</b>\nПроцесс выполняется в фоне."
