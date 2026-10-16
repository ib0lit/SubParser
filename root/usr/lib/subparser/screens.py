# -*- coding: utf-8 -*-
import os
import html
import subprocess
import urllib.parse
from concurrent.futures import ThreadPoolExecutor
from .config import get_uci_val, UA_CHOICES, LOG_FILE
from .subs import get_subscriptions
from .podkop import (
    get_all_podkop_sections, get_podkop_links, get_node_name,
    get_podkop_service_status, get_section_routing_state, CACHED_DELAYS
)
from .system import get_blocked_macs, get_wifi_stations, get_ip_by_mac, check_single_service

def get_main_screen():
    text = "🎛 <b>Панель управления SubParser & OpenWrt</b>\n\nВыберите необходимое действие:"
    kb = {"inline_keyboard": [
        [{"text": "⚙️ Меню парсера", "callback_data": "parser_menu"}, {"text": "📋 Серверы и пинг", "callback_data": "servers"}],
        [{"text": "📟 Ресурсы роутера", "callback_data": "sysinfo"}, {"text": "👥 Клиенты сети", "callback_data": "clients"}],
        [{"text": "🛡 Доступ к сервисам", "callback_data": "check_services"}, {"text": "📊 Статус системы", "callback_data": "status"}],
        [{"text": "♻️ Рестарт Podkop", "callback_data": "restart"}, {"text": "⚠️ Reboot роутера", "callback_data": "reboot"}]
    ]}
    return text, kb

def get_parser_menu_screen(prompt_custom_ping=False):
    threshold = get_uci_val("ping_threshold", "350")
    max_best = get_uci_val("max_best_nodes", "0")
    filter_ru = get_uci_val("filter_ru", "1")
    cron_int = get_uci_val("interval", "never")
    per_sec = get_uci_val("per_section_config", "0") == "1"
    all_secs = get_all_podkop_sections()
    ch = get_uci_val("custom_hour", "3")
    cm = get_uci_val("custom_minute", "0").zfill(2)
    cron_labels = {
        "never": "Отключен",
        "1h": "1 час",
        "12h": "12 часов",
        "24h": "24 часа",
        "custom_daily": f"Ежедневно в {ch}:{cm} (LuCI)",
        "custom_every_h": f"Каждые {ch} ч. (LuCI)"
    }
    cron_badge = cron_labels.get(cron_int, cron_int)
    ru_badge = "Вкл (фильтровать)" if filter_ru == "1" else "Выкл (сохранять)"
    limit_badge = "Все (без лимита)" if max_best == "0" else f"Топ-{max_best}"
    subs_count = len(get_subscriptions())

    fp_val = get_uci_val("override_fp", "firefox").lower().strip()
    fp_map = {
        "as_is": "Как в подписке",
        "firefox": "Firefox 🦊",
        "chrome": "Chrome 🌐",
        "safari": "Safari 🍏",
        "ios": "iOS 📱",
        "edge": "Edge 🔷",
        "random": "Random 🎲",
        "randomized": "Randomized 🎲"
    }
    fp_badge = fp_map.get(fp_val, fp_val.capitalize())

    if per_sec:
        sec_parts = []
        for s in all_secs:
            s_en = get_uci_val(f"sec_en_{s}", "1") == "1"
            s_mode = get_uci_val(f"sec_mode_{s}", "replace")
            if s_en:
                badge = "Замена" if s_mode == "replace" else "Добавление"
                sec_parts.append(f"{s} ({badge})")
        sections_str = ", ".join(sec_parts) if sec_parts else "Все выключены"
    else:
        raw_targets = get_uci_val("target_section", "main").split()
        existing_valid = [s for s in raw_targets if s in all_secs]
        for s in all_secs:
            if s not in existing_valid:
                existing_valid.append(s)
        target_list = existing_valid if existing_valid else ["main"]
        g_mode = get_uci_val("update_mode", "replace")
        mode_label = "Замена" if g_mode == "replace" else "Добавление"
        sections_str = f"{', '.join(target_list)} ({mode_label})"

    text = (
        "⚙️ <b>Параметры парсера SubParser</b>\n\n"
        f"📋 <b>Подписок:</b> <code>{subs_count} шт.</code>\n"
        f"🎯 <b>Секции Podkop:</b> <code>{sections_str}</code>\n"
        f"⚡️ <b>Порог пинга:</b> <code>{threshold} ms</code>\n"
        f"🔝 <b>Лимит узлов:</b> <code>{limit_badge}</code>\n"
        f"⏰ <b>Расписание:</b> <code>{cron_badge}</code>\n"
        f"🇷🇺 <b>Фильтр RU:</b> <code>{ru_badge}</code>\n"
        f"🎭 <b>uTLS Fingerprint:</b> <code>{fp_badge}</code>\n"
    )
    if prompt_custom_ping:
        text += "\n✏️ <b>Введите желаемый пинг (например, 280) в ответном сообщении:</b>"

    kb = {
        "inline_keyboard": [
            [
                {"text": f"📋 Подписки ({subs_count})", "callback_data": "subs_list"},
                {"text": "🎯 Секции Podkop", "callback_data": "bot_sections_menu"}
            ],
            [
                {"text": "150ms" + (" 🔘" if threshold == "150" else ""), "callback_data": "th:150"},
                {"text": "350ms" + (" 🔘" if threshold == "350" else ""), "callback_data": "th:350"},
                {"text": "500ms" + (" 🔘" if threshold == "500" else ""), "callback_data": "th:500"}
            ],
            [
                {"text": "✏️ Задать свой", "callback_data": "ask_custom_th"},
                {"text": f"Лимит: {limit_badge}", "callback_data": "cycle_limit"}
            ],
            [
                {"text": f"Расписание: {cron_badge}", "callback_data": "cycle_cron"},
                {"text": f"RU: {'Фильтр' if filter_ru == '1' else 'Все'}", "callback_data": "toggle_ru"}
            ],
            [
                {"text": f"🎭 Фингерпринт: {fp_badge}", "callback_data": "menu_fp"}
            ],
            [
                {"text": "🚀 Запустить парсер сейчас", "callback_data": "run_parser"}
            ],
            [
                {"text": "📜 Лог синхронизации", "callback_data": "view_log"},
                {"text": "◀ Главное меню", "callback_data": "home"}
            ]
        ]
    }
    return text, kb


