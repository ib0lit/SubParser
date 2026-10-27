# -*- coding: utf-8 -*-
import json
import threading
import time
import urllib.error
import urllib.request

from .config import get_ssl_ctx

TG_MAX_LEN = 4000
_EDIT_LOCK = threading.Lock()
_LAST_EDIT = {}


def tg_api(token, method, params=None, timeout=15, retries=2):
    url = f"https://api.telegram.org/bot{token}/{method}"
    attempt = 0
    while True:
        attempt += 1
        if params:
            data = json.dumps(params).encode("utf-8")
            req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
        else:
            req = urllib.request.Request(url)
        try:
            with urllib.request.urlopen(req, timeout=timeout, context=get_ssl_ctx()) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            retry_after = None
            try:
                body = e.read().decode("utf-8", errors="ignore")
                payload = json.loads(body)
                retry_after = payload.get("parameters", {}).get("retry_after")
            except Exception:
                pass
            if attempt <= retries and (e.code == 429 or 500 <= e.code < 600):
                time.sleep(float(retry_after or (attempt * 2)))
                continue
            return None
        except Exception:
            if attempt <= retries:
                time.sleep(attempt)
                continue
            return None


def _split_text(text, limit=TG_MAX_LEN):
    text = text or ""
    if len(text) <= limit:
        return [text]
    chunks = []
    rest = text
    while rest:
        if len(rest) <= limit:
            chunks.append(rest)
            break
        cut = rest.rfind("\n", 0, limit)
        if cut < limit // 2:
            cut = limit
        chunks.append(rest[:cut])
        rest = rest[cut:].lstrip("\n")
    return chunks


def _throttle_edit(chat_id, min_interval=1.1):
    key = str(chat_id)
    with _EDIT_LOCK:
        now = time.time()
        last = _LAST_EDIT.get(key, 0)
        wait = min_interval - (now - last)
        if wait > 0:
            time.sleep(wait)
        _LAST_EDIT[key] = time.time()


def send_msg(token, chat_id, text, reply_markup=None):
    chunks = _split_text(text)
    result = None
    for i, chunk in enumerate(chunks):
        payload = {"chat_id": chat_id, "text": chunk, "parse_mode": "HTML"}
        if reply_markup and i == len(chunks) - 1:
            payload["reply_markup"] = reply_markup
        result = tg_api(token, "sendMessage", payload, timeout=10)
        if i + 1 < len(chunks):
            time.sleep(0.4)
    return result


def edit_msg(token, chat_id, message_id, text, reply_markup=None):
    text = text or ""
    if len(text) <= TG_MAX_LEN:
        _throttle_edit(chat_id)
        payload = {"chat_id": chat_id, "message_id": message_id, "text": text, "parse_mode": "HTML"}
        if reply_markup:
            payload["reply_markup"] = reply_markup
        return tg_api(token, "editMessageText", payload, timeout=10)

    # Слишком длинное сообщение: редактируем первую часть, остальное отправляем отдельно.
    chunks = _split_text(text)
    _throttle_edit(chat_id)
    payload = {"chat_id": chat_id, "message_id": message_id, "text": chunks[0], "parse_mode": "HTML"}
    result = tg_api(token, "editMessageText", payload, timeout=10)
    for chunk in chunks[1:]:
        tg_api(token, "sendMessage",
               {"chat_id": chat_id, "text": chunk, "parse_mode": "HTML"}, timeout=10)
    if reply_markup:
        tg_api(token, "editMessageReplyMarkup",
               {"chat_id": chat_id, "message_id": message_id, "reply_markup": reply_markup}, timeout=10)
    return result


def delete_msg(token, chat_id, msg_id):
    return tg_api(token, "deleteMessage", {"chat_id": chat_id, "message_id": msg_id}, timeout=5)


def answer_callback(token, cb_id, text=None):
    payload = {"callback_query_id": cb_id}
    if text:
        payload["text"] = text
    tg_api(token, "answerCallbackQuery", payload, timeout=5)
