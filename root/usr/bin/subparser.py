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
import subprocess
import urllib.request
import urllib.parse
from concurrent.futures import ThreadPoolExecutor, as_completed

CONFIG_NAME = "subparser"
SINGBOX_BIN = "/usr/bin/sing-box"
TEST_URL = "https://www.gstatic.com/generate_204"
MAX_WORKERS = 12
HWID_PHONE = "f4c9b1a0d8e27365"

UA_MAP = {
    "v2rayN": "v2rayN/6.23",
    "ClashMeta": "ClashMeta/1.18.0",
    "clashmeta": "ClashMeta/1.18.0",
    "SingBox": "sing-box/1.9.0",
    "singbox": "sing-box/1.9.0",
    "Shadowrocket": "Shadowrocket/2.2.0",
    "shadowrocket": "Shadowrocket/2.2.0",
    "NekoBox": "NekoBox/1.3.1",
    "flclash": "ClashMeta/1.18.0",
    "clash": "ClashforWindows/0.20.39",
    "incy": "Incy/1.0.0 (Android)",
    "happ": "Happ/1.0.0 (iOS)",
    "curl": "curl/7.88.1"
}

PROGRESS_FILE = "/tmp/subparser_progress.json"

def update_live_progress(step_title, step_desc):
    try:
        if not os.path.exists(PROGRESS_FILE):
            return
        with open(PROGRESS_FILE, "r") as pf:
            pdata = json.load(pf)
        token = pdata.get("token")
        chat_id = pdata.get("chat_id")
        msg_id = pdata.get("msg_id")
        if not token or not chat_id or not msg_id:
            return

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
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        with urllib.request.urlopen(req, timeout=3, context=ctx):
            pass
    except Exception:
        pass

def send_telegram_notify(text):
    try:
        token = subprocess.check_output(["uci", "-q", "get", f"{CONFIG_NAME}.settings.tg_bot_token"], text=True).strip()
        chat_id = subprocess.check_output(["uci", "-q", "get", f"{CONFIG_NAME}.settings.tg_chat_id"], text=True).strip()
        if not token or not chat_id:
            return

        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE

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

def get_real_podkop_sections() -> list:
    try:
        raw_secs = subprocess.check_output(["uci", "-q", "show", "podkop"], text=True)
        detected = []
        for line in raw_secs.splitlines():
            if "=podkop" in line:
                s = line.split(".")[1].split("=")[0]
                if s not in detected:
                    detected.append(s)
            elif "urltest_proxy_links" in line:
                s = line.split(".")[1]
                if s not in detected:
                    detected.append(s)
        return detected if detected else ["main"]
    except Exception:
        return ["main"]