def get_fp_menu_screen():
    cur = get_uci_val("override_fp", "firefox").lower().strip()
    fps = [
        ("firefox", "🦊 Firefox (РФ)"),
        ("chrome", "🌐 Chrome"),
        ("safari", "🍏 Safari"),
        ("ios", "📱 iOS"),
        ("edge", "🔷 Edge"),
        ("randomized", "🎲 Randomized"),
        ("as_is", "↩️ Как в подписке")
    ]
    kb = {"inline_keyboard": []}
    row = []
    for val, label in fps:
        mark = " 🔘" if cur == val else ""
        row.append({"text": f"{label}{mark}", "callback_data": f"set_fp:{val}"})
        if len(row) == 2:
            kb["inline_keyboard"].append(row)
            row = []
    if row:
        kb["inline_keyboard"].append(row)
    kb["inline_keyboard"].append([{"text": "◀ Назад в параметры", "callback_data": "parser_menu"}])

    mapping = {
        "as_is": "Как в подписке", "firefox": "Firefox 🦊", "chrome": "Chrome 🌐",
        "safari": "Safari 🍏", "ios": "iOS 📱", "edge": "Edge 🔷", "randomized": "Randomized 🎲"
    }
    cur_label = mapping.get(cur, cur.capitalize())
    text = (
        "🎭 <b>uTLS Fingerprint (эмуляция браузера)</b>\n\n"
        "Глобально заменяет TLS Client Hello для всех узлов во избежание блокировок по сигнатурам браузера.\n\n"
        f"Текущий режим: <b>{cur_label}</b>"
    )
    return text, kb

def get_subs_list_screen():
    subs = get_subscriptions()
    if not subs:
        text = "📋 <b>Источники подписок</b>\n\nАктивные подписки не найдены.\n\n<i>Отправьте ссылку на подписку прямо в чат для её добавления.</i>"
        kb = {"inline_keyboard": [
            [{"text": "◀️ В меню парсера", "callback_data": "parser_menu"}]
        ]}
        return text, kb

    lines = []
    keyboard = []
    for s in subs:
        i = s["idx"]
        en_ico = "🟢" if s["enabled"] == "1" else "🔴"
        ua_short = s["user_agent"]
        lines.append(f"{i+1}. {en_ico} <b>{s['name']}</b>\n   └ UA: <code>{ua_short}</code>")
        keyboard.append([{"text": f"⚙️ #{i+1} {s['name'][:20]}", "callback_data": f"sub_card:{i}"}])

    text = f"📋 <b>Источники подписок ({len(subs)} шт.)</b>\n\n" + "\n\n".join(lines) + "\n\n<i>Выберите подписку для настройки или удаления:</i>"
    keyboard.append([{"text": "🔄 Обновить", "callback_data": "subs_list"}, {"text": "◀️ В меню парсера", "callback_data": "parser_menu"}])
    return text, {"inline_keyboard": keyboard}

