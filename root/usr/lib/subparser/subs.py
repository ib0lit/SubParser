# -*- coding: utf-8 -*-
import subprocess
import urllib.request
import urllib.parse
import base64
from .config import CONFIG_NAME, SSL_CTX, UA_LIST

def get_uci_opt(sec, opt, default=""):
    res = subprocess.run(["uci", "-q", "get", f"{CONFIG_NAME}.{sec}.{opt}"], stdout=subprocess.PIPE, text=True)
    val = res.stdout.strip().strip("'\"")
    return val if val else default

def get_subscriptions():
    subs = []
    try:
        res = subprocess.run(["uci", "-q", "show", CONFIG_NAME], stdout=subprocess.PIPE, text=True)
        sec_names = []
        for line in res.stdout.splitlines():
            if "=subscription" in line:
                s_name = line.split(".")[1].split("=")[0]
                if s_name not in sec_names:
                    sec_names.append(s_name)

        for idx, s_name in enumerate(sec_names):
            url = get_uci_opt(s_name, "url", "")
            name = get_uci_opt(s_name, "name", f"Подписка #{idx+1}")
            en = get_uci_opt(s_name, "enabled", "1")
            ua = get_uci_opt(s_name, "user_agent", "v2rayN")
            custom_ua = get_uci_opt(s_name, "custom_ua", "")
            device_id = get_uci_opt(s_name, "device_id", "")
            device_model = get_uci_opt(s_name, "device_model", "")
            os_ver = get_uci_opt(s_name, "os_version", "")
            subs.append({
                "idx": idx,
                "sec_name": s_name,
                "enabled": en,
                "user_agent": ua,
                "custom_ua": custom_ua,
                "device_id": device_id,
                "device_model": device_model,
                "os_version": os_ver,
                "name": name,
                "url": url
            })
    except Exception:
        pass
    return subs

def toggle_sub_enabled(sub_idx):
    subs = get_subscriptions()
    target = next((s for s in subs if s["idx"] == sub_idx), None)
    if not target:
        return False, "0"
    new_val = "0" if target["enabled"] == "1" else "1"
    subprocess.run(["uci", "-q", "set", f"{CONFIG_NAME}.{target['sec_name']}.enabled={new_val}"], check=False)
    subprocess.run(["uci", "-q", "commit", CONFIG_NAME], check=False)
    return True, new_val

def cycle_sub_ua(sub_idx):
    subs = get_subscriptions()
    target = next((s for s in subs if s["idx"] == sub_idx), None)
    if not target:
        return False, "v2rayN"
    cur_ua = target["user_agent"]
    try:
        curr_pos = UA_LIST.index(cur_ua)
        next_ua = UA_LIST[(curr_pos + 1) % len(UA_LIST)]
    except ValueError:
        next_ua = "v2rayN"
    subprocess.run(["uci", "-q", "set", f"{CONFIG_NAME}.{target['sec_name']}.user_agent={next_ua}"], check=False)
    subprocess.run(["uci", "-q", "commit", CONFIG_NAME], check=False)
    return True, next_ua

def delete_sub_by_idx(sub_idx):
    subs = get_subscriptions()
    target = next((s for s in subs if s["idx"] == sub_idx), None)
    if not target:
        return False
    subprocess.run(["uci", "-q", "delete", f"{CONFIG_NAME}.{target['sec_name']}"], check=False)
    subprocess.run(["uci", "-q", "commit", CONFIG_NAME], check=False)
    return True

def add_subscription_to_uci(url: str, ua_key: str = "v2rayN", name: str = None):
    try:
        if not name:
            parsed = urllib.parse.urlsplit(url)
            name = parsed.netloc or "Подписка"
        add_cmd = subprocess.run(["uci", "add", CONFIG_NAME, "subscription"], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        sec_name = add_cmd.stdout.strip()
        sec_ref = f"{CONFIG_NAME}.{sec_name}" if sec_name else f"{CONFIG_NAME}.@subscription[-1]"
        subprocess.run(["uci", "-q", "set", f"{sec_ref}.enabled=1"], check=False)
        subprocess.run(["uci", "-q", "set", f"{sec_ref}.name={name}"], check=False)
        subprocess.run(["uci", "-q", "set", f"{sec_ref}.url={url}"], check=False)
        subprocess.run(["uci", "-q", "set", f"{sec_ref}.user_agent={ua_key}"], check=False)
        subprocess.run(["uci", "-q", "commit", CONFIG_NAME], check=False)
        return True, name
    except Exception as e:
        return False, str(e)

def detect_is_subscription(url: str):
    try:
        req = urllib.request.Request(
            url,
            headers={"User-Agent": "v2rayN/6.42", "Accept": "*/*"}
        )
        with urllib.request.urlopen(req, timeout=3.0, context=SSL_CTX) as resp:
            headers = resp.headers
            if "subscription-userinfo" in headers or "Subscription-Userinfo" in headers:
                return True, "Провайдер подписок"
            body = resp.read(4096)
            body_low = body.lower()
            if b"<!doctype html" in body_low or b"<html" in body_low or b"<head" in body_low:
                return False, ""
            try:
                decoded = base64.b64decode(body + b"==").decode("utf-8", errors="ignore")
                if any(proto in decoded for proto in ["vless://", "vmess://", "trojan://", "ss://", "hysteria2://"]):
                    return True, "Base64 Подписка"
            except Exception:
                pass
            body_str = body.decode("utf-8", errors="ignore")
            if any(proto in body_str for proto in ["vless://", "trojan://", "ss://", "proxies:"]):
                return True, "Конфиг прокси"
    except Exception:
        pass
    return False, ""
