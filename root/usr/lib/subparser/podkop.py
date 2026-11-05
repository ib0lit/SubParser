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


def _parse_uci_export_links(raw, section):
    """Разбирает `uci export` и сохраняет границы элементов списка.

    `uci get` печатает список через пробел, поэтому ссылки с пробелом в
    имени узла (`#Мой сервер`) рассыпались на несколько «серверов». После
    сохранения такие обрывки оставались в секции. `uci export` печатает
    каждый элемент отдельной строкой в кавычках — shlex возвращает его целиком.
    """
    import shlex
    current = None
    found = []
    for line in (raw or "").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        try:
            parts = shlex.split(line, comments=False)
        except ValueError:
            continue
        if not parts:
            continue
        if parts[0] == "config" and len(parts) >= 3:
            current = parts[2]
        elif parts[0] == "list" and len(parts) >= 3 and \
                current == section and parts[1] == "urltest_proxy_links":
            found.extend(parts[2:])
    return found


def get_podkop_links(section="main"):
    try:
        raw = subprocess.check_output(["uci", "-q", "export", "podkop"],
                                      text=True, stderr=subprocess.DEVNULL)
        links = _parse_uci_export_links(raw, section)
        # Пустая секция — тоже валидный ответ; fallback нужен только если
        # экспорт не дал вообще ничего, а старый способ что-то находит.
        if links:
            return links
    except Exception:
        pass
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


def _find_selector_in(section, proxies):
    """Ищет селектор секции в уже полученном /proxies."""
    for tag in (f"{section}-out", section):
        data = proxies.get(tag)
        if isinstance(data, dict) and \
                str(data.get("type", "")).lower() == "selector":
            return tag, data
    for tag, data in (proxies or {}).items():
        if section in tag and isinstance(data, dict) and \
                str(data.get("type", "")).lower() == "selector":
            return tag, data
    return None, None


def _find_group_in(section, proxies):
    """Ищет URLTest-группу секции в уже полученном /proxies."""
    tag = f"{section}-urltest-out"
    data = proxies.get(tag)
    if isinstance(data, dict) and \
            str(data.get("type", "")).lower() == "urltest":
        return tag, data
    for key, value in (proxies or {}).items():
        if section in key and isinstance(value, dict) and \
                str(value.get("type", "")).lower() == "urltest":
            return key, value
    return None, None


def get_sections_active_state(sections=None):
    """Активное состояние селектора и URLTest по всем секциям.

    Один запрос к Clash API на все секции. Возвращает список словарей
    с полями name/api/selector/urltest. Ссылки узлов сюда не входят:
    для периодического обновления дашборда они не нужны.
    """
    proxies, status = fetch_proxies()
    api_ok = status in (200, 204)
    if sections is None:
        sections = get_all_podkop_sections()

    result = []
    for section in sections:
        selector_tag, selector = _find_selector_in(section, proxies)
        group_tag, group = _find_group_in(section, proxies)
        now = selector.get("now", "") if isinstance(selector, dict) else ""
        node = proxies.get(now) if now else None
        node_type = str(node.get("type", "")).lower() if isinstance(node, dict) else ""
        if not now:
            mode = "unknown"
        elif node_type in ("urltest", "fallback", "loadbalance") or "urltest" in now.lower():
            mode = "auto"
        else:
            mode = "manual"
        result.append({
            "name": section,
            "api": api_ok,
            "selector": {
                "tag": selector_tag or "",
                "now": now,
                "mode": mode,
            },
            "urltest": {
                "enabled": bool(group_tag),
                "tag": group_tag or "",
                "now": group.get("now", "") if isinstance(group, dict) else "",
            },
        })
    return result


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


def _link_identity(link):
    """Ссылка без #фрагмента — имя узла может меняться между синхронизациями."""
    try:
        parsed = urllib.parse.urlsplit(str(link))
        return urllib.parse.urlunsplit(
            (parsed.scheme, parsed.netloc, parsed.path, parsed.query, ""))
    except Exception:
        return str(link)


def _find_link_index(links, link):
    target = _link_identity(link)
    for index, candidate in enumerate(links):
        if _link_identity(candidate) == target:
            return index
    return -1


def capture_selection(section, links, proxies=None):
    """Запоминает текущий ручной выбор секции перед перезаписью списка."""
    if proxies is None:
        proxies = get_proxies()
    return _capture_selection(section, links, proxies)


