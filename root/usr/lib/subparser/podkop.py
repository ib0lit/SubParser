# -*- coding: utf-8 -*-
import fcntl
import json
import os
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor

from .config import (LOCK_FILE, TEST_URL_GLOBAL, get_uci_val, get_uci_bool,
                     set_uci_val)

CACHED_DELAYS = {}
PROGRESS_FILE = "/tmp/subparser_progress.json"
PODKOP_INIT = "/etc/init.d/podkop"


def _singbox_pid():
    try:
        return subprocess.check_output(["pidof", "sing-box"], text=True,
                                       stderr=subprocess.DEVNULL).strip()
    except Exception:
        return ""


def restart_podkop(timeout=60):
    """Перезапускает Podkop и ждёт, пока sing-box действительно поднимется.

    Возвращает True только при подтверждённом новом процессе. Раньше запуск
    шёл через `nohup sh -c ... &`: если nohup отсутствовал в системе, команда
    молча не выполнялась, а бот всё равно сообщал об успехе.
    """
    old_pid = _singbox_pid()
    try:
        proc = subprocess.Popen([PODKOP_INIT, "restart"],
                                stdin=subprocess.DEVNULL,
                                stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL,
                                start_new_session=True, close_fds=True)
        try:
            proc.wait(timeout=min(timeout, 30))
        except subprocess.TimeoutExpired:
            # init-скрипт может ждать procd дольше — сервис проверим по pid.
            pass
    except Exception:
        return False

    deadline = time.time() + timeout
    while time.time() < deadline:
        pid = _singbox_pid()
        if pid and pid != old_pid:
            return True
        time.sleep(2)
    return False


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


def _uci_get(path, default=""):
    """Читает произвольную UCI-опцию Podkop (не только subparser.settings)."""
    try:
        out = subprocess.check_output(["uci", "-q", "get", path], text=True,
                                      stderr=subprocess.DEVNULL).strip()
        return out.strip("'\"") if out else default
    except Exception:
        return default


def get_api_base():
    """Clash API Podkop. Больше не привязан жёстко к 192.168.1.1."""
    override = get_uci_val("podkop_api", "").strip()
    if override:
        if not override.startswith("http"):
            override = "http://" + override
        return override.rstrip("/")
    port = (get_uci_val("podkop_api_port", "") or
            get_uci_val("clash_api_port", "") or "9090").strip()
    # Podkop умеет слушать API на явно заданном адресе. 0.0.0.0/:: — это
    # «все интерфейсы», для подключения нужен реальный адрес роутера.
    listen = _uci_get("podkop.settings.service_listen_address", "").strip()
    if listen and listen not in ("0.0.0.0", "::", "*"):
        listen = listen.split("/")[0]
        return f"http://{listen}:{port}"
    return f"http://{get_lan_ip()}:{port}"


def get_api_headers():
    secret = (get_uci_val("podkop_api_secret", "") or
              get_uci_val("clash_api_secret", "") or
              _uci_get("podkop.settings.yacd_secret_key", "")).strip()
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
    except urllib.error.HTTPError as e:
        # 401/403 и прочие ответы API важны для диагностики: раньше любая
        # ошибка превращалась в «API недоступен» и маскировала неверный секрет.
        body = None
        try:
            raw = e.read()
            body = json.loads(raw.decode("utf-8")) if raw else None
        except Exception:
            body = None
        return e.code, body
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


def fetch_proxies():
    """Возвращает (proxies, http_status), чтобы отличать 401 от недоступности."""
    status, data = _api_request("/proxies", timeout=0.9)
    proxies = {}
    if status in (200, 204) and isinstance(data, dict):
        proxies = data.get("proxies", {})
    return proxies, status


def get_proxies():
    return fetch_proxies()[0]


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


def _selector_data(section, proxies):
    """Возвращает (имя селектора, данные) из уже полученного /proxies."""
    name = f"{section}-out"
    data = proxies.get(name) if isinstance(proxies, dict) else None
    if isinstance(data, dict) and data.get("type", "").lower() == "selector":
        return name, data
    for key, value in (proxies or {}).items():
        if isinstance(value, dict) and \
                value.get("type", "").lower() == "selector" and \
                (key == name or section in key):
            return key, value
    return name, data if isinstance(data, dict) else {}


def _link_from_tag(section, tag, links):
    """Преобразует тег вида `main-3-out` в ссылку из списка секции."""
    prefix = f"{section}-"
    suffix = "-out"
    if not tag.startswith(prefix) or not tag.endswith(suffix):
        return None
    middle = tag[len(prefix):-len(suffix)]
    if not middle.isdigit():
        return None
    index = int(middle) - 1
    if 0 <= index < len(links):
        return links[index]
    return None


def _capture_selection(section, links, proxies):
    """Запоминает ручной выбор до удаления узлов.

    Возвращает (mode, link):
      auto        — был выбран urltest, трогать не нужно;
      manual      — был выбран конкретный узел, link нужно восстановить;
      unknown     — выбор есть, но сопоставить его со ссылкой не удалось.
    """
    _, selector = _selector_data(section, proxies)
    now = selector.get("now", "") if isinstance(selector, dict) else ""
    if not now or "urltest" in now.lower():
        return "auto", None
    link = _link_from_tag(section, now, links)
    if link is not None:
        return "manual", link
    return "unknown", None