def get_target_sections() -> list:
    real = get_real_podkop_sections()
    try:
        raw = subprocess.check_output(["uci", "-q", "get", f"{CONFIG_NAME}.settings.target_section"], text=True).strip()
        sections = [s.strip() for s in raw.split() if s.strip()]
        valid = [s for s in sections if s in real]
        # Добавляем все обнаруженные секции Podkop, чтобы новые подхватывались автоматически
        for s in real:
            if s not in valid:
                valid.append(s)
        return valid if valid else real
    except Exception:
        return real

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
    ua = UA_MAP.get(ua_key, UA_MAP["v2rayN"])
    name = sub_info.get("name", "Подписка")

    update_live_progress("⏳ [1/4] Загрузка подписок...", f"Получение: {name}"); print(f"[{name}] Запрос подписки через {ua_key}...")

    headers = {
        "User-Agent": ua,
        "Accept": "*/*",
        "Accept-Encoding": "gzip, deflate",
        "Accept-Language": "ru-RU,ru;q=0.9",
        "x-hwid": HWID_PHONE,
        "hwid": HWID_PHONE,
        "device-id": HWID_PHONE,
        "x-device-os": "Android",
        "x-ver-os": "16",
        "x-device-model": "Xiaomi 2412DPC0AG",
        "device-model": "Xiaomi 2412DPC0AG"
    }

    raw_data = None
    try:
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
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
        try:
            curl_cmd = [
                "curl", "-sL", "-k", "-m", "15", "-D", "/tmp/sub_headers.tmp",
                "-H", f"User-Agent: {ua}",
                "-H", f"x-hwid: {HWID_PHONE}",
                "-H", f"device-id: {HWID_PHONE}",
                "-H", "x-device-os: Android",
                "-H", "x-device-model: Xiaomi 2412DPC0AG",
                "--compressed", url
            ]
            res = subprocess.run(curl_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            if res.returncode == 0 and res.stdout:
                raw_data = res.stdout
                if os.path.exists("/tmp/sub_headers.tmp"):
                    try:
                        with open("/tmp/sub_headers.tmp", "r", errors="ignore") as hf:
                            for hline in hf:
                                if "subscription-userinfo:" in hline.lower():
                                    u_val = hline.split(":", 1)[1].strip()
                                    GLOBAL_SUB_USERINFO[name] = parse_userinfo_header(u_val)
                                    break
                        os.remove("/tmp/sub_headers.tmp")
                    except Exception:
                        pass
        except Exception:
            pass

    if not raw_data:
        print(f"  [!] Не удалось загрузить подписку {name}")
        return []

    try:
        pad = len(raw_data) % 4
        if pad:
            raw_data += b"=" * (4 - pad)
        text = base64.b64decode(raw_data).decode('utf-8', errors='ignore')
    except Exception:
        text = raw_data.decode('utf-8', errors='ignore')

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
        req = urllib.request.Request(api_url)
        with urllib.request.urlopen(req, timeout=(timeout_ms / 1000.0) + 1.0) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            return int(data.get("delay", -1))
    except Exception:
        return -1

VALID_FPS = {"chrome", "firefox", "safari", "ios", "android", "edge", "360", "qq", "random", "randomized"}

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

def parse_link_to_singbox_outbound(link: str, tag: str) -> dict:
    parsed = urllib.parse.urlparse(link)
    query = urllib.parse.parse_qs(parsed.query)

    uuid = parsed.username or ""
    server = parsed.hostname or ""
    port = parsed.port or 443

    security = query.get("security", ["none"])[0].lower()
    sni = query.get("sni", [""])[0]
    alpn_raw = query.get("alpn", [""])[0]
    alpn = [p.strip() for p in alpn_raw.split(",") if p.strip()] if alpn_raw else []
    insecure = query.get("allowInsecure", ["0"])[0] in ("1", "true")

    try:
        from subparser.config import get_uci_option
        override_fp = (get_uci_option("settings", "override_fp") or "as_is").lower().strip()
    except Exception:
        import subprocess
        res = subprocess.run(["uci", "-q", "get", "subparser.settings.override_fp"], capture_output=True, text=True)
        override_fp = (res.stdout.strip() or "as_is").lower()

    raw_fp = query.get("fp", ["firefox"])[0].lower().strip()
    if override_fp in VALID_FPS:
        fp = override_fp
    else:
        fp = raw_fp if raw_fp in VALID_FPS else "firefox"

    transport_type = query.get("type", [query.get("net", ["tcp"])[0]])[0].lower()
    transport_obj = None

    if transport_type == "ws":
        ws_path = query.get("path", ["/"])[0]
        ws_host = query.get("host", [""])[0]
        transport_obj = {
            "type": "ws",
            "path": ws_path,
            "headers": {"Host": ws_host} if ws_host else {}
        }
    elif transport_type in ("grpc", "gun"):
        service_name = query.get("serviceName", [""])[0]
        transport_obj = {
            "type": "grpc",
            "service_name": service_name
        }

    tls_obj = None
    if security == "tls":
        tls_obj = {
            "enabled": True,
            "server_name": sni or server,
            "insecure": insecure,
            "alpn": alpn if alpn else ["h2", "http/1.1"],
            "utls": {
                "enabled": True,
                "fingerprint": fp
            }
        }
    elif security == "reality":
        pbk = query.get("pbk", [""])[0]
        sid = query.get("sid", [""])[0]
        tls_obj = {
            "enabled": True,
            "server_name": sni or server,
            "insecure": False,
            "utls": {
                "enabled": True,
                "fingerprint": fp
            },
            "reality": {
                "enabled": True,
                "public_key": pbk,
                "short_id": sid
            }
        }

    outbound = {
        "type": "vless",
        "tag": tag,
        "server": server,
        "server_port": port,
        "uuid": uuid
    }

    if tls_obj:
        outbound["tls"] = tls_obj
    if transport_obj:
        outbound["transport"] = transport_obj

    return outbound

def link_fingerprint(link: str) -> str:
    p = urllib.parse.urlsplit(link)
    return f"{p.scheme}://{p.netloc}{p.path}?{p.query}"

def main():
    enabled = get_uci("enabled", "0")
    if enabled != "1" and "--force" not in sys.argv:
        print("Сервис отключен в настройках.")
        return

    subs = get_subscriptions()
    active_subs = [s for s in subs if s.get("enabled", "1") == "1" and s.get("url")]

    if not active_subs:
        print("Ошибка: Нет активных подписок для парсинга.")
        return

    threshold = int(get_uci("ping_threshold", "350"))
    max_best = int(get_uci("max_best_nodes", "0"))
    max_jitter = int(get_uci("max_jitter", "150"))
    filter_ru = get_uci("filter_ru", "1") == "1"
    per_section_config = get_uci("per_section_config", "0") == "1"
    
    # Словарь секций вида: { 'main': 'replace', 'test': 'append' }
    section_targets = {}
    if per_section_config:
        # Считываем доступные секции podkop
        try:
            raw_secs = subprocess.check_output(["uci", "-q", "show", "podkop"], text=True)
            detected = set()
            for line in raw_secs.splitlines():
                if "=podkop" in line:
                    detected.add(line.split(".")[1].split("=")[0])
                elif "urltest_proxy_links" in line:
                    detected.add(line.split(".")[1])
            if not detected: detected = {"main"}
        except Exception:
            detected = {"main"}

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

    for s in active_subs:
        links = fetch_single_sub(s)
        for l in links:
            fp = link_fingerprint(l)
            if fp not in seen:
                seen.add(fp)
                all_links.append(l)

    if not all_links:
        print("Ошибка: Боевые узлы не найдены ни в одной подписке.")
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
        except Exception:
            continue

    outbounds.append({"type": "direct", "tag": "direct"})
    cfg = {
        "log": {"level": "warn"},
        "experimental": {
            "clash_api": {
                "external_controller": f"127.0.0.1:{api_port}"
            }
        },
        "route": {
            "auto_detect_interface": True
        },
        "outbounds": outbounds
    }

    tmp_cfg = f"/tmp/subparser_test_{api_port}.json"
    with open(tmp_cfg, "w") as f:
        json.dump(cfg, f, indent=2)

    env_sb = os.environ.copy()
    env_sb["ENABLE_DEPRECATED_MISSING_DOMAIN_RESOLVER"] = "true"
    env_sb["ENABLE_DEPRECATED_OUTBOUND_DNS_RULE_ITEM"] = "true"
    proc = subprocess.Popen([SINGBOX_BIN, "run", "-c", tmp_cfg], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env_sb)
    time.sleep(2.5)

    if proc.poll() is not None:
        _, err = proc.communicate()
        print(f"\n[ОШИБКА] sing-box не запустился на порту {api_port}:\n{err}")
        if os.path.exists(tmp_cfg): os.remove(tmp_cfg)
        return

    results = {}
    total = len(tag_map)
    try:
        update_live_progress("🔍 [2/4] Скрининг узлов...", f"Проверка отклика {total} серверов..."); print(f"\n[Этап 1/2] Быстрый скрининг {total} узлов...")
        stage1_candidates = []
        def quick_probe(item):
            tag, link = item
            delays = []
            for i in range(2):
                d = query_node_delay(api_port, tag, timeout_ms=3000)
                if d > 0: delays.append(d)
                if i == 0: time.sleep(0.15)
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
                    results[link] = {"delay": min_d, "jitter": 0, "is_stable": False, "success_count": len(delays), "name": name}
                    print(f"[ALIVE] {min_d:4d} ms | {name}")
                else:
                    print(f"[DEAD ]         | {name}")

        print(f"\nОтобрано кандидатов для теста стабильности: {len(stage1_candidates)}")
        if stage1_candidates:
            update_live_progress("📊 [3/4] Тест стабильности...", f"Анализ джиттера ({len(stage1_candidates)} канд.)..."); print(f"\n[Этап 2/2] Анализ стабильности (5 замеров, медиана)...")
            def detailed_probe(cand):
                tag, link, _ = cand
                delays = []
                for i in range(5):
                    d = query_node_delay(api_port, tag, timeout_ms=3000)
                    if d > 0: delays.append(d)
                    if i < 4: time.sleep(0.2)
                name = urllib.parse.unquote(urllib.parse.urlsplit(link).fragment) or tag
                succ = len(delays)
                if succ < 4: return link, name, -1, -1, False, succ
                delays.sort()
                med = delays[succ // 2]
                trimmed = delays[:-1]
                eff_j = max(trimmed) - min(trimmed)
                worst = delays[-1] - med
                is_st = (eff_j <= max(max_jitter, int(med * 0.50)) and worst <= max(350, int(med * 2.0)))
                return link, name, med, eff_j, is_st, succ

            with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
                futures = [executor.submit(detailed_probe, cand) for cand in stage1_candidates]
                for future in as_completed(futures):
                    link, name, med_d, diff, is_stable, succ = future.result()
                    if med_d > 0:
                        results[link] = {"delay": med_d, "jitter": diff, "is_stable": is_stable, "success_count": succ, "name": name}
                        label = f"[STABLE {succ}/5]" if is_stable else f"[UNSTABLE {succ}/5]"
                        print(f"{label} {med_d:4d} ms (±{diff:3d}) | {name}")
                    else:
                        print(f"[DROPPED {succ}/5] | {name}")
    finally:
        proc.terminate()
        try: proc.wait(1.5)
        except Exception: proc.kill()
        if os.path.exists(tmp_cfg): os.remove(tmp_cfg)

    best = [l for l, i in results.items() if i["delay"] < threshold and i["is_stable"] and not (filter_ru and is_ru_node(i["name"]))]
    best.sort(key=lambda l: results[l]["delay"])
    if not best and results:
        print("\n[!] Применяем fallback без RU.")
        fallback = [l for l, i in results.items() if not (filter_ru and is_ru_node(i["name"]))]
        fallback.sort(key=lambda l: results[l]["delay"])
        best = fallback[:10]

    if max_best > 0 and len(best) > max_best:
        print(f"Применен лимит узлов: оставляем Топ-{max_best} из {len(best)}")
        best = best[:max_best]

    elapsed = round(time.time() - start_time, 1)
    MIN_REQUIRED_NODES = 2

    if best and len(best) >= MIN_REQUIRED_NODES:
        print(f"\nВыбрано лучших узлов: {len(best)} шт. (Время: {elapsed}с)")

        section_reports = []
        for sec, mode in section_targets.items():
            existing_links = []
            try:
                out = subprocess.check_output(["uci", "-q", "get", f"podkop.{sec}.urltest_proxy_links"], text=True).strip()
                existing_links = [l.strip() for l in out.split() if l.strip()]
            except Exception:
                existing_links = []

            before_cnt = len(existing_links)

            if mode == "append":
                existing_fps = {link_fingerprint(x) for x in existing_links}
                merged = list(existing_links)
                added = 0
                for b in best:
                    if link_fingerprint(b) not in existing_fps:
                        merged.append(b)
                        existing_fps.add(link_fingerprint(b))
                        added += 1
                target_links = merged
                after_cnt = len(target_links)
                print(f"Секция '{sec}': режим добавления (+{added} новых, было {before_cnt} -> стало {after_cnt})")
                section_reports.append(f"  ▫️ <b>{sec}</b>: добавлено +{added} новых (было {before_cnt} → стало {after_cnt})")
            else:
                target_links = best
                after_cnt = len(target_links)
                print(f"Секция '{sec}': полная замена (было {before_cnt} -> стало {after_cnt})")
                section_reports.append(f"  ▫️ <b>{sec}</b>: полная замена (было {before_cnt} -> стало {after_cnt})")

            batch_cmds = [f"delete podkop.{sec}.urltest_proxy_links"]
            for idx, raw_l in enumerate(target_links):
                l_str = raw_l.strip()
                if "#" in l_str:
                    base_url, raw_name = l_str.split("#", 1)
                else:
                    base_url = l_str
                    # Если имени нет в ссылке, достаем из результатов
                    raw_name = results.get(raw_l, {}).get("name", "")
                
                # Декодируем и заново безопасно кодируем ТОЛЬКО фрагмент имени
                unquoted_name = urllib.parse.unquote(raw_name).strip()
                if not unquoted_name or unquoted_name.startswith("n_"):
                    pr = urllib.parse.urlsplit(base_url).scheme.upper()
                    host = urllib.parse.urlsplit(base_url).hostname or f"Node-{idx+1}"
                    unquoted_name = f"{host} | {pr}"
                
                safe_encoded_name = urllib.parse.quote(unquoted_name)
                # Передаем всю ссылку внутри двойных кавычек: uci batch не сочтет # комментарием
                batch_cmds.append(f"add_list podkop.{sec}.urltest_proxy_links=\"{base_url}#{safe_encoded_name}\"")

            p = subprocess.Popen(["uci", "-q", "batch"], stdin=subprocess.PIPE, text=True)
            p.communicate(input="\n".join(batch_cmds) + "\n")

        subprocess.run(["uci", "commit", "podkop"], check=False)
        update_live_progress("♻️ [4/4] Обновление Podkop...", "Запись серверов в UCI и перезапуск службы...")
        time.sleep(1)
        subprocess.run(["/etc/init.d/podkop", "restart"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
        # Адаптивное ожидание завершения работы Podkop (до 10 минут)
        wait_cycles = 0
        while wait_cycles < 300:
            ps = subprocess.run(["pgrep", "-f", "podkop"], stdout=subprocess.PIPE, text=True)
            if not ps.stdout.strip():
                break
            time.sleep(2)
            wait_cycles += 1
        print("Podkop успешно обновлен!")

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
            nm = info.get("name", "Proxy")
            m = medals[i] if i < len(medals) else "•"
            top_lines.append(f"{m} <code>{d} ms</code> (±{jit}) — {nm}")
        top_text = "\n".join(top_lines)
        sections_block = "\n".join(section_reports)

        msg = (
            f"🟢 <b>SubParser: Podkop обновлен</b>\n\n"
            f"📊 <b>Статистика:</b>\n"
            f"• Отобрано узлов: <b>{len(best)}</b> из {len(all_links)}\n"
            f"• Порог задержки: &lt; {threshold} ms\n"
            f"• Время анализа: <code>{elapsed}с</code>\n\n"
            f"📁 <b>Секции Podkop:</b>\n{sections_block}\n\n"
            f"⚡ <b>Топ-3 быстрых узла:</b>\n{top_text}\n\n"
            f"🏷 <b>Протоколы:</b> <code>{proto_str}</code>"
        )
        if GLOBAL_SUB_USERINFO:
            try:
                with open("/tmp/subparser_subinfo.json", "w", encoding="utf-8") as s_file:
                    json.dump(GLOBAL_SUB_USERINFO, s_file)
            except Exception:
                pass
            sub_lines = []
            for sname, sdata in GLOBAL_SUB_USERINFO.items():
                used = sdata.get("upload", 0) + sdata.get("download", 0)
                tot = sdata.get("total", 0)
                exp = sdata.get("expire", 0)
                exp_str = time.strftime('%d.%m.%Y', time.localtime(exp)) if exp else "бессрочно"
                if tot > 0:
                    sub_lines.append(f"• <b>{sname}</b>: {format_bytes(used)} / {format_bytes(tot)} (до {exp_str})")
            if sub_lines:
                msg += "\n\n💳 <b>Лимиты подписок:</b>\n" + "\n".join(sub_lines)
        send_telegram_notify(msg)
    else:
        count = len(best) if best else 0
        print(f"\n[!] Предупреждение: найдено всего {count} узлов (требуется минимум {MIN_REQUIRED_NODES}).")
        print("[!] Конфигурация Podkop сохранена без изменений.")
        msg = f"🔴 <b>SubParser Alert</b>!\n" \
              f"Найдено всего {count} узлов. Конфигурация Podkop не изменена."
        send_telegram_notify(msg)

if __name__ == "__main__":
    main()