def restore_selection(section, mode, link, new_links, proxies=None):
    """Восстанавливает ручной выбор по ссылке и возвращает статус."""
    if mode == "auto":
        return "auto"
    if mode != "manual":
        return "unchanged"
    new_index = _find_link_index(new_links, link)
    if new_index < 0:
        return "lost"
    if proxies is None:
        proxies, api_status = fetch_proxies()
        api_ready = api_status in (200, 204)
    else:
        api_ready = bool(proxies)
    # В режиме добавления индексы не сдвигаются, и Podkop после рестарта
    # обычно сам оставляет выбор на прежнем сервере. Тогда PUT не нужен:
    # на медленном API лишний запрос давал ложное «не удалось вернуть узел».
    _, selector = _selector_data(section, proxies)
    now = selector.get("now", "") if isinstance(selector, dict) else ""
    current = _link_from_tag(section, now, new_links)
    if current is not None and \
            _link_identity(current) == _link_identity(link):
        return "restored"
    ok, _ = _switch_with_retry(section, new_index)
    if ok:
        return "restored"
    # Если API так и не ответил, это не ошибка восстановления: проверить
    # и переключить селектор было невозможно.
    if not api_ready:
        return "not_ready"
    return "failed"


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


def _switch_with_retry(section, index, attempts=15):
    """Переключает селектор после рестарта, когда API ещё может быть не готов."""
    tag = f"{section}-{index+1}-out"
    for attempt in range(attempts):
        ok, tag = switch_active_node(section, index)
        if ok:
            return True, tag
        if attempt + 1 < attempts:
            time.sleep(2)
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