def _switch_with_retry(section, index, attempts=5):
    """Переключает селектор после рестарта, когда API ещё может быть не готов."""
    tag = f"{section}-{index+1}-out"
    for attempt in range(attempts):
        ok, tag = switch_active_node(section, index)
        if ok:
            return True, tag
        if attempt + 1 < attempts:
            time.sleep(1)
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
    # Пустая секция ломает запуск Podkop, поэтому последний узел не удаляем.
    if not links:
        return False
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
    if len(links) <= 1:
        return False, "Нельзя удалить последний узел секции"
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
    if not new_links:
        return False, 0
    if not _write_links(section, new_links, restart=restart):
        return False, 0
    return True, removed_count


def scan_dead_in_all_sections():
    """Пингует все секции и возвращает план удаления, не меняя конфигурацию.

    Узлы, которых нет в текущем runtime Podkop, не проверяются и в план не
    попадают: удалять непроверенное по ошибке API недопустимо.
    """
    sections = get_all_podkop_sections()
    proxies, api_status = fetch_proxies()
    details = []
    total_dead = 0

    if not proxies:
        if api_status in (401, 403):
            return {"total": 0,
                    "error": "Podkop API требует авторизацию (YACD Secret Key)",
                    "details": []}
        return {"total": 0, "error": "Podkop API недоступен", "details": []}

    for section in sections:
        links = get_podkop_links(section)
        if not links:
            details.append({"section": section, "total": 0, "dead": 0,
                            "skipped": 0, "status": "empty", "indices": []})
            continue

        delays = batch_ping_nodes(links, section=section, force=True)
        dead = []
        skipped = 0
        for i in range(len(links)):
            tag = f"{section}-{i+1}-out"
            if tag not in proxies:
                skipped += 1
                continue
            delay = delays.get(i)
            if isinstance(delay, int) and delay <= 0:
                dead.append(i)

        if dead and len(dead) == len(links):
            # Удалять нечего: секция осталась бы пустой и Podkop не стартовал бы.
            status = "all_dead"
            plan_indices = []
        elif dead:
            status = "dead"
            total_dead += len(dead)
            plan_indices = dead
        else:
            status = "no_data" if skipped == len(links) else "clean"
            plan_indices = []
        details.append({"section": section, "total": len(links),
                        "dead": len(dead), "skipped": skipped,
                        "status": status, "indices": plan_indices,
                        "links": list(links)})

    return {"total": total_dead, "error": "", "details": details}


def delete_dead_plan(plan_details):
    """Удаляет узлы из готового плана и перезапускает Podkop один раз."""
    total_deleted = 0
    details = []
    restore_plan = []
    proxies = None

    for item in plan_details or []:
        section = item.get("section")
        indices = list(item.get("indices") or [])
        total = int(item.get("total", 0))
        skipped = int(item.get("skipped", 0))

        result = {"section": section, "total": total,
                  "dead": int(item.get("dead", 0)), "deleted": 0,
                  "skipped": skipped, "status": item.get("status", "clean")}
        if not indices:
            details.append(result)
            continue

        # Страховка: не оставляем секцию без единого узла даже при битом плане.
        if len(indices) >= total:
            result["status"] = "all_dead"
            details.append(result)
            continue

        # Пока пользователь читал подтверждение, парсер мог переписать секцию.
        # Индексы в этом случае уже не соответствуют узлам — ничего не удаляем.
        links = get_podkop_links(section)
        expected = item.get("links")
        if len(links) != total or (expected is not None and list(expected) != links):
            result["status"] = "stale"
            details.append(result)
            continue

        # Ручной выбор хранится в runtime, а не в UCI. Снимаем его до записи:
        # после удаления индексы сдвинутся и тег `section-N-out` будет уже
        # указывать на другой сервер.
        if proxies is None:
            proxies = get_proxies()
        selection_mode, selected_link = _capture_selection(section, links, proxies)

        ok, deleted = delete_nodes_batch(indices, section=section, restart=False)
        if not ok:
            result["status"] = "error"
            details.append(result)
            continue

        del_set = set(indices)
        new_links = [link for i, link in enumerate(links) if i not in del_set]
        old_delays = CACHED_DELAYS.get(section, {})
        new_delays = {}
        new_idx = 0
        for old_idx in range(total):
            if old_idx not in del_set:
                if old_idx in old_delays:
                    new_delays[new_idx] = old_delays[old_idx]
                new_idx += 1
        CACHED_DELAYS[section] = new_delays

        restore_plan.append((result, section, selection_mode,
                             selected_link, new_links))
        total_deleted += deleted
        result["deleted"] = deleted
        result["status"] = "deleted"
        details.append(result)

    restarted = restart_podkop() if total_deleted else False

    # Возвращаем ручной выбор на тот же сервер по ссылке, а не по старому
    # индексу. Если был urltest — оставляем urltest. Если выбранный узел сам
    # удалён — секция остаётся на urltest.
    for result, section, mode, link, new_links in restore_plan:
        if mode == "auto":
            result["selection"] = "auto"
        elif mode != "manual":
            result["selection"] = "unchanged"
        elif not restarted:
            result["selection"] = "not_restarted"
        else:
            try:
                new_index = new_links.index(link)
            except ValueError:
                result["selection"] = "lost"
            else:
                ok, _ = _switch_with_retry(section, new_index)
                result["selection"] = "restored" if ok else "failed"

    return {"total": total_deleted, "changed": bool(total_deleted),
            "restarted": restarted, "error": "", "details": details}


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
