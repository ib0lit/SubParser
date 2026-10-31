# -*- coding: utf-8 -*-
import os
import html
import subprocess
import urllib.parse
from concurrent.futures import ThreadPoolExecutor
from .config import (get_ssl_ctx, get_uci_val, get_uci_list, normalize_protocol,
                     UA_CHOICES, PROTOCOL_CHOICES, LOG_FILE)
from .subs import get_subscriptions, resolve_subscription
from .podkop import (
    get_all_podkop_sections, get_podkop_links, get_node_name,
    get_podkop_service_status, get_section_routing_state, CACHED_DELAYS
)
from .system import get_blocked_macs, get_wifi_stations, get_ip_by_mac, check_single_service
from .rich import RichScreen, esc, esc_attr


def _parser_running():
    """Идёт ли сейчас синхронизация SubParser."""
    try:
        res = subprocess.run(["pgrep", "-f", "subparser.py"], stdout=subprocess.PIPE,
                             stderr=subprocess.DEVNULL, text=True)
        return bool(res.stdout.strip())
    except Exception:
        return False


def get_main_screen():
    subs_count = len(get_subscriptions())
    sections = get_all_podkop_sections()
    sections_str = ", ".join(esc(s) for s in sections) if sections else "main"
    svc_ok, pid, ram_mb, mode_str = get_podkop_service_status()
    podkop_badge = "🟢 Запущен" if svc_ok else "🔴 Остановлен"
    parser_badge = "⏳ Выполняется" if _parser_running() else "💤 В ожидании"
    if pid:
        core_badge = f"<code>PID {esc(pid)}</code>"
        if ram_mb:
            core_badge += f" · <code>{esc(ram_mb)}</code>"
    else:
        core_badge = "🔴 Остановлено"

    screen = RichScreen()
    screen.h("🎛 Панель управления SubParser &amp; OpenWrt")
    screen.kv([
        ("📦 Подписки", f"<code>{subs_count}</code>"),
        ("📁 Секции Podkop", f"<code>{sections_str}</code>"),
        ("🛰 Podkop", podkop_badge),
        ("⚙️ Ядро sing-box", core_badge),
        ("🌐 Режим трафика", f"<code>{esc(mode_str or 'н/д')}</code>"),
        ("🔄 Синхронизация", parser_badge),
    ])
    screen.p("Выберите необходимое действие:")
    screen.button_row([
        {"type": "callback_data", "text": "⚙️ Меню парсера", "data": "parser_menu"},
        {"type": "callback_data", "text": "📋 Серверы и пинг", "data": "servers"},
    ])
    screen.button_row([
        {"type": "callback_data", "text": "🔄 Обновления", "data": "check_update"},
        {"type": "callback_data", "text": "📊 Статус", "data": "status"},
    ])
    screen.button_row([
        {"type": "callback_data", "text": "📟 Ресурсы", "data": "sysinfo"},
        {"type": "callback_data", "text": "👥 Клиенты", "data": "clients"},
    ])
    screen.button_row([
        {"type": "callback_data", "text": "🛡 Сервисы", "data": "check_services"},
        {"type": "callback_data", "text": "📜 Лог парсера", "data": "view_log"},
    ])
    screen.button_row([
        {"type": "callback_data", "text": "♻️ Рестарт Podkop", "data": "restart"},
        {"type": "callback_data", "text": "⚠️ Reboot", "data": "reboot", "style": "danger"},
    ])
    text = screen.build()
    kb = {"inline_keyboard": [
        [{"text": "⚙️ Меню парсера", "callback_data": "parser_menu"}, {"text": "📋 Серверы и пинг", "callback_data": "servers"}],
        [{"text": "🔄 Обновления", "callback_data": "check_update"}, {"text": "📊 Статус", "callback_data": "status"}],
        [{"text": "📟 Ресурсы", "callback_data": "sysinfo"}, {"text": "👥 Клиенты", "callback_data": "clients"}],
        [{"text": "🛡 Сервисы", "callback_data": "check_services"}, {"text": "📜 Лог парсера", "callback_data": "view_log"}],
        [{"text": "♻️ Рестарт Podkop", "callback_data": "restart"}, {"text": "⚠️ Reboot", "callback_data": "reboot"}]
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

    fp_val = get_uci_val("override_fp", "as_is").lower().strip()
    fp_map = {
        "as_is": "Как в подписке",
        "firefox": "Firefox 🦊",
        "chrome": "Chrome 🌐",
        "safari": "Safari 🍏",
        "ios": "iOS 📱",
        "edge": "Edge 🔷",
        "qq": "QQ Browser 🐧",
        "random": "Random 🎲",
        "randomized": "Randomized 🎲"
    }
    fp_badge = fp_map.get(fp_val, fp_val.capitalize())

    excluded_protocols = [normalize_protocol(p) for p in get_uci_list("exclude_protocols") if p]
    proto_badge = ("Нет" if not excluded_protocols
                   else ", ".join(PROTOCOL_CHOICES.get(p, p) for p in excluded_protocols))

    if per_sec:
        sec_parts = []
        for s in all_secs:
            s_en = get_uci_val(f"sec_en_{s}", "1") == "1"
            if s_en:
                sec_parts.append(html.escape(s))
        sections_str = ", ".join(sec_parts) if sec_parts else "Все выключены"
    else:
        raw_targets = get_uci_val("target_section", "main").split()
        existing_valid = [s for s in raw_targets if s in all_secs]
        for s in all_secs:
            if s not in existing_valid:
                existing_valid.append(s)
        target_list = existing_valid if existing_valid else ["main"]
        sections_str = ", ".join(html.escape(s) for s in target_list)

    screen = RichScreen()
    screen.h("⚙️ Параметры парсера SubParser")
    screen.kv([
        ("📋 Подписок", f"<code>{subs_count} шт.</code>"),
        ("🎯 Секции Podkop", f"<code>{sections_str}</code>"),
        ("⚡️ Порог пинга", f"<code>{esc(threshold)} ms</code>"),
        ("🔝 Лимит узлов", f"<code>{esc(limit_badge)}</code>"),
        ("⏰ Расписание", f"<code>{esc(cron_badge)}</code>"),
        ("🇷🇺 Фильтр RU", f"<code>{esc(ru_badge)}</code>"),
        ("🎭 uTLS Fingerprint", f"<code>{esc(fp_badge)}</code>"),
        ("🚫 Исключённые протоколы", f"<code>{esc(proto_badge)}</code>"),
        ("🔄 Состояние", "⏳ парсер выполняется" if _parser_running() else "💤 готов к запуску"),
    ])
    if prompt_custom_ping:
        screen.quote("✏️ <b>Введите желаемый пинг (например, 280) "
                     "в ответном сообщении:</b>")
    else:
        screen.p("Настройте параметры и запустите синхронизацию:")

    screen.button_row([
        {"type": "callback_data", "text": f"📋 Подписки ({subs_count})", "data": "subs_list"},
        {"type": "callback_data", "text": "🎯 Секции Podkop", "data": "bot_sections_menu"},
    ])
    screen.button_row([
        {"type": "callback_data", "text": "150ms" + (" 🔘" if threshold == "150" else ""), "data": "th:150"},
        {"type": "callback_data", "text": "350ms" + (" 🔘" if threshold == "350" else ""), "data": "th:350"},
        {"type": "callback_data", "text": "500ms" + (" 🔘" if threshold == "500" else ""), "data": "th:500"},
    ])
    screen.button_row([
        {"type": "callback_data", "text": "✏️ Задать свой", "data": "ask_custom_th"},
        {"type": "callback_data", "text": f"Лимит: {limit_badge}", "data": "cycle_limit"},
    ])
    screen.button_row([
        {"type": "callback_data", "text": f"Расписание: {cron_badge}", "data": "cycle_cron"},
        {"type": "callback_data", "text": f"RU: {'Фильтр' if filter_ru == '1' else 'Все'}", "data": "toggle_ru"},
    ])
    screen.button_row([
        {"type": "callback_data", "text": f"🎭 Фингерпринт: {fp_badge}", "data": "menu_fp"},
    ])
    screen.button_row([
        {"type": "callback_data", "text": f"🚫 Протоколы: {proto_badge}", "data": "menu_proto"},
    ])
    screen.button_row([
        {"type": "callback_data", "text": "🚀 Запустить парсер сейчас", "data": "run_parser", "style": "success"},
    ])
    screen.button_row([
        {"type": "callback_data", "text": "📜 Лог", "data": "view_log"},
        {"type": "callback_data", "text": "◀ Главное меню", "data": "home"},
    ])
    text = screen.build()

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
                {"text": f"🚫 Протоколы: {proto_badge}", "callback_data": "menu_proto"}
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


def get_protocols_menu_screen():
    excluded = {normalize_protocol(p) for p in get_uci_list("exclude_protocols") if p}
    kb = {"inline_keyboard": []}
    row = []
    for proto, label in PROTOCOL_CHOICES.items():
        mark = "🚫" if proto in excluded else "✅"
        row.append({"text": f"{mark} {label}", "callback_data": f"tgl_proto:{proto}"})
        if len(row) == 2:
            kb["inline_keyboard"].append(row)
            row = []
    if row:
        kb["inline_keyboard"].append(row)
    if excluded:
        kb["inline_keyboard"].append([
            {"text": "🧹 Сбросить фильтр", "callback_data": "reset_proto"}
        ])
    kb["inline_keyboard"].append([{"text": "◀ Назад в параметры", "callback_data": "parser_menu"}])

    rows = []
    fallback_rows = []
    for proto, label in PROTOCOL_CHOICES.items():
        is_excluded = proto in excluded
        mark = "🚫 <b>Исключён</b>" if is_excluded else "✅ <b>Включён</b>"
        rows.append((esc(label), mark))
        fallback_rows.append(f"• <b>{esc(label)}:</b> {'исключён' if is_excluded else 'включён'}")
    screen = RichScreen()
    screen.h("🚫 Фильтр протоколов")
    screen.p("Отмеченные протоколы исключаются сразу после загрузки подписки — "
             "они не попадают ни в дедупликацию, ни на первый этап скрининга.")
    screen.kv([("Исключено сейчас", f"<b>{len(excluded)}</b>")])
    screen.table(["Протокол", "Статус"], rows, fallback=fallback_rows)
    if not excluded:
        screen.quote("Фильтр пуст: обрабатываются все протоколы.")
    text = screen.build()
    return text, kb


def get_fp_menu_screen():
    cur = get_uci_val("override_fp", "as_is").lower().strip()
    fps = [
        ("firefox", "🦊 Firefox (РФ)"),
        ("chrome", "🌐 Chrome"),
        ("safari", "🍏 Safari"),
        ("ios", "📱 iOS"),
        ("edge", "🔷 Edge"),
        ("qq", "🐧 QQ"),
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
        "safari": "Safari 🍏", "ios": "iOS 📱", "edge": "Edge 🔷",
        "qq": "QQ Browser 🐧", "randomized": "Randomized 🎲"
    }
    cur_label = mapping.get(cur, cur.capitalize())
    rows = []
    for val, label in fps:
        mark = "🔘 <b>Текущий</b>" if cur == val else "—"
        rows.append((esc(label.replace(" 🔘", "")), mark))
    screen = RichScreen()
    screen.h("🎭 uTLS Fingerprint (эмуляция браузера)")
    screen.p("Глобально заменяет TLS Client Hello для всех узлов во избежание "
             "блокировок по сигнатурам браузера.")
    screen.kv([("Текущий режим", f"<b>{esc(cur_label)}</b>")])
    screen.table(["Режим", "Выбран"], rows)
    text = screen.build()
    return text, kb

def get_subs_list_screen():
    subs = get_subscriptions()
    if not subs:
        screen = RichScreen()
        screen.h("📋 Источники подписок")
        screen.quote("Активные подписки не найдены.")
        screen.p("Отправьте ссылку на подписку прямо в чат для её добавления.")
        kb = {"inline_keyboard": [
            [{"text": "◀️ В меню парсера", "callback_data": "parser_menu"}]
        ]}
        return screen.build(), kb

    rows = []
    fallback_rows = []
    keyboard = []
    for s in subs:
        i = s["idx"]
        sec_id = s["sec_name"]
        en_ico = "🟢" if s["enabled"] == "1" else "🔴"
        ua_short = esc(UA_CHOICES.get(s["user_agent"], s["user_agent"]))
        name_safe = esc(s["name"])
        rows.append((f"<b>{i+1}</b>", en_ico, name_safe, f"<code>{ua_short}</code>"))
        fallback_rows.append(f"{en_ico} <b>{name_safe}</b> — <code>{ua_short}</code>")
        keyboard.append([{"text": f"⚙️ #{i+1} {s['name'][:20]}", "callback_data": f"sub_card:{sec_id}"}])

    screen = RichScreen()
    screen.h(f"📋 Источники подписок ({len(subs)} шт.)")
    screen.table(["№", "Состояние", "Подписка", "Клиент"], rows,
                 fallback=fallback_rows)
    screen.p("Выберите подписку для настройки или удаления:")
    text = screen.build()
    keyboard.append([{"text": "🔄 Обновить", "callback_data": "subs_list"}, {"text": "◀️ В меню парсера", "callback_data": "parser_menu"}])
    return text, {"inline_keyboard": keyboard}

def get_sub_card_screen(sub_idx):
    target = resolve_subscription(sub_idx)
    if not target:
        return get_subs_list_screen()
    sec_id = target["sec_name"]

    is_en = target["enabled"] == "1"
    st_badge = "🟢 <b>Активна (участвует в парсинге)</b>" if is_en else "🔴 <b>Отключена (пропускается)</b>"
    btn_en_txt = "⏸ Выключить подписку" if is_en else "▶️ Включить подписку"
    if target.get("user_agent") == "custom":
        dev_m = target.get("device_model") or "Кастомное устройство"
        hw_cut = (target.get("device_id")[:8] + "...") if target.get("device_id") else "дефолт"
        ua_title = f"🛠 {dev_m} (HWID: {hw_cut})"
    else:
        ua_title = UA_CHOICES.get(target["user_agent"], target["user_agent"])

    if target["url"]:
        parsed = urllib.parse.urlsplit(target["url"])
        domain = parsed.netloc or "н/д"
        url_preview = esc(target["url"][:48]) + ("..." if len(target["url"]) > 48 else "")
    else:
        domain = "не задан"
        url_preview = "⚠️ <i>URL не указан в настройках</i>"

    screen = RichScreen()
    screen.h(f"📋 Управление подпиской #{target['idx']+1}")
    screen.kv([
        ("🏷 Название", f"<code>{esc(target['name'])}</code>"),
        ("🌐 Сервер", f"<code>{esc(domain)}</code>"),
        ("👤 Клиент (UA)", f"<code>{esc(ua_title)}</code>"),
        ("📡 Статус", st_badge),
        ("🔗 URL", f"<code>{url_preview}</code>"),
    ])
    if target["url"]:
        screen.button_row([
            {"type": "copy_text", "text": "📋 Копировать URL подписки",
             "text_to_copy": target["url"], "style": "primary"},
        ], align="left")
    text = screen.build()
    kb = {"inline_keyboard": [
        [{"text": btn_en_txt, "callback_data": f"sub_tgl:{sec_id}"}],
        [{"text": f"👤 Сменить UA: {target['user_agent']} ➔", "callback_data": f"sub_cycle_ua:{sec_id}"}],
        [{"text": "🗑 Удалить подписку", "callback_data": f"sub_del_ask:{sec_id}"}],
        [{"text": "◀️ Назад к списку подписок", "callback_data": "subs_list"}]
    ]}
    return text, kb

def get_sub_delete_confirm_screen(sub_idx):
    target = resolve_subscription(sub_idx)
    sec_id = target["sec_name"] if target else str(sub_idx)
    name = esc(target["name"]) if target else f"#{sub_idx}"
    screen = RichScreen()
    screen.h("⚠️ Удаление подписки")
    screen.quote(f"Вы действительно хотите навсегда удалить подписку "
                 f"<b>«{name}»</b> из роутера?")
    if target:
        host = urllib.parse.urlsplit(target["url"]).netloc if target["url"] else ""
        enabled = "🟢 активна" if target["enabled"] == "1" else "🔴 отключена"
        screen.kv([
            ("🏷 Подписка", f"<b>{name}</b>"),
            ("🌐 Сервер", f"<code>{esc(host or 'не задан')}</code>"),
            ("📡 Состояние", enabled),
        ])
    screen.p("Действие необратимо: ссылка и настройки подписки будут удалены.")
    text = screen.build()
    kb = {"inline_keyboard": [
        [{"text": "❌ Да, удалить навсегда!", "callback_data": f"sub_del_do:{sec_id}"}],
        [{"text": "↩️ Отмена", "callback_data": f"sub_card:{sec_id}"}]
    ]}
    return text, kb

def get_sections_menu_screen():
    per_sec = get_uci_val("per_section_config", "0") == "1"
    all_secs = get_all_podkop_sections()
    keyboard = []
    screen = RichScreen()
    screen.h("📁 Настройка секций Podkop")

    if not per_sec:
        raw_targets = get_uci_val("target_section", "main").split()
        target_set = set(raw_targets) if raw_targets else set(all_secs)
        for s in all_secs:
            if s not in target_set:
                target_set.add(s)

        g_mode = get_uci_val("update_mode", "replace")
        mode_label = "🔄 Замена серверов" if g_mode == "replace" else "➕ Добавление (без дублей)"

        screen.kv([
            ("🌐 Общий режим", "включён"),
            ("Текущее действие", f"<b>{mode_label}</b>"),
            ("Индивидуальный режим", "❌ выключен"),
        ])
        screen.p("Все выбранные секции обновляются по единому правилу.")
        keyboard.append([{"text": "Индивидуальный режим: ❌ ВЫКЛ", "callback_data": "toggle_per_sec"}])
        keyboard.append([{"text": f"Режим: {mode_label}", "callback_data": "toggle_glob_mode"}])

        sec_rows = []
        fallback_rows = []
        sec_buttons = []
        for s in all_secs:
            is_active = s in target_set
            icon = "✅" if is_active else "⬜"
            sec_rows.append((esc(s), "✅ обновляется" if is_active else "⬜ пропускается"))
            fallback_rows.append(f"{icon} <b>{esc(s)}</b>")
            sec_buttons.append({"text": f"{icon} {s}", "callback_data": f"tgl_sec:{s}"})
            if len(sec_buttons) == 2:
                keyboard.append(sec_buttons)
                sec_buttons = []
        if sec_buttons:
            keyboard.append(sec_buttons)
        screen.table(["Секция", "Участие"], sec_rows, fallback=fallback_rows)
    else:
        screen.kv([
            ("🎯 Индивидуальный режим", "✅ включён"),
            ("Общий режим", "не используется"),
        ])
        screen.p("Нажмите на секцию, чтобы изменить её персональный режим.")
        keyboard.append([{"text": "Индивидуальный режим: ✅ ВКЛ", "callback_data": "toggle_per_sec"}])

        sec_rows = []
        fallback_rows = []
        for s in all_secs:
            s_en = get_uci_val(f"sec_en_{s}", "1") == "1"
            s_mode = get_uci_val(f"sec_mode_{s}", "replace")
            if not s_en:
                badge, short = "❌ Отключена", "отключена"
            elif s_mode == "replace":
                badge, short = "🔄 Замена", "замена"
            else:
                badge, short = "➕ Добавление", "добавление"
            sec_rows.append((esc(s), badge))
            fallback_rows.append(f"• <b>{esc(s)}:</b> {short}")
            keyboard.append([{"text": f"{s}: {badge}", "callback_data": f"cycle_sec:{s}"}])
        screen.table(["Секция", "Режим"], sec_rows, fallback=fallback_rows)

    keyboard.append([{"text": "◀️ Назад в меню парсера", "callback_data": "parser_menu"}])
    return screen.build(), {"inline_keyboard": keyboard}

def get_servers_screen(delays_map=None, section="main", page=0, delete_mode=False, selected_indices=None):
    if selected_indices is None:
        selected_indices = []
    sel_set = set(selected_indices)
    links = get_podkop_links(section)
    all_secs = get_all_podkop_sections()
    back_btn = {"text": "◀️ К секциям", "callback_data": "servers"} if len(all_secs) > 1 else {"text": "◀️ В меню", "callback_data": "home"}
    if not links:
        screen = RichScreen()
        screen.h(f"🌐 Секция {esc(section)}")
        screen.quote("В этой секции нет серверов.")
        return screen.build(), {"inline_keyboard": [[back_btn]]}
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
    node_rows = []
    fallback_nodes = []
    for i, link in enumerate(page_links, start=start_idx):
        tag_cur = f"{section}-{i+1}-out"
        is_active = (not is_auto and tag_cur == active_manual_tag)
        name = get_node_name(link, i)
        proto = urllib.parse.urlsplit(link).scheme.upper() or "?"
        d = delays_map.get(i, -1) if delays_map else -1
        if d > 0:
            ico = "🟢" if d < 180 else ("🟡" if d < 350 else "🔴")
            prefix = f"{ico} [{d}ms]"
            ping_cell = f"{ico} {d} ms"
            state_cell = "🔘 активен" if is_active else "✅ жив"
        elif delays_map and i in delays_map:
            prefix = "❌ [DEAD]"
            ping_cell = "❌"
            state_cell = "❌ DEAD"
        else:
            prefix = f"#{i+1}"
            ping_cell = "—"
            state_cell = "🔘 активен" if is_active else "—"
        first_cell = ("☑️" if i in sel_set else "◻️") if delete_mode else str(i + 1)
        short_name = esc(name[:32]) + ("…" if len(name) > 32 else "")
        node_rows.append((first_cell, short_name, esc(proto), ping_cell, state_cell))
        fallback_nodes.append(f"{first_cell} {prefix} <b>{short_name}</b> · {esc(proto)}")
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

    alive = [d for d in delays_map.values() if d > 0] if delays_map else []
    dead = [d for d in delays_map.values() if d <= 0] if delays_map else []
    avg_ping = f"{int(sum(alive) / len(alive))} ms" if alive else "—"

    screen = RichScreen()
    screen.h(f"🌐 Узлы Podkop [{esc(section)}]")
    if delete_mode:
        screen.quote("Режим мультиудаления: отметьте узлы, затем подтвердите удаление.")
    screen.kv([
        ("📦 Всего узлов", f"<code>{total_nodes}</code>"),
        ("📄 Страница", f"<code>{page+1}/{total_pages}</code>"),
        ("✅ Живых", f"<code>{len(alive)}</code>"),
        ("❌ DEAD", f"<code>{len(dead)}</code>"),
        ("⚡ Средний пинг", f"<code>{avg_ping}</code>"),
        ("🧭 Режим", "🤖 авто (URL-Test)" if is_auto else "⚡ ручной выбор"),
    ])
    screen.table(["№", "Узел", "Протокол", "Пинг", "Статус"], node_rows,
                 fallback=fallback_nodes)
    screen.p("🟢 &lt; 180 ms · 🟡 &lt; 350 ms · 🔴 ≥ 350 ms")
    if delete_mode:
        screen.p(f"Отмечено для удаления: <b>{len(sel_set)}</b>")
    else:
        screen.p("Нажмите на узел в списке ниже для управления:")

    if delete_mode:
        cnt_sel = len(sel_set)
        keyboard.append([{"text": f"🗑 Подтвердить удаление ({cnt_sel})", "callback_data": f"apply_bdel:{section}:{page}"}])
        keyboard.append([{"text": "❌ Выбрать все [DEAD]", "callback_data": f"sel_dead:{section}:{page}"}])
        keyboard.append([{"text": "◀️ Отмена (выход)", "callback_data": f"exit_del_mode:{section}:{page}"}])
    else:
        auto_label = "✅ Режим: Авто (URL-Test)" if is_auto else "🔄 Включить Авто (URL-Test)"
        keyboard.append([{"text": auto_label, "callback_data": f"auto_mode:{section}:{page}"}])
        keyboard.append([
            {"text": "⚡️ Пинг", "callback_data": f"ping_sec:{section}:{page}"},
            {"text": "🗑 Мультиудаление", "callback_data": f"enter_del_mode:{section}:{page}"}
        ])
        keyboard.append([back_btn])
    return screen.build(), {"inline_keyboard": keyboard}

def get_sections_selector_screen():
    all_secs = get_all_podkop_sections()
    keyboard = []
    sec_rows = []
    fallback_rows = []
    for s in all_secs:
        count = len(get_podkop_links(s))
        sec_rows.append((esc(s), f"<code>{count}</code>"))
        fallback_rows.append(f"• <b>{esc(s)}</b>: <code>{count} узлов</code>")
        keyboard.append([{"text": f"📁 Секция: {s} ({count} шт.)", "callback_data": f"sec_view:{s}"}])

    keyboard.append([{"text": "🧹 Удалить DEAD из всех секций", "callback_data": "del_dead_all"}])
    keyboard.append([{"text": "◀️ В главное меню", "callback_data": "home"}])
    screen = RichScreen()
    screen.h("📋 Серверы Podkop: выбор секции")
    screen.table(["Секция", "Узлов"], sec_rows, fallback=fallback_rows)
    screen.p("Выберите секцию для просмотра серверов:")
    return screen.build(), {"inline_keyboard": keyboard}


def get_del_dead_all_confirm_screen(details, total_dead):
    rows = []
    fallback_rows = []
    for item in details or []:
        sec = esc(str(item.get("section", "?")))
        status = item.get("status")
        total = int(item.get("total", 0))
        dead = int(item.get("dead", 0))
        skipped = int(item.get("skipped", 0))
        if status == "empty":
            state = "нет узлов"
            note = "—"
            plain = f"• <b>{sec}</b>: нет узлов"
        elif status == "no_data":
            state = "нет данных Podkop"
            note = "пропущено"
            plain = f"• <b>{sec}</b>: пропущено (нет данных Podkop)"
        elif status == "dead":
            state = f"🔴 мёртвых <b>{dead}</b>"
            note = "будет очищена"
            plain = f"• <b>{sec}</b>: мёртвых <b>{dead}</b> из {total}"
        elif status == "all_dead":
            state = f"❌ все <b>{total}</b> мёртвые"
            note = "не изменяется"
            plain = (f"• <b>{sec}</b>: все <b>{total}</b> мёртвые — "
                     "секция не изменяется")
        else:
            state = "✅ мёртвых нет"
            note = "—"
            plain = f"• <b>{sec}</b>: мёртвых нет ({total} шт.)"
        if skipped and status != "no_data":
            plain += f"\n   <i>не проверено: {skipped}</i>"
            note += f" · не проверено {skipped}"
        rows.append((f"<b>{sec}</b>", str(total), state, note))
        fallback_rows.append(plain)

    screen = RichScreen()
    has_all_dead = any(item.get("status") == "all_dead" for item in details or [])
    if total_dead:
        screen.h("🧹 Проверка завершена")
    elif has_all_dead:
        screen.h("⚠️ Удалять нечего")
    else:
        screen.h("✅ Мёртвых узлов не найдено")
    screen.table(["Секция", "Всего", "Мёртвых", "Действие"], rows,
                 fallback=fallback_rows)

    if total_dead:
        screen.kv([
            ("🗑 Будет удалено", f"<b>{total_dead}</b>"),
            ("♻️ Podkop", "перезапустится <b>один раз</b>"),
        ])
        screen.p("Удалить выбранные мёртвые узлы?")
        text = screen.build()
        kb = {"inline_keyboard": [
            [{"text": f"✅ Удалить {total_dead}", "callback_data": "del_dead_all_do"}],
            [{"text": "❌ Отмена", "callback_data": "servers"}],
        ]}
    elif has_all_dead:
        screen.quote("Секции без живых узлов оставлены без изменений, "
                     "чтобы Podkop мог запуститься. Разберитесь с ними вручную.")
        text = screen.build()
        kb = {"inline_keyboard": [
            [{"text": "◀️ К секциям", "callback_data": "servers"}],
            [{"text": "🏠 В главное меню", "callback_data": "home"}],
        ]}
    else:
        screen.quote("Podkop не изменялся.")
        text = screen.build()
        kb = {"inline_keyboard": [
            [{"text": "◀️ К секциям", "callback_data": "servers"}],
            [{"text": "🏠 В главное меню", "callback_data": "home"}],
        ]}
    return text, kb

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
    ram_pct = None
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
        ram_pct = pct
        filled = pct // 10
        ram_bar = f"[{'█' * filled}{'░' * (10 - filled)}] {pct}%"
        ram_str = f"{used} MB / {total} MB"
    except Exception:
        pass

    screen = RichScreen()
    screen.h("📟 Системные ресурсы роутера")
    screen.kv([
        ("⏱ Время работы", f"<code>{esc(uptime_str)}</code>"),
        ("🌡 Температура SoC", f"<code>{esc(temp_str)}</code>"),
        ("📈 Load Average", f"<code>{esc(load_str)}</code>"),
        ("🧠 Память RAM", f"<code>{esc(ram_str)}</code>"),
    ])
    if ram_pct is not None:
        screen.p(f"Загрузка RAM: {screen.bar(ram_pct)}")
    text = screen.build()
    kb = {
        "inline_keyboard": [
            [{"text": "🔄 Обновить показатели", "callback_data": "sysinfo"}, {"text": "🗑 Закрыть", "callback_data": "close_msg"}],
            [{"text": "◀️ В главное меню", "callback_data": "home"}]
        ]
    }
    return text, kb

def get_clients_screen(page=0):
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
        screen = RichScreen()
        screen.h("👥 Подключенные устройства")
        screen.quote("Активные клиенты не обнаружены.")
        kb = {"inline_keyboard": [
            [{"text": "🗑 Закрыть", "callback_data": "close_msg"}, {"text": "◀️ В главное меню", "callback_data": "home"}]
        ]}
        return screen.build(), kb
    else:
        wifi_info = get_wifi_stations()
        blocked = get_blocked_macs()

        def ip_sort_key(ip_str):
            try:
                return [int(p) for p in ip_str.split(".")]
            except Exception:
                return [0, 0, 0, 0]

        sorted_ips = sorted(clients.keys(), key=ip_sort_key)
        page_size = 8
        total_pages = max(1, (len(sorted_ips) + page_size - 1) // page_size)
        page = max(0, min(int(page), total_pages - 1))
        page_ips = sorted_ips[page * page_size:(page + 1) * page_size]
        keyboard = []
        rows = []
        fallback_rows = []

        for i, ip in enumerate(page_ips, start=page * page_size + 1):
            c = clients[ip]
            mac = c["mac"]
            is_bl = mac in blocked
            bl_icon = "🚫 " if is_bl else ""

            if mac in wifi_info:
                w = wifi_info[mac]
                tx_part = f" (<code>{esc(w['tx'])}</code>)" if w.get("tx") else ""
                conn_badge = (f"🛜 <b>{esc(w['type'])}</b> | "
                              f"{esc(w['signal'])}{tx_part}")
            else:
                conn_badge = "🔌 <b>LAN</b>"

            access = "🚫 заблокирован" if is_bl else "🟢 открыт"
            rows.append((
                str(i),
                f"{bl_icon}<b>{esc(c['name'])}</b>",
                f"<code>{esc(ip)}</code>",
                conn_badge,
                access,
            ))
            fallback_rows.append(
                f"{i}. {bl_icon}<b>{esc(c['name'])}</b>\n"
                f"   IP: <code>{esc(ip)}</code> · MAC: <code>{esc(mac)}</code>\n"
                f"   Связь: {conn_badge} · Доступ: {access}"
            )
            keyboard.append([{"text": f"⚙️ {bl_icon}{c['name'][:18]} ({ip})", "callback_data": f"dev:{mac}"}])

        screen = RichScreen()
        screen.h(f"👥 Устройства в сети ({len(clients)} шт.)")
        blocked_count = len(blocked)
        screen.kv([
            ("📄 Страница", f"<code>{page+1}/{total_pages}</code>"),
            ("🚫 Заблокировано", f"<code>{blocked_count}</code>"),
        ])
        screen.table(["№", "Устройство", "IP", "Связь", "Доступ"], rows,
                     fallback=fallback_rows)
        screen.p("Выберите устройство для управления доступом:")
        text = screen.build()
        nav = []
        if page > 0:
            nav.append({"text": "⬅️", "callback_data": f"clients_pg:{page-1}"})
        nav.append({"text": f"📄 {page+1}/{total_pages}", "callback_data": "noop"})
        if page < total_pages - 1:
            nav.append({"text": "➡️", "callback_data": f"clients_pg:{page+1}"})
        keyboard.append(nav)
        keyboard.append([{"text": "🔄 Обновить список", "callback_data": f"clients_pg:{page}"}, {"text": "🗑 Закрыть", "callback_data": "close_msg"}])
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

    screen = RichScreen()
    screen.h(f"📱 Управление клиентом: {esc(hname)}")
    screen.kv([
        ("🌐 IP адрес", f"<code>{esc(str(ip))}</code>"),
        ("🔗 MAC адрес", f"<code>{esc(mac_u)}</code>"),
        ("📡 Статус", status_badge),
    ])
    if is_blocked:
        screen.quote("Устройство не имеет доступа в интернет через этот роутер.")
    else:
        screen.quote("Устройство имеет доступ в интернет через этот роутер.")
    text = screen.build()
    kb = {
        "inline_keyboard": [
            [{"text": btn_text, "callback_data": action}],
            [{"text": "◀️ Назад к списку устройств", "callback_data": "clients_pg:0"}]
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

    rows = []
    fallback_rows = []
    for name, is_proxied, ok, ms in results:
        route_badge = "🛡 Прокси" if is_proxied else "🇷🇺 Прямой"
        if ok and ms > 0:
            status_badge = f"🟢 <code>{ms} ms</code>"
        else:
            status_badge = "🔴 <b>Блок / Таймаут</b>"
        rows.append((f"<b>{esc(name)}</b>", route_badge, status_badge))
        fallback_rows.append(f"• <b>{esc(name)}</b>: {status_badge} | {route_badge}")

    screen = RichScreen()
    screen.h("🧭 Маршрутизация и доступность сервисов")
    screen.table(["Сервис", "Маршрут", "Ответ"], rows, fallback=fallback_rows)
    screen.details(
        "Как читать результат",
        "<ul>"
        "<li>🛡 <b>Прокси</b> — трафик идёт через туннель Podkop (Fake-IP);</li>"
        "<li>🇷🇺 <b>Прямой</b> — без VPN через провайдера РФ;</li>"
        "<li>🔴 <b>Блок / Таймаут</b> — сервис не ответил за отведённое время.</li>"
        "</ul>",
        "🛡 Прокси — через туннель Podkop (Fake-IP)\n"
        "🇷🇺 Прямой — без VPN через провайдера РФ\n"
        "🔴 Блок / Таймаут — сервис не ответил",
    )
    text = screen.build()
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
    is_sync = "⏳ Выполняется" if _parser_running() else "💤 В ожидании"
    core_badge = f"🟢 <code>PID {esc(pid)}</code> · RAM <code>{esc(ram_mb)}</code>" if pid else "🔴 Остановлен"

    screen = RichScreen()
    screen.h("📊 Статус системы и Podkop")
    screen.kv([
        ("🛰 Служба Podkop", st_badge),
        ("⚙️ Ядро sing-box", core_badge),
        ("🌐 Режим трафика", f"<code>{esc(mode_str)}</code>"),
        ("📁 Серверов в секции main", f"<code>{len(links)} шт.</code>"),
        ("🔄 Синхронизация SubParser", is_sync),
    ])
    screen.p("Подробные логи и замер скорости — кнопками ниже.")
    text = screen.build()
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
                log_text = "".join(lines[-25:]).strip()
        except Exception as e:
            log_text = f"Ошибка чтения лога: {e}"

    safe_log = esc(log_text[-3000:])
    line_count = len(log_text.strip().splitlines()) if log_text.strip() else 0
    screen = RichScreen()
    screen.h("📄 Лог парсера")
    screen.p(f"Последние строки лога синхронизации: <code>{line_count}</code>")
    screen.details(f"Показать лог ({line_count} строк)", f"<pre>{safe_log}</pre>",
                   f"<pre>{safe_log}</pre>", open=True)
    screen.button_row([
        {"type": "copy_text", "text": "📋 Копировать лог", "text_to_copy": log_text[-3000:],
         "style": "primary"},
    ], align="left")
    text = screen.build()
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

    safe_out = esc(out[-3000:])
    line_count = len(out.strip().splitlines()) if out.strip() else 0
    screen = RichScreen()
    screen.h("📜 Системный лог")
    screen.p(f"logread, последние строки: <code>{line_count}</code>")
    screen.details(f"Показать лог ({line_count} строк)", f"<pre>{safe_out}</pre>",
                   f"<pre>{safe_out}</pre>", open=True)
    screen.button_row([
        {"type": "copy_text", "text": "📋 Копировать logread", "text_to_copy": out[-3000:],
         "style": "primary"},
    ], align="left")
    text = screen.build()
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

    safe_out = esc(out[-3000:])
    line_count = len(out.strip().splitlines()) if out.strip() else 0
    screen = RichScreen()
    screen.h("🖨 Лог ядра")
    screen.p(f"dmesg, последние строки: <code>{line_count}</code>")
    screen.details(f"Показать лог ({line_count} строк)", f"<pre>{safe_out}</pre>",
                   f"<pre>{safe_out}</pre>", open=True)
    screen.button_row([
        {"type": "copy_text", "text": "📋 Копировать dmesg", "text_to_copy": out[-3000:],
         "style": "primary"},
    ], align="left")
    text = screen.build()
    kb = {"inline_keyboard": [
        [{"text": "🔄 Обновить dmesg", "callback_data": "log_dmesg"}, {"text": "🗑 Закрыть", "callback_data": "close_msg"}],
        [{"text": "◀️ Назад к статусу", "callback_data": "status"}]
    ]}
    return text, kb

def get_reboot_confirm_screen():
    screen = RichScreen()
    screen.h("⚠️ Перезагрузка роутера")
    screen.quote("Вы действительно хотите перезагрузить устройство? Все соединения будут прерваны на 1–2 минуты.")
    screen.button_row([
        {"type": "callback_data", "text": "✅ Да, перезагрузить!", "data": "do_reboot", "style": "danger"},
        {"type": "callback_data", "text": "❌ Отмена", "data": "home"},
    ])
    text = screen.build()
    kb = {
        "inline_keyboard": [
            [{"text": "✅ Да, перезагрузить!", "callback_data": "do_reboot"}],
            [{"text": "❌ Отмена", "callback_data": "home"}]
        ]
    }
    return text, kb


def get_update_screen():
    import urllib.request, json
    local_sha = ""
    local_ver = "unknown"
    try:
        with open('/etc/subparser_commit', 'r') as f:
            local_sha = f.read().strip()
    except Exception:
        pass

    try:
        with open('/etc/subparser_version', 'r') as f:
            local_ver = f.read().strip()
    except Exception:
        pass

    local_display_ver = local_ver

    remote_sha = ""
    remote_ver = local_ver
    changelog = "Описание недоступно"
    branch = get_uci_val("update_branch", "main") or "main"
    branch = "".join(ch for ch in branch if ch.isalnum() or ch in "-_./") or "main"

    try:
        req = urllib.request.Request(
            f"https://api.github.com/repos/ib0lit/SubParser/commits/{branch}",
            headers={'User-Agent': 'OpenWrt-SubParser-Bot'})
        with urllib.request.urlopen(req, timeout=5, context=get_ssl_ctx()) as resp:
            data = json.loads(resp.read().decode('utf-8'))
            remote_sha = data.get('sha', '').strip()
            changelog = html.escape(data.get('commit', {}).get('message', ''))
    except Exception:
        # Fallback при исчерпании лимита API GitHub (403 rate limit) или сбое:
        # получаем последний коммит через публичный Atom-feed без ограничения по rate limit
        try:
            import re
            atom_url = f"https://github.com/ib0lit/SubParser/commits/{branch}.atom"
            req_atom = urllib.request.Request(atom_url, headers={'User-Agent': 'OpenWrt-SubParser-Bot'})
            with urllib.request.urlopen(req_atom, timeout=5, context=get_ssl_ctx()) as resp_atom:
                atom_text = resp_atom.read().decode('utf-8', errors='ignore')
                entry_match = re.search(r'<entry>(.*?)</entry>', atom_text, re.DOTALL)
                block = entry_match.group(1) if entry_match else atom_text
                sha_match = re.search(r'Commit/([0-9a-fA-F]{40})', block)
                if sha_match:
                    remote_sha = sha_match.group(1).strip()
                title_match = re.search(r'<title>\s*(.*?)\s*</title>', block, re.DOTALL)
                if title_match:
                    changelog = html.escape(re.sub(r'\s+', ' ', title_match.group(1).strip()))
        except Exception:
            pass

    if not remote_sha:
        # Если ни API, ни Atom не ответили, пробуем хотя бы raw subparser_version
        try:
            req_v = urllib.request.Request(
                f"https://raw.githubusercontent.com/ib0lit/SubParser/{branch}/root/etc/subparser_version",
                headers={'User-Agent': 'OpenWrt-SubParser-Bot'})
            with urllib.request.urlopen(req_v, timeout=5, context=get_ssl_ctx()) as resp_v:
                remote_ver = resp_v.read().decode('utf-8').strip()
        except Exception:
            screen = RichScreen()
            screen.h("❌ Ошибка сети")
            screen.quote("Не удалось проверить наличие обновлений на GitHub.")
            screen.button_row([
                {"type": "url", "text": "🔗 Открыть репозиторий",
                 "url": "https://github.com/ib0lit/SubParser", "style": "primary"},
            ])
            return screen.build(), {"inline_keyboard": [[{"text": "◀ Главное меню", "callback_data": "home"}]]}

    try:
        req_v = urllib.request.Request(
            f"https://raw.githubusercontent.com/ib0lit/SubParser/{branch}/root/etc/subparser_version",
            headers={'User-Agent': 'OpenWrt-SubParser-Bot'})
        with urllib.request.urlopen(req_v, timeout=5, context=get_ssl_ctx()) as resp_v:
            remote_ver = resp_v.read().decode('utf-8').strip()
    except Exception:
        pass

    is_update_available = False
    if local_sha and remote_sha:
        is_update_available = (local_sha != remote_sha)
    elif remote_ver and local_ver != "unknown":
        is_update_available = (local_ver != remote_ver)

    branch_btns = [
        {"text": f"{'🔘' if branch == 'main' else '⚪'} main", "callback_data": "switch_branch_main"},
        {"text": f"{'🔘' if branch == 'beta' else '⚪'} beta", "callback_data": "switch_branch_beta"},
    ]
    rich_branch_row = [
        {"type": "callback_data", "text": f"{'🔘' if branch == 'main' else '⚪'} main",
         "data": "switch_branch_main", "style": "primary" if branch == "main" else "link"},
        {"type": "callback_data", "text": f"{'🔘' if branch == 'beta' else '⚪'} beta",
         "data": "switch_branch_beta", "style": "primary" if branch == "beta" else "link"},
    ]

    if is_update_available:
        short_remote_sha = remote_sha[:7] if remote_sha else "н/д"
        short_local_sha = local_sha[:7] if local_sha else "н/д"
        screen = RichScreen()
        screen.h("🎉 Доступно обновление!")
        screen.kv([
            ("🌿 Ветка", f"<code>{esc(branch)}</code>"),
            ("📦 Текущая версия", f"<code>v{esc(local_display_ver)}</code> · "
                                  f"<code>{esc(short_local_sha)}</code>"),
            ("🚀 Новая версия", f"<code>v{esc(remote_ver)}</code> · "
                                f"<code>{esc(short_remote_sha)}</code>"),
        ])
        screen.details("📝 Что нового (последний коммит)",
                       f"<p>{changelog}</p>", f"<i>{changelog}</i>", open=True)
        screen.button_row([
            {"type": "callback_data", "text": "🔄 Обновить SubParser", "data": "do_update", "style": "success"},
        ])
        screen.button_row(rich_branch_row)
        screen.button_row([
            {"type": "url", "text": "🔗 Коммит на GitHub",
             "url": f"https://github.com/ib0lit/SubParser/commit/{esc_attr(remote_sha)}",
             "style": "link"},
            {"type": "callback_data", "text": "◀ В меню", "data": "home", "style": "link"},
        ])
        text = screen.build()
        kb = {"inline_keyboard": [
            [{"text": "🔄 Обновить", "callback_data": "do_update"}],
            branch_btns,
            [{"text": "◀ Главное меню", "callback_data": "home"}]
        ]}
    else:
        screen = RichScreen()
        screen.h("✅ Установлена последняя версия")
        screen.kv([
            ("🌿 Ветка", f"<code>{esc(branch)}</code>"),
            ("📦 Версия", f"<code>v{esc(local_display_ver)}</code>"),
            ("🔖 Коммит", f"<code>{esc(local_sha[:7] if local_sha else 'н/д')}</code>"),
        ])
        screen.button_row(rich_branch_row)
        screen.button_row([
            {"type": "callback_data", "text": "◀ Главное меню", "data": "home", "style": "link"},
        ])
        text = screen.build()
        kb = {"inline_keyboard": [
            branch_btns,
            [{"text": "◀ Главное меню", "callback_data": "home"}]
        ]}

    return text, kb