def _wait_for_api(timeout=6):
    """Ждёт готовности Clash API после рестарта, чтобы LuCI сразу увидел список."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        _proxies, status = fetch_proxies()
        if status in (200, 204):
            return True
        time.sleep(0.7)
    return False


def _spawn_restart_worker(plan, token=""):
    """Запускает перезапуск Podkop и восстановление выбора в фоне.

    LuCI-запрос не должен ждать рестарт: rpcd обрывает XHR по таймауту, а
    Podkop может подниматься дольше. Helper отвечает сразу, готовность
    отмечается в /tmp/subparser_restart_ready.json для опроса из UI.
    """
    ready_file = "/tmp/subparser_restart_ready.json"
    safe_token = "".join(ch for ch in str(token) if ch.isalnum())[:32] or "manual"
    plan_file = f"/tmp/subparser_restart_plan_{safe_token}.json"
    try:
        with open(ready_file, "w", encoding="utf-8") as fh:
            json.dump({"ready": False, "token": token}, fh)
    except Exception:
        pass
    try:
        with open(plan_file, "w", encoding="utf-8") as fh:
            json.dump({"token": token, "restore": plan}, fh, ensure_ascii=False)
    except Exception:
        plan_file = ""
        plan = []
    try:
        args = ["python3", "/usr/lib/subparser/podkop_restart.py"]
        if plan_file:
            args.append(plan_file)
        subprocess.Popen(args, stdin=subprocess.DEVNULL,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         start_new_session=True, close_fds=True)
    except Exception:
        try:
            subprocess.Popen([PODKOP_INIT, "restart"], stdin=subprocess.DEVNULL,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                             start_new_session=True, close_fds=True)
        except Exception:
            pass


def replace_sections_links(jobs, restart=True, token=""):
    """Перезаписывает списки узлов одной или нескольких секций.

    Все секции пишутся до единственного перезапуска Podkop, а ручной выбор
    восстанавливается после него. Перезапуск выполняется в фоне: иначе
    XHR в LuCI обрывался по таймауту.

    jobs: список словарей {section, links, selection?}, где selection —
    необязательный {'mode': 'manual'|'auto', 'link': ...}, снятый в LuCI.
    """
    prepared = []
    for job in jobs or []:
        section = str(job.get("section", "") or "").strip()
        if not section:
            return False, "Не указана секция Podkop"
        clean = [str(link).strip() for link in (job.get("links") or [])
                 if str(link).strip()]
        if not clean:
            return False, f"Нельзя удалить все узлы секции {section}"

        selection = job.get("selection") if isinstance(job, dict) else None
        if isinstance(selection, dict) and selection.get("mode"):
            mode = str(selection.get("mode"))
            selected_link = selection.get("link")
        else:
            # Резервный путь: определяем выбор через Clash API до записи.
            old_links = get_podkop_links(section)
            try:
                proxies = get_proxies()
                mode, selected_link = _capture_selection(section, old_links, proxies)
            except Exception:
                mode, selected_link = "unknown", None

        if not _write_links(section, clean):
            return False, f"Не удалось записать конфигурацию секции {section}"
        prepared.append((section, mode, selected_link, clean))

    if restart and prepared:
        plan = [{"section": section, "mode": mode, "link": selected_link,
                 "links": links}
                for section, mode, selected_link, links in prepared
                if mode == "manual" and selected_link]
        _spawn_restart_worker(plan, token)
    return True, ""


def replace_section_links(section, links, restart=True, selection=None, token=""):
    """Совместимость: перезапись ровно одной секции."""
    job = {"section": section, "links": links}
    if selection is not None:
        job["selection"] = selection
    return replace_sections_links([job], restart=restart, token=token)


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


def scan_dead_in_all_sections(sections=None, include_delays=False, retries=1):
    """Пингует секции и возвращает план удаления, не меняя конфигурацию.

    Узлы, которых нет в текущем runtime Podkop, не проверяются и в план не
    попадают: удалять непроверенное по ошибке API недопустимо.

    ``sections`` ограничивает проверку выбранными секциями, ``include_delays``
    добавляет в результат задержки и имена узлов для лога.
    """
    all_sections = get_all_podkop_sections()
    if sections is None:
        target_sections = all_sections
    else:
        wanted = set(sections)
        target_sections = [s for s in all_sections if s in wanted]

    proxies, api_status = fetch_proxies()
    details = []
    total_dead = 0

    if not proxies:
        if api_status in (401, 403):
            return {"total": 0,
                    "error": "Podkop API требует авторизацию (YACD Secret Key)",
                    "details": []}
        return {"total": 0, "error": "Podkop API недоступен", "details": []}

    for section in target_sections:
        links = get_podkop_links(section)
        if not links:
            detail = {"section": section, "total": 0, "dead": 0,
                      "skipped": 0, "status": "empty", "indices": [],
                      "links": []}
            if include_delays:
                detail["delays"] = {}
                detail["names"] = []
            details.append(detail)
            continue

        delays = batch_ping_nodes(links, section=section, force=True,
                                  retries=retries)
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
        detail = {"section": section, "total": len(links),
                  "dead": len(dead), "skipped": skipped,
                  "status": status, "indices": plan_indices,
                  "links": list(links)}
        if include_delays:
            detail["delays"] = {str(i): delays.get(i) for i in range(len(links))}
            detail["names"] = [get_node_name(link, i)
                               for i, link in enumerate(links)]
        details.append(detail)

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
        if not restarted and mode != "auto":
            result["selection"] = "not_restarted"
        else:
            result["selection"] = restore_selection(section, mode, link, new_links)

    return {"total": total_deleted, "changed": bool(total_deleted),
            "restarted": restarted, "error": "", "details": details}


def _normalize_test_url(test_url):
    target = str(test_url or "").strip() or TEST_URL_GLOBAL
    if not target.startswith(("http://", "https://")):
        target = TEST_URL_GLOBAL
    return target


def group_ping_nodes(section="main", test_url=None, delay_timeout=5000,
                     request_timeout=9):
    """Замеряет задержки всех узлов группы URLTest одним запросом.

    Clash API `GET /group/<tag>/delay` сам запускает проверку всех прокси
    группы параллельно внутри sing-box и возвращает готовую карту
    `{tag: delay}`. Это то же, что делает дашборд Podkop, и заметно быстрее
    отдельных запросов `/proxies/<tag>/delay` по каждому узлу.
    """
    encoded_url = urllib.parse.quote(_normalize_test_url(test_url), safe="")
    tag = urllib.parse.quote(f"{section}-urltest-out", safe="")
    status, data = _api_request(
        f"/group/{tag}/delay?url={encoded_url}&timeout={int(delay_timeout)}",
        timeout=request_timeout)
    if status != 200 or not isinstance(data, dict):
        return {}
    result = {}
    for key, value in data.items():
        try:
            result[str(key)] = int(value)
        except (TypeError, ValueError):
            result[str(key)] = -1
    return result


def batch_ping_nodes(links, section="main", force=False, max_workers=10,
                    request_timeout=2.5, delay_timeout=1800, retries=1,
                    test_url=None):
    results = {}
    if not links:
        return results

    # Свой URL для проверки задержки: дашборд передаёт его аргументом,
    # по умолчанию используется общий generate_204.
    test_url = urllib.parse.quote(_normalize_test_url(test_url), safe="")

    # 1. История ядра, если не запрошен принудительный замер.
    if not force:
        proxies = get_proxies()
        for i in range(len(links)):
            tag = f"{section}-{i+1}-out"
            if tag in proxies:
                hist = proxies[tag].get("history", [])
                if hist and hist[-1].get("delay", 0) > 0:
                    results[i] = hist[-1]["delay"]

    def probe_node(idx):
        t = urllib.parse.quote(f"{section}-{idx+1}-out")
        status, data = _api_request(
            f"/proxies/{t}/delay?url={test_url}&timeout={delay_timeout}",
            timeout=request_timeout)
        if status == 200 and isinstance(data, dict):
            return idx, int(data.get("delay", -1))
        return idx, -1

    # Повторные попытки нужны автоматическому удалению мёртвых: одиночный
    # таймаут не должен приводить к удалению рабочего узла.
    for _attempt in range(max(1, int(retries))):
        missing = [i for i in range(len(links)) if results.get(i, -1) <= 0]
        if not missing:
            break
        with ThreadPoolExecutor(max_workers=min(max_workers, len(missing))) as pool:
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