def get_sub_card_screen(sub_idx):
    subs = get_subscriptions()
    target = next((s for s in subs if s["idx"] == sub_idx), None)
    if not target:
        return get_subs_list_screen()

    is_en = target["enabled"] == "1"
    st_badge = "🟢 <b>Активна (участвует в парсинге)</b>" if is_en else "🔴 <b>Отключена (пропускается)</b>"
    btn_en_txt = "⏸ Выключить подписку" if is_en else "▶️ Включить подписку"
    ua_title = UA_CHOICES.get(target["user_agent"], target["user_agent"])

    if target["url"]:
        parsed = urllib.parse.urlsplit(target["url"])
        domain = parsed.netloc or "н/д"
        url_preview = html.escape(target["url"][:48]) + ("..." if len(target["url"]) > 48 else "")
    else:
        domain = "не задан"
        url_preview = "⚠️ <i>URL не указан в настройках</i>"

    text = (
        f"📋 <b>Управление подпиской #{sub_idx+1}</b>\n\n"
        f"• <b>Название:</b> <code>{target['name']}</code>\n"
        f"• <b>Сервер:</b> <code>{domain}</code>\n"
        f"• <b>Клиент (UA):</b> <code>{ua_title}</code>\n"
        f"• <b>Статус:</b> {st_badge}\n"
        f"• <b>URL:</b> <code>{url_preview}</code>\n"
    )
    kb = {"inline_keyboard": [
        [{"text": btn_en_txt, "callback_data": f"sub_tgl:{sub_idx}"}],
        [{"text": f"👤 Сменить UA: {target['user_agent']} ➔", "callback_data": f"sub_cycle_ua:{sub_idx}"}],
        [{"text": "🗑 Удалить подписку", "callback_data": f"sub_del_ask:{sub_idx}"}],
        [{"text": "◀️ Назад к списку подписок", "callback_data": "subs_list"}]
    ]}
    return text, kb

def get_sub_delete_confirm_screen(sub_idx):
    subs = get_subscriptions()
    target = next((s for s in subs if s["idx"] == sub_idx), None)
    name = target["name"] if target else f"#{sub_idx+1}"
    text = (
        f"⚠️ <b>Удаление подписки</b>\n\n"
        f"Вы действительно хотите навсегда удалить подписку <b>«{name}»</b> из роутера?"
    )
    kb = {"inline_keyboard": [
        [{"text": "❌ Да, удалить навсегда!", "callback_data": f"sub_del_do:{sub_idx}"}],
        [{"text": "↩️ Отмена", "callback_data": f"sub_card:{sub_idx}"}]
    ]}
    return text, kb

