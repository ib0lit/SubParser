# -*- coding: utf-8 -*-
import ipaddress
import os
import socket
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor

from .config import (
    BLOCKED_FILE, get_ssl_ctx, get_uci_bool, get_uci_val,
    load_blocked_macs as _load_blocked, save_blocked_macs as _save_blocked,
)

_FIREWALL_READY = False


def get_blocked_macs():
    return _load_blocked()


def save_blocked_macs(mac_list):
    return _save_blocked(mac_list)


def _nft(*args):
    subprocess.run(["nft"] + list(args), stderr=subprocess.DEVNULL, check=False)


def init_bot_firewall(force=False):
    """Создаёт nft-таблицу. Элементы set'ов при этом не сбрасываются."""
    global _FIREWALL_READY
    if _FIREWALL_READY and not force:
        return
    _nft("add", "table", "inet", "bot_block")
    _nft("add", "chain", "inet", "bot_block", "prerouting",
         "{ type filter hook prerouting priority -300; policy accept; }")
    _nft("add", "set", "inet", "bot_block", "blocked_ips", "{ type ipv4_addr; }")
    _nft("add", "set", "inet", "bot_block", "blocked_macs", "{ type ether_addr; }")
    # Пересоздаём только правила, сами элементы блокировок сохраняются.
    _nft("flush", "chain", "inet", "bot_block", "prerouting")
    _nft("add", "rule", "inet", "bot_block", "prerouting",
         "ether", "saddr", "@blocked_macs", "reject")
    _nft("add", "rule", "inet", "bot_block", "prerouting",
         "ip", "saddr", "@blocked_ips", "meta", "l4proto", "tcp", "reject", "with", "tcp", "reset")
    _nft("add", "rule", "inet", "bot_block", "prerouting",
         "ip", "saddr", "@blocked_ips", "reject")
    _FIREWALL_READY = True

    # Восстанавливаем сохранённые блокировки после перезагрузки роутера.
    for mac in _load_blocked():
        _nft("add", "element", "inet", "bot_block", "blocked_macs", f"{{ {mac} }}")
        ip = get_ip_by_mac(mac)
        if ip:
            _nft("add", "element", "inet", "bot_block", "blocked_ips", f"{{ {ip} }}")


def get_ip_by_mac(mac):
    mac_upper = str(mac).upper()
    try:
        with open("/tmp/dhcp.leases", "r") as f:
            for line in f:
                parts = line.strip().split()
                if len(parts) >= 4 and parts[1].upper() == mac_upper:
                    return parts[2]
    except Exception:
        pass
    try:
        with open("/proc/net/arp", "r") as f:
            for line in f.readlines()[1:]:
                parts = line.strip().split()
                if len(parts) >= 4 and parts[3].upper() == mac_upper:
                    return parts[0]
    except Exception:
        pass
    return None


def apply_mac_firewall(mac, block=True):
    init_bot_firewall()
    mac_u = str(mac).upper()
    ip = get_ip_by_mac(mac_u)

    if block:
        # Блокируем по MAC (не слетает при смене IP по DHCP) и по текущему IP
        # для сброса уже установленных соединений.
        _nft("add", "element", "inet", "bot_block", "blocked_macs", f"{{ {mac_u} }}")
        if ip:
            _nft("add", "element", "inet", "bot_block", "blocked_ips", f"{{ {ip} }}")
            subprocess.run(["conntrack", "-D", "-s", ip], stderr=subprocess.DEVNULL, check=False)
            subprocess.run(["conntrack", "-D", "-d", ip], stderr=subprocess.DEVNULL, check=False)
            subprocess.run(["ip", "neigh", "flush", "to", ip], stderr=subprocess.DEVNULL, check=False)
    else:
        _nft("delete", "element", "inet", "bot_block", "blocked_macs", f"{{ {mac_u} }}")
        if ip:
            _nft("delete", "element", "inet", "bot_block", "blocked_ips", f"{{ {ip} }}")
    return True


def get_wifi_stations():
    wifi_data = {}
    try:
        res = subprocess.check_output(["ubus", "list", "hostapd.*"], text=True, stderr=subprocess.DEVNULL)
        h_objs = [line.strip() for line in res.splitlines() if line.strip()]
        for obj in h_objs:
            try:
                raw_json = subprocess.check_output(["ubus", "call", obj, "get_clients"],
                                                   text=True, stderr=subprocess.DEVNULL)
                clients_map = __import__("json").loads(raw_json).get("clients", {})
                for mac, meta in clients_map.items():
                    sig = meta.get("signal", -100)
                    freq = meta.get("freq", 0) or meta.get("frequency", 0)
                    if freq:
                        band = "5G" if int(freq) >= 4000 else "2.4G"
                    else:
                        band = "5G" if ("5" in obj or "phy1" in obj) else "2.4G"
                    tx_rate = meta.get("tx", {}).get("rate", 0)
                    tx_mbps = f"{tx_rate / 1000:.1f} M" if tx_rate > 1000 else (f"{tx_rate} M" if tx_rate > 0 else "")
                    if sig >= -55:
                        s_ico = "📶 Отличный"
                    elif sig >= -70:
                        s_ico = "📶 Хороший"
                    elif sig >= -82:
                        s_ico = "🛜 Средний"
                    else:
                        s_ico = "🛜 Слабый"
                    wifi_data[mac.upper()] = {
                        "type": f"Wi-Fi {band}",
                        "signal": f"{s_ico} ({sig} dBm)",
                        "tx": tx_mbps,
                    }
            except Exception:
                pass
    except Exception:
        pass
    return wifi_data


