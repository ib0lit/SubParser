#!/usr/bin/env python3
# -*- coding: utf-8 -*-
import sys
import time
sys.path.insert(0, "/usr/lib")
import html
import logging
import subprocess
import threading
import urllib.parse
import os
import json

from subparser.config import (
    CONFIG_NAME, UA_CHOICES, check_credentials,
    get_uci_val, set_uci_val, get_user_state, set_user_state,
    clear_user_state, get_bot_offset, set_bot_offset,
)
from subparser.telegram import tg_api, send_msg, edit_msg, delete_msg, answer_callback
from subparser.subs import (
    detect_is_subscription, add_subscription_to_uci,
    toggle_sub_enabled, cycle_sub_ua, delete_sub_by_idx
)
from subparser.system import (
    init_bot_firewall, get_blocked_macs, save_blocked_macs,
    apply_mac_firewall, analyze_custom_url
)
from subparser.podkop import (
    get_all_podkop_sections, get_podkop_links, get_node_name, get_section_routing_state,
    switch_active_node, reset_to_auto_urltest, delete_node, delete_nodes_batch,
    batch_ping_nodes, run_parser_process
)
from subparser.screens import (
    get_clients_screen, get_device_action_screen, get_dmesg_screen,
    get_log_screen, get_logread_screen, get_main_screen, get_update_screen,
    get_parser_menu_screen, get_fp_menu_screen, get_reboot_confirm_screen,
    get_sections_menu_screen, get_sections_selector_screen,
    get_del_dead_all_confirm_screen,
    get_servers_screen, get_services_status_screen, get_status_screen, get_system_metrics,
    get_sub_card_screen, get_sub_delete_confirm_screen, get_subs_list_screen
)

def get_allowed_user_ids():
    ids = set()
    try:
        import subprocess
        admin = subprocess.check_output(['uci', '-q', 'get', 'subparser.settings.tg_chat_id'], text=True).strip()
        if admin.isdigit():
            ids.add(int(admin))
        res = subprocess.check_output(['uci', '-q', 'get', 'subparser.settings.allowed_users'], text=True).strip()
        for item in res.split():
            if item.isdigit():
                ids.add(int(item))
    except Exception:
        pass
    return ids

def watch_parser_and_restore_menu(token, chat_id, old_msg_id):
    time.sleep(3)
    while True:
        ps = subprocess.run(["pgrep", "-f", "[s]ubparser.py"], stdout=subprocess.PIPE, text=True)
        if not ps.stdout.strip():
            break
        time.sleep(2)
    try:
        os.remove("/tmp/subparser_progress.json")
    except Exception:
        pass
    time.sleep(3)
    try:
        delete_msg(token, chat_id, old_msg_id)
    except Exception:
        pass
    try:
        t, kb = get_main_screen()
        send_msg(token, chat_id, t, reply_markup=kb)
    except Exception:
        pass

def setup_bot_commands(token):
    commands = [
        {"command": "menu", "description": "🎛 Главное меню управления"}
    ]
    tg_api(token, "setMyCommands", {"commands": commands})

def check_post_update_notification(token):
    marker = "/tmp/subparser_update_pending"
    if os.path.exists(marker):
        try:
            with open(marker, "r") as f:
                data = json.load(f)
            ver = "актуальной"
            try:
                with open("/etc/subparser_version", "r") as vf:
                    ver = vf.read().strip()
            except Exception:
                pass
            msg = f"🚀 <b>Сервис успешно обновлен до версии v{ver}!</b>\nВсе компоненты работают в штатном режиме."
            kb = {"inline_keyboard": [[{"text": "◀ В главное меню", "callback_data": "home"}]]}
            edit_msg(token, data["chat_id"], data["msg_id"], msg, kb)
        except Exception as e:
            print(f"[!] Ошибка отправки пост-апдейт уведомления: {e}")
        finally:
            try:
                os.remove(marker)
            except Exception:
                pass