def get_sections_menu_screen():
    per_sec = get_uci_val("per_section_config", "0") == "1"
    all_secs = get_all_podkop_sections()
    keyboard = []

    if not per_sec:
        raw_targets = get_uci_val("target_section", "main").split()
        target_set = set(raw_targets) if raw_targets else set(all_secs)
        for s in all_secs:
            if s not in target_set:
                target_set.add(s)

        g_mode = get_uci_val("update_mode", "replace")
        mode_label = "🔄 Замена серверов" if g_mode == "replace" else "➕ Добавление (без дублей)"

        status_text = (
            "🌐 <b>Общий режим обновления</b>\n"
            f"Текущее действие: <b>{mode_label}</b>\n"
            "Все выбранные секции обновляются по единому правилу."
        )
        keyboard.append([{"text": "Индивидуальный режим: ❌ ВЫКЛ", "callback_data": "toggle_per_sec"}])
        keyboard.append([{"text": f"Режим: {mode_label}", "callback_data": "toggle_glob_mode"}])

        sec_buttons = []
        for s in all_secs:
            is_active = s in target_set
            icon = "✅" if is_active else "⬜"
            sec_buttons.append({"text": f"{icon} {s}", "callback_data": f"tgl_sec:{s}"})
            if len(sec_buttons) == 2:
                keyboard.append(sec_buttons)
                sec_buttons = []
        if sec_buttons:
            keyboard.append(sec_buttons)
    else:
        status_text = "🎯 <b>Индивидуальный режим</b>\nНажимайте на секцию для изменения её персонального режима:"
        keyboard.append([{"text": "Индивидуальный режим: ✅ ВКЛ", "callback_data": "toggle_per_sec"}])

        for s in all_secs:
            s_en = get_uci_val(f"sec_en_{s}", "1") == "1"
            s_mode = get_uci_val(f"sec_mode_{s}", "replace")
            if not s_en:
                badge = "❌ Отключена"
            elif s_mode == "replace":
                badge = "🔄 Замена"
            else:
                badge = "➕ Добавление"

            keyboard.append([{"text": f"{s}: {badge}", "callback_data": f"cycle_sec:{s}"}])

    keyboard.append([{"text": "◀️ Назад в меню парсера", "callback_data": "parser_menu"}])
    return f"📁 <b>Настройка секций Podkop</b>\n\n{status_text}", {"inline_keyboard": keyboard}

def get_servers_screen(delays_map=None, section="main", page=0, delete_mode=False, selected_indices=None):
    if selected_indices is None:
        selected_indices = []
    sel_set = set(selected_indices)
    links = get_podkop_links(section)
    all_secs = get_all_podkop_sections()
    back_btn = {"text": "◀️ К секциям", "callback_data": "servers"} if len(all_secs) > 1 else {"text": "◀️ В меню", "callback_data": "home"}
    if not links:
        return f"🌐 В секции <b>{section}</b> нет серверов.", {"inline_keyboard": [[back_btn]]}
    if delays_map is not None:
        CACHED_DELAYS[section] = delays_map
    else:
        delays_map = CACHED_DELAYS.get(section, {})
    active_manual_tag, is_auto = get_section_routing_state(section)
    page_size = 10
    total_nodes = len(links)
    total_pages = (total_nodes + page_size - 1) // page_size
    if page < 0:
        page = 0
    elif page >= total_pages:
        page = total_pages - 1
    start_idx = page * page_size
    end_idx = min(start_idx + page_size, total_nodes)
    page_links = links[start_idx:end_idx]
    keyboard = []
    for i, link in enumerate(page_links, start=start_idx):
        tag_cur = f"{section}-{i+1}-out"
        is_active = (not is_auto and tag_cur == active_manual_tag)
        name = get_node_name(link, i)
        d = delays_map.get(i, -1) if delays_map else -1
        if d > 0:
            ico = "🟢" if d < 180 else ("🟡" if d < 350 else "🔴")
            prefix = f"{ico} [{d}ms]"
        elif delays_map and i in delays_map:
            prefix = "❌ [DEAD]"
        else:
            prefix = f"#{i+1}"
        if delete_mode:
            cb_ico = "☑️" if i in sel_set else "◻️"
            keyboard.append([{"text": f"{cb_ico} {prefix} {name[:20]}", "callback_data": f"tgl_del:{section}:{i}:{page}"}])
        else:
            btn_mark = "🔘 " if is_active else ""
            keyboard.append([{"text": f"{btn_mark}{prefix} {name[:20]}", "callback_data": f"sel:{section}:{i}:{page}"}])
    nav_row = []
    pg_callback_prefix = "srv_del_pg" if delete_mode else "srv_pg"
    if page > 0:
        nav_row.append({"text": "⬅️", "callback_data": f"{pg_callback_prefix}:{section}:{page-1}"})
    nav_row.append({"text": f"📄 {page+1}/{total_pages}", "callback_data": "noop"})
    if page < total_pages - 1:
        nav_row.append({"text": "➡️", "callback_data": f"{pg_callback_prefix}:{section}:{page+1}"})
    if nav_row:
        keyboard.append(nav_row)
    if delete_mode:
        cnt_sel = len(sel_set)
        keyboard.append([{"text": f"🗑 Подтвердить удаление ({cnt_sel})", "callback_data": f"apply_bdel:{section}:{page}"}])
        keyboard.append([{"text": "❌ Выбрать все [DEAD]", "callback_data": f"sel_dead:{section}:{page}"}])
        keyboard.append([{"text": "◀️ Отмена (выход)", "callback_data": f"exit_del_mode:{section}:{page}"}])
        title = f"🌐 <b>Узлы Podkop [{section}] [Мультиудаление]</b> ({total_nodes} шт., стр. {page+1}/{total_pages}):\n<i>Отметьте узлы для удаления:</i>"
    else:
        auto_label = "✅ Режим: Авто (URL-Test)" if is_auto else "🔄 Включить Авто (URL-Test)"
        keyboard.append([{"text": auto_label, "callback_data": f"auto_mode:{section}:{page}"}])
        keyboard.append([
            {"text": "⚡️ Пинг", "callback_data": f"ping_sec:{section}:{page}"},
            {"text": "🗑 Мультиудаление", "callback_data": f"enter_del_mode:{section}:{page}"}
        ])
        keyboard.append([back_btn])
        title = f"🌐 <b>Узлы Podkop [{section}]</b> ({total_nodes} шт., стр. {page+1}/{total_pages}):\n<i>Нажмите для управления:</i>"
    return title, {"inline_keyboard": keyboard}