def _is_private_address(ip_str):
    try:
        ip = ipaddress.ip_address(ip_str)
        return (ip.is_private or ip.is_loopback or ip.is_link_local or
                ip.is_multicast or ip.is_reserved)
    except Exception:
        return False


def _validate_diag_target(raw_input):
    """Разрешает только http/https и по умолчанию не даёт сканировать LAN/loopback."""
    text = (raw_input or "").strip()
    if not text:
        raise ValueError("пустой адрес")
    if "://" not in text:
        text = "https://" + text
    parsed = urllib.parse.urlsplit(text)
    if parsed.scheme not in ("http", "https"):
        raise ValueError("поддерживаются только http/https")
    host = parsed.hostname
    if not host:
        raise ValueError("не удалось определить хост")
    if not get_uci_bool("allow_local_diag", False):
        try:
            infos = socket.getaddrinfo(host, parsed.port or (443 if parsed.scheme == "https" else 80),
                                       socket.AF_INET, socket.SOCK_STREAM)
            for info in infos:
                if _is_private_address(info[4][0]):
                    raise ValueError("локальные и служебные адреса запрещены")
        except ValueError:
            raise
        except Exception:
            pass
    return text, host


def analyze_custom_url(raw_input):
    try:
        test_url, domain = _validate_diag_target(raw_input)

        is_proxied = False
        resolved_ip = "н/д"
        try:
            addrinfo = socket.getaddrinfo(domain, urllib.parse.urlsplit(test_url).port or 443,
                                          socket.AF_INET, socket.SOCK_STREAM)
            if addrinfo:
                resolved_ip = addrinfo[0][4][0]
                if resolved_ip.startswith("198.18.") or resolved_ip.startswith("198.19."):
                    is_proxied = True
        except Exception:
            resolved_ip = "DNS ошибка"

        headers = {"User-Agent": "curl/7.88.1"}
        ms = -1
        status_text = "🔴 Не отвечает"
        t0 = time.time()
        try:
            req = urllib.request.Request(test_url, headers=headers)
            with urllib.request.urlopen(req, timeout=3.5, context=get_ssl_ctx()) as r:
                ms = int((time.time() - t0) * 1000)
                status_text = f"🟢 Доступен ({r.status}, <code>{ms} ms</code>)"
        except urllib.error.HTTPError as he:
            ms = int((time.time() - t0) * 1000)
            status_text = f"🟢 Доступен (HTTP {he.code}, <code>{ms} ms</code>)"
        except Exception:
            status_text = "🔴 Ошибка соединения / Блок"

        route_badge = "🛡 <b>Туннель Podkop (Fake-IP)</b>" if is_proxied else "🇷🇺 <b>Напрямую без VPN (Провайдер РФ)</b>"
        return (
            f"🔍 <b>Диагностика маршрута:</b> <code>{domain}</code>\n\n"
            f"• <b>Маршрут:</b> {route_badge}\n"
            f"• <b>Резолв IP:</b> <code>{resolved_ip}</code>\n"
            f"• <b>Отклик:</b> {status_text}\n"
            f"• <b>Проверен URL:</b> <code>{test_url[:50]}</code>"
        )
    except ValueError as e:
        return f"⚠️ Недопустимый адрес: {e}"
    except Exception as e:
        return f"⚠️ Ошибка при анализе адреса: {e}"


def check_single_service(item):
    name, domain, url = item
    is_proxied = False
    try:
        addrinfo = socket.getaddrinfo(domain, 443, socket.AF_INET, socket.SOCK_STREAM)
        if addrinfo:
            ip = addrinfo[0][4][0]
            if ip.startswith("198.18.") or ip.startswith("198.19."):
                is_proxied = True
    except Exception:
        pass

    headers = {"User-Agent": "curl/7.88.1"}
    ms = -1
    ok = False
    t0 = time.time()
    try:
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=3.0, context=get_ssl_ctx()) as r:
            ms = int((time.time() - t0) * 1000)
            ok = True
    except urllib.error.HTTPError:
        ms = int((time.time() - t0) * 1000)
        ok = True
    except Exception:
        ok = False

    return name, is_proxied, ok, ms


def measure_download_speed():
    test_url = "https://speed.cloudflare.com/__down?bytes=10485760"
    proxy = get_uci_val("speedtest_proxy", "socks5h://127.0.0.1:4534")
    cmd = [
        "curl", "-s", "-x", proxy,
        "-w", "%{http_code} %{speed_download} %{time_total}",
        "-o", "/dev/null", "-m", "10",
        test_url,
    ]
    try:
        res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
        parts = res.stdout.strip().split()
        if len(parts) >= 3 and parts[0] == "200":
            bytes_sec = float(parts[1])
            elapsed = float(parts[2])
            if bytes_sec > 0:
                mbps = (bytes_sec * 8.0) / (1024.0 * 1024.0)
                return True, f"{mbps:.1f} Мбит/с", f"{elapsed:.1f}с"
    except Exception:
        pass
    return False, "0 Мбит/с", "ошибка"