def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        handlers=[
            logging.FileHandler("/tmp/subparser_bot.log", encoding="utf-8"),
            logging.StreamHandler(sys.stdout),
        ],
    )
    log = logging.getLogger("subparser-bot")
    offset = get_bot_offset()
    first_poll = offset == 0
    print("[*] SubParser Bot запущен (модульная архитектура)...")
    init_bot_firewall()
    token_init, _ = check_credentials()
    if token_init:
        setup_bot_commands(token_init)
        check_post_update_notification(token_init)

    while True:
        token, admin_id = check_credentials()
        if not token or not admin_id:
            time.sleep(10)
            continue

        try:
            payload = {"offset": offset, "timeout": 20}
            if first_poll:
                # Не выполняем старые кнопки, накопившиеся до первого запуска.
                payload["drop_pending_updates"] = True
            updates = tg_api(token, "getUpdates", payload, timeout=25)
            first_poll = False
            if not updates or not updates.get("ok"):
                time.sleep(3)
                continue

            for u in updates.get("result", []):
                offset = u["update_id"] + 1
                set_bot_offset(offset)

                if "message" in u:
                    msg = u["message"]
                    user_id = str(msg.get("from", {}).get("id", "")).strip()
                    username = msg.get("from", {}).get("username", "unknown")
                    allowed = {str(i) for i in get_allowed_user_ids()}
                    if user_id not in allowed:
                        print(f"[AUTH REJECT] Msg from unauthorized user {username} (ID: {user_id})")
                        continue
                    print(f"[MSG] From {username} (ID: {user_id}): {msg.get('text', '')}")

                    raw_text = msg.get("text", "").strip()
                    if not raw_text:
                        continue

                    state = get_user_state(user_id)

                    if state.get("waiting_for") == "custom_threshold":
                        clear_user_state(user_id)
                        if raw_text.isdigit() and 30 <= int(raw_text) <= 3000:
                            set_uci_val("ping_threshold", raw_text)
                            send_msg(token, user_id, f"✅ <b>Порог задержки установлен:</b> <code>{raw_text} ms</code>")
                            t, kb = get_parser_menu_screen()
                            send_msg(token, user_id, t, kb)
                            continue
                        else:
                            send_msg(token, user_id, "⚠️ <b>Некорректное значение.</b> Введите число от 30 до 3000.")
                            t, kb = get_parser_menu_screen(prompt_custom_ping=True)
                            send_msg(token, user_id, t, kb)
                            continue

                    if raw_text in ("/start", "/menu", "/help"):
                        t, kb = get_main_screen()
                        send_msg(token, user_id, t, kb)
                    elif raw_text.startswith("http://") or raw_text.startswith("https://"):
                        is_sub, sub_type = detect_is_subscription(raw_text)
                        if is_sub:
                            parsed = urllib.parse.urlsplit(raw_text)
                            set_user_state(user_id, {"pending_sub_url": raw_text})
                            card = (
                                f"📥 <b>Обнаружена подписка ({sub_type})!</b>\n\n"
                                f"🌐 <b>Хост:</b> <code>{parsed.netloc}</code>\n"
                                f"🔗 <b>URL:</b> <code>{html.escape(raw_text[:50])}...</code>\n\n"
                                "<i>Выберите клиентский профиль (User-Agent):</i>"
                            )
                            kb = {"inline_keyboard": [
                                [
                                    {"text": "📱 v2rayN", "callback_data": "addsub_ua:v2rayN"},
                                    {"text": "⚡ FlClash", "callback_data": "addsub_ua:flclash"}
                                ],
                                [
                                    {"text": "📦 Sing-Box", "callback_data": "addsub_ua:singbox"},
                                    {"text": "🍏 Happ (iOS)", "callback_data": "addsub_ua:happ"}
                                ],
                                [
                                    {"text": "🤖 Incy (Android)", "callback_data": "addsub_ua:incy"},
                                    {"text": "🔍 Тест как сайт", "callback_data": "diag_host"}
                                ],
                                [{"text": "❌ Отмена", "callback_data": "close_msg"}]
                            ]}
                            send_msg(token, user_id, card, kb)
                        else:
                            wait_m = send_msg(token, user_id, "⏳ <i>Анализирую маршрут и доступность узла...</i>")
                            wait_id = wait_m.get("result", {}).get("message_id") if wait_m else None
                            rep = analyze_custom_url(raw_text)
                            kb_back = {"inline_keyboard": [
                                [{"text": "🗑 Закрыть", "callback_data": "close_msg"}, {"text": "◀️ В главное меню", "callback_data": "home"}]
                            ]}
                            if wait_id:
                                edit_msg(token, user_id, wait_id, rep, kb_back)
                            else:
                                send_msg(token, user_id, rep, kb_back)
                    else:
                        wait_m = send_msg(token, user_id, "⏳ <i>Анализирую маршрут и доступность узла...</i>")
                        wait_id = wait_m.get("result", {}).get("message_id") if wait_m else None
                        rep = analyze_custom_url(raw_text)
                        kb_back = {"inline_keyboard": [
                            [{"text": "🗑 Закрыть", "callback_data": "close_msg"}, {"text": "◀️ В главное меню", "callback_data": "home"}]
                        ]}
                        if wait_id:
                            edit_msg(token, user_id, wait_id, rep, kb_back)
                        else:
                            send_msg(token, user_id, rep, kb_back)

                elif "callback_query" in u:
                    cb = u["callback_query"]
                    cb_id = cb["id"]
                    user_id = str(cb.get("from", {}).get("id", "")).strip()
                    username = cb.get("from", {}).get("username", "unknown")
                    allowed = {str(i) for i in get_allowed_user_ids()}
                    if user_id not in allowed:
                        print(f"[AUTH REJECT] Click from unauthorized user {username} (ID: {user_id})")
                        answer_callback(token, cb_id, "⛔ Доступ ограничен")
                        continue
                    print(f"[CLICK] From {username} (ID: {user_id}) -> {cb.get('data', '')}")

                    data = cb.get("data", "")
                    msg_obj = cb.get("message", {})
                    msg_id = msg_obj.get("message_id")
                    chat_id = msg_obj.get("chat", {}).get("id")

                    ADMIN_PREFIXES = (
                        'sub_del_', 'reboot', 'do_reboot', 'restart', 'enter_del_mode',
                        'srv_del_pg', 'apply_bdel', 'del:', 'blk:', 'unblk:', 'do_update',
                        'del_dead_all',
                    )
                    if any(data == p or data.startswith(p) for p in ADMIN_PREFIXES) and user_id != admin_id:
                        print(f"[AUTH REJECT] Admin-only action '{data}' blocked for user {username} ({user_id})")
                        answer_callback(token, cb_id, "⛔ Действие доступно только администратору!")
                        continue

                    if data == "close_msg":
                        clear_user_state(user_id)
                        answer_callback(token, cb_id, "Окно закрыто")
                        delete_msg(token, chat_id, msg_id)
                        continue

                    elif data == "home":
                        clear_user_state(user_id)
                        answer_callback(token, cb_id)
                        t, kb = get_main_screen()
                        edit_msg(token, chat_id, msg_id, t, kb)

                    elif data == "menu_fp":
                        clear_user_state(user_id)
                        answer_callback(token, cb_id)
                        t, kb = get_fp_menu_screen()
                        edit_msg(token, chat_id, msg_id, t, kb)

                    elif data.startswith("set_fp:"):
                        target_fp = data.split("set_fp:")[1].strip()
                        set_uci_val("override_fp", target_fp)
                        answer_callback(token, cb_id, f"Установлен: {target_fp}")
                        t, kb = get_fp_menu_screen()
                        edit_msg(token, chat_id, msg_id, t, kb)

                    elif data == "check_update":
                        answer_callback(token, cb_id, "Поиск обновлений...")
                        t, kb = get_update_screen()
                        edit_msg(token, chat_id, msg_id, t, kb)
                        continue

                    elif data == "do_update":
                        if user_id != admin_id:
                            answer_callback(token, cb_id, "⛔ Доступно только администратору!")
                            continue
                        answer_callback(token, cb_id, "Запуск обновления...")
                        edit_msg(token, chat_id, msg_id, "🔄 <b>Выполняется загрузка и установка обновления...</b>\nБот автоматически перезапустится через 1-2 минуты.", {"inline_keyboard": []})
                        try:
                            with open("/tmp/subparser_update_pending", "w") as f:
                                json.dump({"chat_id": chat_id, "msg_id": msg_id}, f)
                        except Exception:
                            pass
                        branch = get_uci_val("update_branch", "main") or "main"
                        branch = "".join(ch for ch in branch if ch.isalnum() or ch in "-_./")
                        subprocess.Popen(
                            ["sh", "-c",
                             f"curl -fsSL https://raw.githubusercontent.com/ib0lit/SubParser/{branch}/update.sh -o /tmp/run_update.sh && chmod +x /tmp/run_update.sh && (SUBPARSER_BRANCH={branch} /tmp/run_update.sh >/tmp/subparser_update.log 2>&1 &)"],
                            close_fds=True,
                            start_new_session=True
                        )
                        continue

                    elif data == "parser_menu":
                        clear_user_state(user_id)
                        answer_callback(token, cb_id)
                        t, kb = get_parser_menu_screen()
                        edit_msg(token, chat_id, msg_id, t, kb)

                    elif data == "subs_list":
                        answer_callback(token, cb_id)
                        t, kb = get_subs_list_screen()
                        edit_msg(token, chat_id, msg_id, t, kb)

                    elif data.startswith("sub_card:"):
                        answer_callback(token, cb_id)
                        s_idx = data.split(":", 1)[1]
                        t, kb = get_sub_card_screen(s_idx)
                        edit_msg(token, chat_id, msg_id, t, kb)

                    elif data.startswith("sub_tgl:"):
                        s_idx = data.split(":", 1)[1]
                        ok, new_st = toggle_sub_enabled(s_idx)
                        txt = "Подписка включена" if new_st == "1" else "Подписка выключена"
                        answer_callback(token, cb_id, txt)
                        t, kb = get_sub_card_screen(s_idx)
                        edit_msg(token, chat_id, msg_id, t, kb)

                    elif data.startswith("sub_cycle_ua:"):
                        s_idx = data.split(":", 1)[1]
                        ok, new_ua = cycle_sub_ua(s_idx)
                        answer_callback(token, cb_id, f"UA изменен: {new_ua}")
                        t, kb = get_sub_card_screen(s_idx)
                        edit_msg(token, chat_id, msg_id, t, kb)

                    elif data.startswith("sub_del_ask:"):
                        answer_callback(token, cb_id)
                        s_idx = data.split(":", 1)[1]
                        t, kb = get_sub_delete_confirm_screen(s_idx)
                        edit_msg(token, chat_id, msg_id, t, kb)

                    elif data.startswith("sub_del_do:"):
                        s_idx = data.split(":", 1)[1]
                        ok = delete_sub_by_idx(s_idx)
                        answer_callback(token, cb_id, "Подписка удалена" if ok else "Ошибка удаления")
                        t, kb = get_subs_list_screen()
                        prefix = "🗑 <b>Подписка удалена из роутера!</b>" if ok else "⚠️ <b>Подписка не найдена.</b>"
                        edit_msg(token, chat_id, msg_id, f"{prefix}\n\n{t}", kb)

                    elif data.startswith("addsub_ua:"):
                        answer_callback(token, cb_id, "Сохраняю...")
                        ua_key = data.split("addsub_ua:")[1]
                        state = get_user_state(user_id)
                        sub_url = state.get("pending_sub_url", "")
                        clear_user_state(user_id)

                        if not sub_url:
                            edit_msg(token, chat_id, msg_id, "⚠️ <b>Время действия ссылки истекло.</b>\nОтправьте ссылку на подписку снова.")
                            continue

                        ok, res_name = add_subscription_to_uci(sub_url, ua_key=ua_key)
                        if ok:
                            ua_title = UA_CHOICES.get(ua_key, ua_key)
                            text_done = (
                                "✅ <b>Подписка успешно сохранена!</b>\n\n"
                                f"🏷 <b>Имя:</b> <code>{res_name}</code>\n"
                                f"👤 <b>Клиент:</b> <code>{ua_title}</code>\n"
                                f"⚙️ Ссылка добавлена в конфигурацию роутера.\n\n"
                                "Запустить синхронизацию узлов прямо сейчас?"
                            )
                            kb_done = {"inline_keyboard": [
                                [{"text": "▶️ Запустить синхронизацию", "callback_data": "run_parser"}],
                                [{"text": "📋 Мои подписки", "callback_data": "subs_list"}],
                                [{"text": "⚙️ Меню парсера", "callback_data": "parser_menu"}]
                            ]}
                            edit_msg(token, chat_id, msg_id, text_done, kb_done)
                        else:
                            edit_msg(token, chat_id, msg_id, f"❌ <b>Ошибка сохранения:</b> {res_name}")

                    elif data == "diag_host":
                        answer_callback(token, cb_id, "Тестирую...")
                        state = get_user_state(user_id)
                        test_url = state.get("pending_sub_url", "")
                        clear_user_state(user_id)
                        rep = analyze_custom_url(test_url)
                        kb_back = {"inline_keyboard": [
                            [{"text": "🗑 Закрыть", "callback_data": "close_msg"}, {"text": "◀️ В главное меню", "callback_data": "home"}]
                        ]}
                        edit_msg(token, chat_id, msg_id, rep, kb_back)

                    elif data == "bot_sections_menu":
                        answer_callback(token, cb_id)
                        t, kb = get_sections_menu_screen()
                        edit_msg(token, chat_id, msg_id, t, kb)

                    elif data == "toggle_glob_mode":
                        cur = get_uci_val("update_mode", "replace")
                        new_val = "append" if cur == "replace" else "replace"
                        set_uci_val("update_mode", new_val)
                        badge = "Замена" if new_val == "replace" else "Добавление"
                        answer_callback(token, cb_id, f"Режим: {badge}")
                        t, kb = get_sections_menu_screen()
                        edit_msg(token, chat_id, msg_id, t, kb)

                    elif data == "toggle_per_sec":
                        cur = get_uci_val("per_section_config", "0")
                        new_val = "0" if cur == "1" else "1"
                        set_uci_val("per_section_config", new_val)
                        answer_callback(token, cb_id, "Режим секций изменен")
                        t, kb = get_sections_menu_screen()
                        edit_msg(token, chat_id, msg_id, t, kb)

                    elif data.startswith("tgl_sec:"):
                        sec_name = data.split("tgl_sec:")[1]
                        raw_cur = get_uci_val("target_section", "main").split()
                        s_set = set(raw_cur)
                        if sec_name in s_set:
                            if len(s_set) > 1:
                                s_set.remove(sec_name)
                        else:
                            s_set.add(sec_name)
                        subprocess.run(["uci", "-q", "delete", f"{CONFIG_NAME}.settings.target_section"], check=False)
                        for sn in s_set:
                            subprocess.run(["uci", "add_list", f"{CONFIG_NAME}.settings.target_section={sn}"], check=False)
                        subprocess.run(["uci", "commit", CONFIG_NAME], check=False)
                        answer_callback(token, cb_id)
                        t, kb = get_sections_menu_screen()
                        edit_msg(token, chat_id, msg_id, t, kb)

                    elif data.startswith("cycle_sec:"):
                        sec_name = data.split("cycle_sec:")[1]
                        s_en = get_uci_val(f"sec_en_{sec_name}", "1") == "1"
                        s_mode = get_uci_val(f"sec_mode_{sec_name}", "replace")
                        if s_en and s_mode == "replace":
                            set_uci_val(f"sec_mode_{sec_name}", "append")
                        elif s_en and s_mode == "append":
                            set_uci_val(f"sec_en_{sec_name}", "0")
                        else:
                            set_uci_val(f"sec_en_{sec_name}", "1")
                            set_uci_val(f"sec_mode_{sec_name}", "replace")
                        answer_callback(token, cb_id)
                        t, kb = get_sections_menu_screen()
                        edit_msg(token, chat_id, msg_id, t, kb)

                    elif data == "ask_custom_th":
                        set_user_state(user_id, {"waiting_for": "custom_threshold"})
                        answer_callback(token, cb_id, "Ожидаю число...")
                        t, kb = get_parser_menu_screen(prompt_custom_ping=True)
                        edit_msg(token, chat_id, msg_id, t, kb)

                    elif data.startswith("th:"):
                        val = data.split(":")[1]
                        set_uci_val("ping_threshold", val)
                        answer_callback(token, cb_id, f"Порог установлен: {val} ms")
                        t, kb = get_parser_menu_screen()
                        edit_msg(token, chat_id, msg_id, t, kb)

                    elif data == "cycle_limit":
                        cur_lim = get_uci_val("max_best_nodes", "0")
                        cycle_map = {"0": "3", "3": "5", "5": "10", "10": "0"}
                        new_lim = cycle_map.get(cur_lim, "0")
                        set_uci_val("max_best_nodes", new_lim)
                        badge_txt = "Все" if new_lim == "0" else f"Топ-{new_lim}"
                        answer_callback(token, cb_id, f"Лимит узлов: {badge_txt}")
                        t, kb = get_parser_menu_screen()
                        edit_msg(token, chat_id, msg_id, t, kb)

                    elif data == "cycle_cron":
                        cur_cron = get_uci_val("interval", "never")
                        cron_seq = ["never", "1h", "12h", "24h"]
                        try:
                            idx = cron_seq.index(cur_cron)
                            next_cron = cron_seq[(idx + 1) % len(cron_seq)]
                        except ValueError:
                            next_cron = "never"

                        set_uci_val("interval", next_cron)
                        subprocess.run(["/etc/init.d/subparser", "restart"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
                        labels_map = {
                            "never": "Автопроверка отключена",
                            "1h": "Каждый 1 час",
                            "12h": "Каждые 12 часов",
                            "24h": "Раз в 24 часа"
                        }
                        badge_ans = labels_map.get(next_cron, next_cron)
                        answer_callback(token, cb_id, f"Расписание: {badge_ans}")
                        t, kb = get_parser_menu_screen()
                        edit_msg(token, chat_id, msg_id, t, kb)

                    elif data == "toggle_ru":
                        cur = get_uci_val("filter_ru", "1")
                        new_val = "0" if cur == "1" else "1"
                        set_uci_val("filter_ru", new_val)
                        answer_callback(token, cb_id, "Фильтр РФ переключен")
                        t, kb = get_parser_menu_screen()
                        edit_msg(token, chat_id, msg_id, t, kb)

                    elif data == "run_parser":
                        answer_callback(token, cb_id, "Запуск парсера...")
                        # Файл прогресса создаёт run_parser_process: только
                        # после проверки, что парсер ещё не выполняется.
                        ok, text_sync = run_parser_process(progress={
                            "token": token,
                            "chat_id": chat_id,
                            "msg_id": msg_id,
                            "started": int(time.time()),
                        })
                        edit_msg(token, chat_id, msg_id, f"▶️ <b>Синхронизация по текущей конфигурации</b>\n\n{text_sync}", reply_markup={"inline_keyboard": []})
                        th = threading.Thread(target=watch_parser_and_restore_menu, args=(token, chat_id, msg_id), daemon=True)
                        th.start()

                    elif data == "sysinfo":
                        answer_callback(token, cb_id)
                        t, kb = get_system_metrics()
                        edit_msg(token, chat_id, msg_id, t, kb)

                    elif data == "clients":
                        answer_callback(token, cb_id)
                        t, kb = get_clients_screen(page=0)
                        edit_msg(token, chat_id, msg_id, t, kb)

                    elif data.startswith("clients_pg:"):
                        answer_callback(token, cb_id)
                        try:
                            pg = int(data.split(":", 1)[1])
                        except Exception:
                            pg = 0
                        t, kb = get_clients_screen(page=pg)
                        edit_msg(token, chat_id, msg_id, t, kb)

                    elif data.startswith("dev:"):
                        mac = data.split("dev:")[1]
                        answer_callback(token, cb_id)
                        t, kb = get_device_action_screen(mac)
                        edit_msg(token, chat_id, msg_id, t, kb)

                    elif data.startswith("blk:"):
                        mac = data.split("blk:")[1].upper()
                        bl = [m.upper() for m in get_blocked_macs()]
                        if mac not in bl:
                            bl.append(mac)
                            save_blocked_macs(bl)
                        answer_callback(token, cb_id, "Интернет заблокирован")
                        t, kb = get_device_action_screen(mac, force_blocked=True)
                        edit_msg(token, chat_id, msg_id, t, kb)
                        apply_mac_firewall(mac, block=True)

                    elif data.startswith("unblk:"):
                        mac = data.split("unblk:")[1].upper()
                        bl = [m.upper() for m in get_blocked_macs()]
                        if mac in bl:
                            bl.remove(mac)
                            save_blocked_macs(bl)
                        answer_callback(token, cb_id, "Доступ разблокирован")
                        t, kb = get_device_action_screen(mac, force_blocked=False)
                        edit_msg(token, chat_id, msg_id, t, kb)
                        apply_mac_firewall(mac, block=False)

                    elif data == "view_log":
                        answer_callback(token, cb_id)
                        t, kb = get_log_screen()
                        edit_msg(token, chat_id, msg_id, t, kb)

                    elif data == "log_sys":
                        answer_callback(token, cb_id)
                        t, kb = get_logread_screen()
                        edit_msg(token, chat_id, msg_id, t, kb)

                    elif data == "log_dmesg":
                        answer_callback(token, cb_id)
                        t, kb = get_dmesg_screen()
                        edit_msg(token, chat_id, msg_id, t, kb)

                    elif data == "check_services":
                        answer_callback(token, cb_id, "Тестирую доступ...")
                        edit_msg(token, chat_id, msg_id, "⏳ <b>Проверяю маршруты и доступность (параллельно)...</b>")
                        t, kb = get_services_status_screen()
                        edit_msg(token, chat_id, msg_id, t, kb)

                    elif data == "run_speedtest":
                        answer_callback(token, cb_id, "Замеряю скорость...")
                        edit_msg(token, chat_id, msg_id, "⏳ <b>Тестирую скорость скачивания через туннель...</b>\n<i>(загрузка пакета 10 МБ через активный узел)</i>")
                        from subparser.system import measure_download_speed
                        ok, speed, elapsed = measure_download_speed()
                        if ok:
                            res_text = (
                                "🚀 <b>Результат теста скорости</b>\n\n"
                                f"• Скорость загрузки: <b>{speed}</b>\n"
                                f"• Время загрузки: <code>{elapsed}</code>\n"
                                f"• Маршрут: через активный туннель Podkop\n"
                            )
                        else:
                            res_text = "❌ <b>Ошибка замера скорости.</b>\nТаймаут соединения с тестовым CDN сервером."
                        kb_back = {"inline_keyboard": [
                            [{"text": "🔄 Повторить замер", "callback_data": "run_speedtest"}],
                            [{"text": "◀️ В меню статуса", "callback_data": "status"}]
                        ]}
                        edit_msg(token, chat_id, msg_id, res_text, kb_back)

                    elif data == "status":
                        answer_callback(token, cb_id)
                        t, kb = get_status_screen()
                        edit_msg(token, chat_id, msg_id, t, kb)

                    elif data == "reboot":
                        answer_callback(token, cb_id)
                        t, kb = get_reboot_confirm_screen()
                        edit_msg(token, chat_id, msg_id, t, kb)

                    elif data == "do_reboot":
                        answer_callback(token, cb_id, "Роутер перезагружается...")
                        edit_msg(token, chat_id, msg_id, "⚠️ <b>Роутер уходит в перезагрузку...</b>\nСвязь восстановится через 1-2 минуты.")
                        subprocess.Popen(["/sbin/reboot"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

                    elif data == "restart":
                        answer_callback(token, cb_id, "Служба перезапускается...")
                        text_pending = "♻️ <b>Служба Podkop перезапускается...</b>\nСоединение восстановится через 3-5 секунд."
                        kb_pending = {"inline_keyboard": [
                            [{"text": "🔄 Проверить статус", "callback_data": "status"}],
                            [{"text": "◀️ В главное меню", "callback_data": "home"}]
                        ]}
                        edit_msg(token, chat_id, msg_id, text_pending, kb_pending)
                        subprocess.Popen(["/etc/init.d/podkop", "restart"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

                    elif data == "servers":
                        answer_callback(token, cb_id)
                        all_secs = get_all_podkop_sections()
                        if len(all_secs) <= 1:
                            t, kb = get_servers_screen(section=all_secs[0] if all_secs else "main")
                        else:
                            t, kb = get_sections_selector_screen()
                        edit_msg(token, chat_id, msg_id, t, kb)

                    elif data == "del_dead_all":
                        answer_callback(token, cb_id)
                        t, kb = get_del_dead_all_confirm_screen()
                        edit_msg(token, chat_id, msg_id, t, kb)

                    elif data == "del_dead_all_do":
                        answer_callback(token, cb_id, "Проверяю узлы...")
                        edit_msg(token, chat_id, msg_id,
                                 "🧹 <b>Проверяю узлы во всех секциях...</b>\n\n"
                                 "<i>Это может занять до минуты.</i>",
                                 {"inline_keyboard": []})
                        from subparser.podkop import delete_dead_from_all_sections
                        res = delete_dead_from_all_sections()
                        if res.get("error"):
                            text = (f"⚠️ <b>Не удалось проверить узлы</b>\n\n"
                                    f"{html.escape(res['error'])}\n\n"
                                    "Podkop не изменялся.")
                        else:
                            lines = []
                            for item in res.get("details", []):
                                sec = html.escape(str(item.get("section", "?")))
                                status = item.get("status")
                                total = int(item.get("total", 0))
                                deleted = int(item.get("deleted", 0))
                                skipped = int(item.get("skipped", 0))
                                if status == "empty":
                                    lines.append(f"• <b>{sec}</b>: нет узлов")
                                elif status == "no_data":
                                    lines.append(f"• <b>{sec}</b>: пропущено (нет данных Podkop)")
                                elif status == "error":
                                    lines.append(f"• <b>{sec}</b>: ошибка записи")
                                elif status == "deleted":
                                    lines.append(f"• <b>{sec}</b>: удалено <b>{deleted}</b> из {total}")
                                else:
                                    lines.append(f"• <b>{sec}</b>: мёртвых нет ({total} шт.)")
                                if skipped and status != "no_data":
                                    lines.append(f"   <i>не проверено: {skipped}</i>")

                            total_deleted = int(res.get("total", 0))
                            if total_deleted:
                                note = ("Podkop перезапускается один раз в фоне."
                                        if res.get("restarted")
                                        else "Не удалось запустить перезапуск Podkop.")
                                text = (f"🧹 <b>Удалено мёртвых узлов: {total_deleted}</b>\n\n"
                                        + "\n".join(lines) +
                                        f"\n\n<i>{note}</i>")
                            else:
                                text = ("✅ <b>Мёртвых узлов не найдено</b>\n\n"
                                        + "\n".join(lines) +
                                        "\n\n<i>Podkop не перезапускался.</i>")
                        kb = {"inline_keyboard": [
                            [{"text": "◀️ К секциям", "callback_data": "servers"}],
                            [{"text": "🏠 В главное меню", "callback_data": "home"}],
                        ]}
                        edit_msg(token, chat_id, msg_id, text, kb)

                    elif data.startswith("sec_view:"):
                        answer_callback(token, cb_id, "Замеряю пинг...")
                        sec_name = data.split("sec_view:")[1]
                        from subparser.podkop import CACHED_DELAYS, get_podkop_links, batch_ping_nodes
                        delays = CACHED_DELAYS.get(sec_name)
                        links = get_podkop_links(sec_name)
                        if delays is None or not delays:
                            delays = batch_ping_nodes(links, section=sec_name) if links else {}
                        t, kb = get_servers_screen(delays_map=delays, section=sec_name, page=0)
                        edit_msg(token, chat_id, msg_id, t, kb)

                    elif data.startswith("enter_del_mode:"):
                        answer_callback(token, cb_id)
                        p = data.split(":")
                        sec_name = p[1]
                        pg = int(p[2]) if len(p) > 2 else 0
                        set_user_state(user_id, {"del_batch_sel": []})
                        from subparser.podkop import CACHED_DELAYS
                        delays = CACHED_DELAYS.get(sec_name, {})
                        t, kb = get_servers_screen(delays_map=delays, section=sec_name, page=pg, delete_mode=True, selected_indices=[])
                        edit_msg(token, chat_id, msg_id, t, kb)

                    elif data.startswith("tgl_del:"):
                        answer_callback(token, cb_id)
                        p = data.split(":")
                        sec_name = p[1]
                        idx = int(p[2])
                        pg = int(p[3]) if len(p) > 3 else 0
                        st = get_user_state(user_id)
                        sel_set = set(st.get("del_batch_sel", []))
                        if idx in sel_set:
                            sel_set.remove(idx)
                        else:
                            sel_set.add(idx)
                        sel_list = list(sel_set)
                        set_user_state(user_id, {"del_batch_sel": sel_list})
                        from subparser.podkop import CACHED_DELAYS
                        delays = CACHED_DELAYS.get(sec_name, {})
                        t, kb = get_servers_screen(delays_map=delays, section=sec_name, page=pg, delete_mode=True, selected_indices=sel_list)
                        edit_msg(token, chat_id, msg_id, t, kb)

                    elif data.startswith("sel_dead:"):
                        answer_callback(token, cb_id, "Выбираю [DEAD]...")
                        p = data.split(":")
                        sec_name = p[1]
                        pg = int(p[2]) if len(p) > 2 else 0
                        from subparser.podkop import CACHED_DELAYS, get_podkop_links
                        delays = CACHED_DELAYS.get(sec_name, {})
                        links = get_podkop_links(sec_name)
                        st = get_user_state(user_id)
                        sel_set = set(st.get("del_batch_sel", []))
                        for i in range(len(links)):
                            if delays.get(i, -1) <= 0 and i in delays:
                                sel_set.add(i)
                        sel_list = list(sel_set)
                        set_user_state(user_id, {"del_batch_sel": sel_list})
                        t, kb = get_servers_screen(delays_map=delays, section=sec_name, page=pg, delete_mode=True, selected_indices=sel_list)
                        edit_msg(token, chat_id, msg_id, t, kb)

                    elif data.startswith("exit_del_mode:"):
                        answer_callback(token, cb_id)
                        p = data.split(":")
                        sec_name = p[1]
                        pg = int(p[2]) if len(p) > 2 else 0
                        clear_user_state(user_id)
                        from subparser.podkop import CACHED_DELAYS
                        delays = CACHED_DELAYS.get(sec_name, {})
                        t, kb = get_servers_screen(delays_map=delays, section=sec_name, page=pg, delete_mode=False, selected_indices=[])
                        edit_msg(token, chat_id, msg_id, t, kb)

                    elif data.startswith("srv_del_pg:"):
                        answer_callback(token, cb_id)
                        p = data.split(":")
                        sec_name = p[1]
                        pg = int(p[2])
                        st = get_user_state(user_id)
                        sel_list = st.get("del_batch_sel", [])
                        from subparser.podkop import CACHED_DELAYS
                        delays = CACHED_DELAYS.get(sec_name, {})
                        t, kb = get_servers_screen(delays_map=delays, section=sec_name, page=pg, delete_mode=True, selected_indices=sel_list)
                        edit_msg(token, chat_id, msg_id, t, kb)

                    elif data.startswith("apply_bdel:"):
                        answer_callback(token, cb_id, "Применяю...")
                        p = data.split(":")
                        sec_name = p[1]
                        st = get_user_state(user_id)
                        sel_list = st.get("del_batch_sel", [])
                        clear_user_state(user_id)
                        if not sel_list:
                            answer_callback(token, cb_id, "Ничего не выбрано")
                            continue

                        from subparser.podkop import CACHED_DELAYS, get_podkop_links, delete_nodes_batch
                        all_links = get_podkop_links(sec_name)
                        sel_set = set(sel_list)
                        cnt = len(sel_set)

                        old_delays = CACHED_DELAYS.get(sec_name, {})
                        new_delays = {}
                        new_idx = 0
                        for old_idx in range(len(all_links)):
                            if old_idx not in sel_set:
                                if old_idx in old_delays:
                                    new_delays[new_idx] = old_delays[old_idx]
                                new_idx += 1
                        CACHED_DELAYS[sec_name] = new_delays

                        delete_nodes_batch(sel_list, section=sec_name)

                        msg_alert = f"🗑 <b>Удалено узлов: {cnt} шт.</b>\n<i>(Служба перезапускается в фоне)</i>\n\n"
                        t, kb = get_servers_screen(delays_map=new_delays, section=sec_name, page=0, delete_mode=False, selected_indices=[])
                        edit_msg(token, chat_id, msg_id, f"{msg_alert}{t}", kb)
                        subprocess.Popen("sh -c 'sleep 2 && /etc/init.d/podkop restart' >/dev/null 2>&1 &", shell=True)

                    elif data.startswith("srv_pg:"):
                        answer_callback(token, cb_id)
                        p = data.split(":")
                        sec_name = p[1]
                        pg = int(p[2]) if len(p) > 2 else 0
                        t, kb = get_servers_screen(section=sec_name, page=pg)
                        edit_msg(token, chat_id, msg_id, t, kb)

                    elif data.startswith("ping_sec:"):
                        p = data.split(":")
                        sec_name = p[1]
                        pg = int(p[2]) if len(p) > 2 else 0
                        from subparser.podkop import get_podkop_links, batch_ping_nodes
                        links = get_podkop_links(sec_name)
                        delays = batch_ping_nodes(links, section=sec_name, force=True)
                        t, kb = get_servers_screen(delays_map=delays, section=sec_name, page=pg)
                        edit_msg(token, chat_id, msg_id, t, kb)
                        answer_callback(token, cb_id, "⚡️ Пинги обновлены!")

                    elif data == "noop":
                        answer_callback(token, cb_id)

                    elif data.startswith("auto_mode:"):
                        answer_callback(token, cb_id, "Возврат в авто-режим...")
                        p = data.split(":")
                        sec_name = p[1] if len(p) > 1 else "main"
                        pg = int(p[2]) if len(p) > 2 else 0
                        reset_to_auto_urltest(sec_name)
                        t, kb = get_servers_screen(section=sec_name, page=pg)
                        edit_msg(token, chat_id, msg_id, t, kb)

                    elif data.startswith("sel:"):
                        answer_callback(token, cb_id)
                        p = data.split(":")
                        sec_name = p[1] if len(p) > 1 else "main"
                        try:
                            idx = int(p[2])
                        except Exception:
                            idx = 0
                        pg = int(p[3]) if len(p) > 3 else (idx // 10)

                        links = get_podkop_links(sec_name)
                        if idx < 0 or idx >= len(links):
                            t, kb = get_servers_screen(section=sec_name, page=pg)
                            edit_msg(token, chat_id, msg_id, t, kb)
                            continue

                        name = get_node_name(links[idx], idx)
                        proto = urllib.parse.urlsplit(links[idx]).scheme.upper()
                        target_tag = f"{sec_name}-{idx+1}-out"
                        active_manual_tag, is_auto = get_section_routing_state(sec_name)
                        is_active = (not is_auto and target_tag == active_manual_tag)
                        status_badge = "⚡ <b>АКТИВЕН В ДАННЫЙ МОМЕНТ (Вручную)</b>" if is_active else ("🤖 <b>Автовыбор (URL-Test)</b>" if is_auto else "💤 <b>В резерве</b>")

                        card = (
                            f"📌 <b>Узел #{idx+1} [Секция {sec_name}]</b>\n\n"
                            f"🏷 <b>Имя:</b> <code>{name}</code>\n"
                            f"⚡ <b>Протокол:</b> <code>{proto}</code>\n"
                            f"🏷 <b>Тег:</b> <code>{target_tag}</code>\n"
                            f"Статус: {status_badge}\n\n"
                            "Действие:"
                        )
                        btn_row = []
                        if not is_active:
                            btn_row.append({"text": "⚡ Сделать активным", "callback_data": f"act:{sec_name}:{idx}:{pg}"})
                        else:
                            btn_row.append({"text": "🔄 Сбросить на Авто (URL-Test)", "callback_data": f"auto_mode:{sec_name}:{pg}"})

                        kb = {"inline_keyboard": [
                            btn_row,
                            [{"text": f"🗑 Удалить из [{sec_name}]", "callback_data": f"del:{sec_name}:{idx}:{pg}"}],
                            [{"text": "◀️ Назад к списку", "callback_data": f"srv_pg:{sec_name}:{pg}"}]
                        ]}
                        edit_msg(token, chat_id, msg_id, card, kb)

                    elif data.startswith("act:"):
                        answer_callback(token, cb_id, "Переключаю...")
                        p = data.split(":")
                        sec_name = p[1] if len(p) > 1 else "main"
                        try:
                            idx = int(p[2])
                        except Exception:
                            idx = 0
                        pg = int(p[3]) if len(p) > 3 else (idx // 10)

                        ok, actual_tag = switch_active_node(sec_name, idx)
                        links = get_podkop_links(sec_name)
                        name = get_node_name(links[idx], idx) if idx < len(links) else f"#{idx+1}"
                        proto = urllib.parse.urlsplit(links[idx]).scheme.upper() if idx < len(links) else "PROXY"

                        card = (
                            f"📌 <b>Узел #{idx+1} [Секция {sec_name}]</b>\n\n"
                            f"🏷 <b>Имя:</b> <code>{name}</code>\n"
                            f"⚡ <b>Протокол:</b> <code>{proto}</code>\n"
                            f"🏷 <b>Тег:</b> <code>{actual_tag}</code>\n"
                            f"Статус: ⚡ <b>АКТИВЕН В ДАННЫЙ МОМЕНТ</b>\n\n"
                            "✅ <i>Трафик зафиксирован через этот сервер.</i>"
                        )
                        kb = {"inline_keyboard": [
                            [{"text": "🔄 Сбросить на Авто (URL-Test)", "callback_data": f"auto_mode:{sec_name}:{pg}"}],
                            [{"text": f"🗑 Удалить из [{sec_name}]", "callback_data": f"del:{sec_name}:{idx}:{pg}"}],
                            [{"text": "◀️ Назад к списку", "callback_data": f"srv_pg:{sec_name}:{pg}"}]
                        ]}
                        edit_msg(token, chat_id, msg_id, card, kb)

                    elif data.startswith("del:"):
                        answer_callback(token, cb_id, "Удаление...")
                        p = data.split(":")
                        sec_name = p[1] if len(p) > 1 else "main"
                        try:
                            idx = int(p[2])
                        except Exception:
                            idx = 0
                        pg = int(p[3]) if len(p) > 3 else 0
                        ok, res = delete_node(idx, section=sec_name)
                        alert = f"🗑 Удален: <b>{res}</b>" if ok else f"❌ Ошибка: {res}"
                        t, kb = get_servers_screen(section=sec_name, page=pg)
                        edit_msg(token, chat_id, msg_id, f"{alert}\n\n{t}", kb)

        except Exception:
            log.exception("Необработанная ошибка в цикле бота")
            time.sleep(3)

if __name__ == "__main__":
    main()