def get_sections_selector_screen():
    all_secs = get_all_podkop_sections()
    keyboard = []
    lines = []
    for s in all_secs:
        count = len(get_podkop_links(s))
        lines.append(f"• <b>{s}</b>: <code>{count} узлов</code>")
        keyboard.append([{"text": f"📁 Секция: {s} ({count} шт.)", "callback_data": f"sec_view:{s}"}])

    keyboard.append([{"text": "◀️ В главное меню", "callback_data": "home"}])
    text = "📋 <b>Серверы Podkop: выбор секции</b>\n\n" + "\n".join(lines) + "\n\n<i>Выберите секцию для просмотра серверов:</i>"
    return text, {"inline_keyboard": keyboard}

def get_system_metrics():
    uptime_str = "н/д"
    try:
        with open("/proc/uptime", "r") as f:
            up_secs = int(float(f.readline().split()[0]))
            d, rem = divmod(up_secs, 86400)
            h, rem = divmod(rem, 3600)
            m, s = divmod(rem, 60)
            uptime_str = f"{d}д {h}ч {m}м" if d > 0 else f"{h}ч {m}м {s}с"
    except Exception:
        pass

    temp_str = "н/д"
    for path in ["/sys/class/thermal/thermal_zone0/temp", "/sys/class/hwmon/hwmon0/temp1_input"]:
        if os.path.exists(path):
            try:
                with open(path, "r") as f:
                    val = float(f.read().strip())
                    if val > 1000:
                        val /= 1000
                    temp_str = f"{val:.1f}°C"
                    break
            except Exception:
                pass

    load_str = "н/д"
    try:
        with open("/proc/loadavg", "r") as f:
            parts = f.read().strip().split()
            load_str = f"{parts[0]}, {parts[1]}, {parts[2]}"
    except Exception:
        pass

    ram_str = "н/д"
    ram_bar = ""
    try:
        mem = {}
        with open("/proc/meminfo", "r") as f:
            for l in f:
                k, v = l.split(":")
                mem[k.strip()] = int(v.split()[0])
        total = mem.get("MemTotal", 0) // 1024
        avail = mem.get("MemAvailable", mem.get("MemFree", 0)) // 1024
        used = total - avail
        pct = int((used / total) * 100) if total > 0 else 0
        filled = pct // 10
        ram_bar = f"[{'█' * filled}{'░' * (10 - filled)}] {pct}%"
        ram_str = f"{used} MB / {total} MB"
    except Exception:
        pass

    text = (
        "📟 <b>Системные ресурсы роутера</b>\n\n"
        f"• <b>Время работы:</b> <code>{uptime_str}</code>\n"
        f"• <b>Температура SoC:</b> <code>{temp_str}</code>\n"
        f"• <b>Load Average:</b> <code>{load_str}</code>\n"
        f"• <b>Память RAM:</b> <code>{ram_str}</code>\n"
        f"  <code>{ram_bar}</code>\n"
    )
    kb = {
        "inline_keyboard": [
            [{"text": "🔄 Обновить показатели", "callback_data": "sysinfo"}, {"text": "🗑 Закрыть", "callback_data": "close_msg"}],
            [{"text": "◀️ В главное меню", "callback_data": "home"}]
        ]
    }
    return text, kb

def get_clients_screen():
    clients = {}
    lease_file = "/tmp/dhcp.leases"
    if os.path.exists(lease_file):
        try:
            with open(lease_file, "r") as f:
                for line in f:
                    parts = line.strip().split()
                    if len(parts) >= 4:
                        mac = parts[1].upper()
                        ip = parts[2]
                        hname = parts[3]
                        if hname == "*":
                            hname = "Без имени"
                        clients[ip] = {"mac": mac, "name": hname}
        except Exception:
            pass

    arp_file = "/proc/net/arp"
    if os.path.exists(arp_file):
        try:
            with open(arp_file, "r") as f:
                for line in f.readlines()[1:]:
                    parts = line.strip().split()
                    if len(parts) >= 6:
                        ip = parts[0]
                        mac = parts[3].upper()
                        flags = parts[2]
                        if flags != "0x0" and mac != "00:00:00:00:00:00":
                            if ip not in clients:
                                clients[ip] = {"mac": mac, "name": "Статический IP"}
        except Exception:
            pass

    if not clients:
        text = "👥 <b>Подключенные устройства</b>\n\nАктивные клиенты не обнаружены."
        kb = {"inline_keyboard": [
            [{"text": "🗑 Закрыть", "callback_data": "close_msg"}, {"text": "◀️ В главное меню", "callback_data": "home"}]
        ]}
    else:
        wifi_info = get_wifi_stations()
        blocked = get_blocked_macs()

        def ip_sort_key(ip_str):
            try:
                return [int(p) for p in ip_str.split(".")]
            except Exception:
                return [0, 0, 0, 0]

        sorted_ips = sorted(clients.keys(), key=ip_sort_key)
        lines = []
        keyboard = []

        for i, ip in enumerate(sorted_ips, 1):
            c = clients[ip]
            mac = c["mac"]
            is_bl = mac in blocked
            bl_icon = "🚫 " if is_bl else ""

            if mac in wifi_info:
                w = wifi_info[mac]
                tx_part = f" (<code>{w['tx']}</code>)" if w.get("tx") else ""
                conn_badge = f"🛜 <b>{w['type']}</b> | {w['signal']}{tx_part}"
            else:
                conn_badge = "🔌 <b>LAN Кабель</b>"

            lines.append(
                f"{i}. {bl_icon}<b>{c['name']}</b>\n"
                f"   ├ IP: <code>{ip}</code> | MAC: <code>{mac}</code>\n"
                f"   └ Связь: {conn_badge}"
            )
            keyboard.append([{"text": f"⚙️ {bl_icon}{c['name'][:18]} ({ip})", "callback_data": f"dev:{mac}"}])

        text = (
            f"👥 <b>Устройства в сети ({len(clients)} шт.)</b>\n\n"
            + "\n\n".join(lines) +
            "\n\n<i>Выберите устройство для управления доступом:</i>"
        )
        keyboard.append([{"text": "🔄 Обновить список", "callback_data": "clients"}, {"text": "🗑 Закрыть", "callback_data": "close_msg"}])
        keyboard.append([{"text": "◀️ В главное меню", "callback_data": "home"}])
        kb = {"inline_keyboard": keyboard}

    return text, kb

def get_device_action_screen(mac, force_blocked=None):
    mac_u = mac.upper()
    if force_blocked is not None:
        is_blocked = force_blocked
    else:
        blocked = [m.upper() for m in get_blocked_macs()]
        is_blocked = mac_u in blocked

    ip = get_ip_by_mac(mac_u) or "н/д"
    hname = "Устройство"
    if os.path.exists("/tmp/dhcp.leases"):
        try:
            with open("/tmp/dhcp.leases", "r") as f:
                for line in f:
                    p = line.strip().split()
                    if len(p) >= 4 and p[1].upper() == mac_u:
                        hname = p[3] if p[3] != "*" else "Без имени"
                        break
        except Exception:
            pass

    if is_blocked:
        status_badge = "🔴 <b>Интернет заблокирован</b>"
        btn_text = "✅ Разблокировать доступ"
        action = f"unblk:{mac_u}"
    else:
        status_badge = "🟢 <b>Доступ открыт</b>"
        btn_text = "🚫 Заблокировать интернет"
        action = f"blk:{mac_u}"

    text = (
        f"📱 <b>Управление клиентом: {hname}</b>\n\n"
        f"• <b>IP адрес:</b> <code>{ip}</code>\n"
        f"• <b>MAC адрес:</b> <code>{mac_u}</code>\n"
        f"• <b>Статус:</b> {status_badge}\n"
    )
    kb = {
        "inline_keyboard": [
            [{"text": btn_text, "callback_data": action}],
            [{"text": "◀️ Назад к списку устройств", "callback_data": "clients"}]
        ]
    }
    return text, kb

def get_services_status_screen():
    targets = [
        ("YouTube", "www.youtube.com", "https://www.youtube.com/generate_204"),
        ("Discord", "discord.com", "https://discord.com/api/v9/gateway"),
        ("Steam", "store.steampowered.com", "https://store.steampowered.com/"),
        ("Instagram", "www.instagram.com", "https://www.instagram.com/robots.txt"),
        ("TikTok", "www.tiktok.com", "https://www.tiktok.com/robots.txt"),
        ("Яндекс", "ya.ru", "https://ya.ru/robots.txt"),
        ("Госуслуги", "www.gosuslugi.ru", "https://www.gosuslugi.ru/robots.txt")
    ]
    with ThreadPoolExecutor(max_workers=7) as pool:
        results = list(pool.map(check_single_service, targets))

    lines = []
    for name, is_proxied, ok, ms in results:
        route_badge = "🛡 <i>Прокси</i>" if is_proxied else "🇷🇺 <i>Прямой</i>"
        if ok and ms > 0:
            status_badge = f"🟢 <code>{ms} ms</code>"
        else:
            status_badge = "🔴 <b>Блок / Таймаут</b>"
        lines.append(f"• <b>{name}</b>: {status_badge} | {route_badge}")

    text = (
        "🧭 <b>Маршрутизация и доступность сервисов</b>\n\n"
        + "\n".join(lines) +
        "\n\n<i>🛡 Прокси — через туннель Podkop (Fake-IP)\n🇷🇺 Прямой — без VPN через провайдера РФ</i>"
    )
    kb = {
        "inline_keyboard": [
            [{"text": "🔄 Перепроверить статус", "callback_data": "check_services"}, {"text": "🗑 Закрыть", "callback_data": "close_msg"}],
            [{"text": "◀️ В главное меню", "callback_data": "home"}]
        ]
    }
    return text, kb

def get_status_screen():
    links = get_podkop_links("main")
    svc_ok, pid, ram_mb, mode_str = get_podkop_service_status()
    st_badge = "🟢 Запущена" if svc_ok else "🔴 Остановлена"

    ps_check = subprocess.run(["pgrep", "-f", "subparser.py"], stdout=subprocess.PIPE, text=True)
    is_sync = "⏳ Выполняется" if ps_check.stdout.strip() else "💤 В ожидании"

    text = (
        "📊 <b>Детальный статус системы и Podkop</b>\n\n"
        f"• <b>Служба Podkop:</b> {st_badge}\n"
        f"• <b>Ядро sing-box:</b> " + (f"🟢 PID {pid} (RAM: <code>{ram_mb}</code>)" if pid else "🔴 Остановлен") + "\n"
        f"• <b>Режим трафика:</b> <code>{mode_str}</code>\n"
        f"• <b>Серверов в секции main:</b> <code>{len(links)} шт.</code>\n"
        f"• <b>Синхронизация SubParser:</b> {is_sync}\n"
    )
    kb = {"inline_keyboard": [
        [{"text": "🚀 Замер скорости канала", "callback_data": "run_speedtest"}],
        [{"text": "📜 Лог системы (logread)", "callback_data": "log_sys"}],
        [{"text": "🖨 Лог ядра (dmesg)", "callback_data": "log_dmesg"}],
        [{"text": "🔄 Обновить статус", "callback_data": "status"}, {"text": "♻️ Рестарт Podkop", "callback_data": "restart"}],
        [{"text": "🗑 Закрыть", "callback_data": "close_msg"}, {"text": "◀️ В главное меню", "callback_data": "home"}]
    ]}
    return text, kb

def get_log_screen():
    log_text = "Лог пуст или еще не создан."
    if os.path.exists(LOG_FILE):
        try:
            with open(LOG_FILE, "r", encoding="utf-8", errors="ignore") as f:
                lines = f.readlines()
                log_text = "".join(lines[-15:]).strip()
        except Exception as e:
            log_text = f"Ошибка чтения лога: {e}"

    safe_log = html.escape(log_text[-3000:])
    text = f"📄 <b>Последние строки лога парсера:</b>\n\n<pre>{safe_log}</pre>"
    kb = {
        "inline_keyboard": [
            [{"text": "🔄 Обновить лог", "callback_data": "view_log"}, {"text": "🗑 Закрыть", "callback_data": "close_msg"}],
            [{"text": "◀️ К меню парсера", "callback_data": "parser_menu"}]
        ]
    }
    return text, kb

def get_logread_screen():
    out = "Лог пуст."
    try:
        res = subprocess.run(["logread", "-l", "30"], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
        raw_text = res.stdout if res.stdout else subprocess.run(["logread"], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True).stdout
        lines = raw_text.strip().splitlines()
        out = "\n".join(lines[-25:]) if lines else "Лог пуст."
    except Exception as e:
        out = f"Ошибка чтения logread: {e}"

    safe_out = html.escape(out[-3000:])
    text = f"📜 <b>Системный лог (последние 25 строк):</b>\n\n<pre>{safe_out}</pre>"
    kb = {"inline_keyboard": [
        [{"text": "🔄 Обновить logread", "callback_data": "log_sys"}, {"text": "🗑 Закрыть", "callback_data": "close_msg"}],
        [{"text": "◀️ Назад к статусу", "callback_data": "status"}]
    ]}
    return text, kb

def get_dmesg_screen():
    out = "Лог пуст."
    try:
        res = subprocess.run(["dmesg"], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
        lines = res.stdout.strip().splitlines()
        out = "\n".join(lines[-20:]) if lines else "Лог пуст."
    except Exception as e:
        out = f"Ошибка чтения dmesg: {e}"

    safe_out = html.escape(out[-3000:])
    text = f"🖨 <b>Лог ядра dmesg (последние 20 строк):</b>\n\n<pre>{safe_out}</pre>"
    kb = {"inline_keyboard": [
        [{"text": "🔄 Обновить dmesg", "callback_data": "log_dmesg"}, {"text": "🗑 Закрыть", "callback_data": "close_msg"}],
        [{"text": "◀️ Назад к статусу", "callback_data": "status"}]
    ]}
    return text, kb

def get_reboot_confirm_screen():
    text = "⚠️ <b>Подтверждение перезагрузки роутера</b>\n\nВы действительно хотите перезагрузить устройство?"
    kb = {
        "inline_keyboard": [
            [{"text": "✅ Да, перезагрузить!", "callback_data": "do_reboot"}],
            [{"text": "❌ Отмена", "callback_data": "home"}]
        ]
    }
    return text, kb


def get_fp_menu_screen():
    cur = get_uci_val("override_fp", "firefox").lower().strip()
    fps = [
        ("firefox", "🦊 Firefox (РФ)"),
        ("chrome", "🌐 Chrome"),
        ("safari", "🍏 Safari"),
        ("ios", "📱 iOS"),
        ("edge", "🔷 Edge"),
        ("randomized", "🎲 Randomized"),
        ("as_is", "↩️ Как в подписке")
    ]
    kb = {"inline_keyboard": []}
    row = []
    for val, label in fps:
        mark = " 🔘" if cur == val else ""
        row.append({"text": f"{label}{mark}", "callback_data": f"set_fp:{val}"})
        if len(row) == 2:
            kb["inline_keyboard"].append(row)
            row = []
    if row:
        kb["inline_keyboard"].append(row)
    kb["inline_keyboard"].append([{"text": "◀ Назад в параметры", "callback_data": "parser_menu"}])

    mapping = {
        "as_is": "Как в подписке", "firefox": "Firefox 🦊", "chrome": "Chrome 🌐",
        "safari": "Safari 🍏", "ios": "iOS 📱", "edge": "Edge 🔷", "randomized": "Randomized 🎲"
    }
    cur_label = mapping.get(cur, cur.capitalize())
    text = (
        "🎭 <b>uTLS Fingerprint (эмуляция браузера)</b>\n\n"
        "Глобально заменяет TLS Client Hello для всех узлов во избежание блокировок по сигнатурам браузера.\n\n"
        f"Текущий режим: <b>{cur_label}</b>"
    )
    return text, kb
