#!/bin/sh
# SubParser watchdog: следит за Podkop и при длительном сбое запускает
# внеочередной отбор узлов. Все пороги настраиваются в LuCI на вкладке
# «Watchdog»; значения по умолчанию повторяют прежнее поведение.
# Скрипт не удаляет lock-файл парсера: блокировку держит flock внутри
# subparser.py.

# Чтение настройки с запасным значением по умолчанию.
cfg() {
    _wd_cfg=$(uci -q get "subparser.settings.$1")
    [ -n "$_wd_cfg" ] || _wd_cfg="$2"
    printf '%s' "$_wd_cfg"
}

# Приведение числа к диапазону: значение, дефолт, минимум, максимум.
num() {
    _wd_num="$1"
    case "$_wd_num" in ''|*[!0-9]*) _wd_num="$2" ;; esac
    [ "$_wd_num" -lt "$3" ] && _wd_num="$3"
    [ "$_wd_num" -gt "$4" ] && _wd_num="$4"
    printf '%s' "$_wd_num"
}

# Счётчики WAN-интерфейса: rx tx. Если интерфейс не определён, суммируем все,
# кроме lo. Это только индикатор активности, а не точный биллинг.
wan_counters() {
    _if=$(ip route show default 2>/dev/null | awk '{for (i=1;i<=NF;i++) if ($i=="dev") {print $(i+1); exit}}')
    [ -z "$_if" ] && _if=$(uci -q get network.wan.device)
    [ -z "$_if" ] && _if=$(uci -q get network.wan.ifname)
    _rx=0
    _tx=0
    if [ -n "$_if" ] && [ -r "/sys/class/net/${_if}/statistics/rx_bytes" ]; then
        _rx=$(cat "/sys/class/net/${_if}/statistics/rx_bytes" 2>/dev/null)
        _tx=$(cat "/sys/class/net/${_if}/statistics/tx_bytes" 2>/dev/null)
    else
        for _f in /sys/class/net/*/statistics/rx_bytes; do
            [ -r "$_f" ] || continue
            _name=${_f#/sys/class/net/}
            _name=${_name%%/*}
            [ "$_name" = "lo" ] && continue
            _rx=$((_rx + $(cat "$_f" 2>/dev/null || echo 0)))
            _tx=$((_tx + $(cat "${_f%rx_bytes}tx_bytes" 2>/dev/null || echo 0)))
        done
    fi
    case "$_rx" in ''|*[!0-9]*) _rx=0 ;; esac
    case "$_tx" in ''|*[!0-9]*) _tx=0 ;; esac
    printf '%s %s' "$_rx" "$_tx"
}

[ "$(cfg enabled 0)" = "1" ] || exit 0
[ "$(cfg watchdog_enabled 1)" = "1" ] || exit 0

# Podkop может быть выключен намеренно — тогда это не авария.
[ "$(uci -q get podkop.settings.enabled)" = "0" ] && exit 0

LOCK_FILE="/var/run/subparser.lock"
WD_LOCK="/var/run/subparser-watchdog.lock"
STATE_FILE="/tmp/subparser_watchdog.state"
LAST_RUN_FILE="/tmp/subparser_watchdog.last"
DOWN_FILE="/tmp/subparser_watchdog.down"

# Собственный lock: долгая проверка не должна накладываться на следующий запуск.
if command -v flock >/dev/null 2>&1; then
    exec 8>"$WD_LOCK"
    flock -n 8 2>/dev/null || exit 0
fi

# Cron запускает скрипт каждую минуту. Основные проверки идут по
# watchdog_interval, а сервисы — по своему адаптивному расписанию.
INTERVAL=$(num "$(cfg watchdog_interval 5)" 5 1 60)
SERVICES=$(uci -q get subparser.settings.watchdog_services)
SERVICE_COOLDOWN=$(num "$(cfg watchdog_service_cooldown 10)" 10 1 1440)
SERVICE_NOTIFY=$(cfg watchdog_service_notify 1)
SERVICE_ACTIVE_INTERVAL=$(num "$(cfg watchdog_service_active_interval 1)" 1 1 60)
SERVICE_IDLE_INTERVAL=$(num "$(cfg watchdog_service_idle_interval 5)" 5 1 60)
SERVICE_TRAFFIC_KB=$(num "$(cfg watchdog_service_traffic_kb 20)" 20 1 100000)
LAST_SERVICE_FILE="/tmp/subparser_service.last"
TRAFFIC_FILE="/tmp/subparser_service.traffic"
NOW=$(date +%s)

MAIN_DUE=1
if [ -f "$LAST_RUN_FILE" ]; then
    LAST_RUN=$(cat "$LAST_RUN_FILE" 2>/dev/null)
    case "$LAST_RUN" in ''|*[!0-9]*) LAST_RUN=0 ;; esac
    if [ "$LAST_RUN" -gt 0 ] && [ $((NOW - LAST_RUN)) -ge 0 ] &&
       [ $((NOW - LAST_RUN)) -lt $((INTERVAL * 60)) ]; then
        MAIN_DUE=0
    fi
fi

# Адаптивная частота проверки сервисов: при активном трафике чаще, без
# трафика — реже. Счётчики читаются каждый запуск cron (раз в минуту).
SERVICE_DUE=0
SERVICE_ACTIVE=0
if [ -n "$SERVICES" ]; then
    set -- $(wan_counters)
    RX="$1"
    TX="$2"
    PREV_TS=0
    PREV_RX=0
    PREV_TX=0
    if [ -f "$TRAFFIC_FILE" ]; then
        read -r PREV_TS PREV_RX PREV_TX < "$TRAFFIC_FILE" 2>/dev/null || true
        case "$PREV_TS" in ''|*[!0-9]*) PREV_TS=0 ;; esac
        case "$PREV_RX" in ''|*[!0-9]*) PREV_RX=0 ;; esac
        case "$PREV_TX" in ''|*[!0-9]*) PREV_TX=0 ;; esac
    else
        # Первый запуск после установки/перезагрузки: не ждём накопления.
        SERVICE_ACTIVE=1
    fi
    if [ "$PREV_TS" -gt 0 ] && [ "$NOW" -gt "$PREV_TS" ]; then
        ELAPSED=$((NOW - PREV_TS))
        DELTA=$((RX + TX - PREV_RX - PREV_TX))
        [ "$DELTA" -lt 0 ] && DELTA=0
        RATE_KB=$((DELTA / 1024 / ELAPSED))
        [ "$RATE_KB" -ge "$SERVICE_TRAFFIC_KB" ] && SERVICE_ACTIVE=1
    fi
    printf '%s %s %s\n' "$NOW" "$RX" "$TX" > "$TRAFFIC_FILE" 2>/dev/null || true

    SVC_INTERVAL=$SERVICE_IDLE_INTERVAL
    [ "$SERVICE_ACTIVE" = "1" ] && SVC_INTERVAL=$SERVICE_ACTIVE_INTERVAL
    if [ ! -f "$LAST_SERVICE_FILE" ]; then
        SERVICE_DUE=1
    else
        LAST_SVC=$(cat "$LAST_SERVICE_FILE" 2>/dev/null)
        case "$LAST_SVC" in ''|*[!0-9]*) LAST_SVC=0 ;; esac
        if [ "$LAST_SVC" -le 0 ] || [ $((NOW - LAST_SVC)) -ge $((SVC_INTERVAL * 60 - 5)) ]; then
            SERVICE_DUE=1
        fi
    fi
fi

if [ "$MAIN_DUE" = "0" ] && [ "$SERVICE_DUE" = "0" ]; then
    exit 0
fi
if [ "$MAIN_DUE" = "1" ]; then
    printf '%s\n' "$NOW" > "$LAST_RUN_FILE"
fi

DOWN_WAIT=$(num "$(cfg watchdog_down_wait 10)" 10 2 600)
RESTART_WAIT=$(num "$(cfg watchdog_restart_wait 15)" 15 2 600)
YOUNG_SEC=$(num "$(cfg watchdog_young_sec 35)" 35 0 3600)
COOLDOWN_MIN=$(num "$(cfg watchdog_cooldown 45)" 45 1 10080)
HTTP_TIMEOUT=$(num "$(cfg watchdog_http_timeout 8)" 8 2 60)
TEST_TIMEOUT=$(num "$(cfg watchdog_test_timeout 4000)" 4000 500 30000)
GROUP_TIMEOUT=$(num "$(cfg watchdog_group_timeout 3000)" 3000 500 30000)
GROUP_RETRY=$(num "$(cfg watchdog_group_retry 3)" 3 0 60)
MAX_RUN=$(num "$(cfg watchdog_max_run 600)" 600 30 3600)
WAN_CHECK=$(cfg watchdog_wan_check 1)
RESTART_PODKOP=$(cfg watchdog_restart_podkop 1)
EMERGENCY=$(cfg watchdog_emergency 1)
NOTIFY_RECOVERY=$(cfg watchdog_notify_recovery 1)
SUB_LIMITS=$(cfg watchdog_sub_limits 1)
TEST_URL_RAW=$(cfg watchdog_test_url "https://www.gstatic.com/generate_204")

# Парсер или синхронизация уже работают — не вмешиваемся. Проверяем именно
# flock, а не факт существования файла (файл остаётся после завершения).
if command -v flock >/dev/null 2>&1 && [ -f "$LOCK_FILE" ]; then
    if ! ( flock -n 9 ) 9>"$LOCK_FILE" 2>/dev/null; then
        exit 0
    fi
fi
if pgrep -f "[s]ubparser.py" >/dev/null 2>&1 || pgrep -f "[s]ubparser-helper.sh" >/dev/null 2>&1; then
    exit 0
fi

TOKEN=$(uci -q get subparser.settings.tg_bot_token)
CHAT_ID=$(uci -q get subparser.settings.tg_chat_id)

notify() {
    [ -n "$TOKEN" ] && [ -n "$CHAT_ID" ] || return 0
    curl -s -m 10 -X POST "https://api.telegram.org/bot${TOKEN}/sendMessage" \
        --data-urlencode "chat_id=${CHAT_ID}" \
        --data-urlencode "text=$1" \
        --data-urlencode "parse_mode=HTML" >/dev/null 2>&1
}

check_wan_alive() {
    ping -c 2 -W 2 77.88.8.8 >/dev/null 2>&1 || ping -c 2 -W 2 1.1.1.1 >/dev/null 2>&1
}

# --- Мониторинг лимитов подписок -------------------------------------------------
check_sub_limits() {
    [ -f "/tmp/subparser_subinfo.json" ] || return 0
    TOKEN="$TOKEN" CHAT_ID="$CHAT_ID" python3 - <<'PYEOF'
import json, os, time, urllib.parse, urllib.request

SUB_FILE = '/tmp/subparser_subinfo.json'
ALERT_FILE = '/tmp/subparser_sub_alerts.json'
TOKEN = os.environ.get('TOKEN', '')
CHAT_ID = os.environ.get('CHAT_ID', '')
if not TOKEN or not CHAT_ID or not os.path.exists(SUB_FILE):
    raise SystemExit(0)

try:
    with open(SUB_FILE, 'r', encoding='utf-8') as f:
        subs = json.load(f)
except Exception:
    raise SystemExit(0)

alerts_hist = {}
if os.path.exists(ALERT_FILE):
    try:
        with open(ALERT_FILE, 'r', encoding='utf-8') as f:
            alerts_hist = json.load(f)
    except Exception:
        alerts_hist = {}

now = int(time.time())
warns = []
for name, info in subs.items():
    total = info.get('total', 0)
    used = info.get('upload', 0) + info.get('download', 0)
    expire = info.get('expire', 0)
    if (now - alerts_hist.get(name, 0)) < 86400:
        continue
    reasons = []
    if expire and expire > 0:
        days_left = (expire - now) / 86400.0
        if 0 <= days_left <= 3:
            reasons.append('до окончания осталось <b>%.1f дн.</b>' % days_left)
        elif days_left < 0:
            reasons.append('срок действия <b>истёк!</b>')
    if total and total > 0:
        pct = (used / total) * 100
        if pct >= 90:
            reasons.append('израсходовано <b>%.1f%% трафика</b>' % pct)
    if reasons:
        alerts_hist[name] = now
        warns.append('⚠️ Подписка <b>%s</b>: %s' % (name, ', '.join(reasons)))

if not warns:
    raise SystemExit(0)

msg = '🔔 <b>Внимание: лимиты подписок на исходе!</b>\n\n' + '\n'.join(warns)
try:
    data = urllib.parse.urlencode({'chat_id': CHAT_ID, 'text': msg, 'parse_mode': 'HTML'}).encode('utf-8')
    req = urllib.request.Request('https://api.telegram.org/bot%s/sendMessage' % TOKEN, data=data)
    urllib.request.urlopen(req, timeout=8)
    with open(ALERT_FILE, 'w', encoding='utf-8') as f:
        json.dump(alerts_hist, f)
except Exception:
    pass
PYEOF
}
# Лимиты подписок — часть основной проверки, а не сервисной.
[ "$SUB_LIMITS" = "1" ] && [ "$MAIN_DUE" = "1" ] && check_sub_limits

# --- Проверка процесса sing-box --------------------------------------------------
# Процесс может отсутствовать во время перезапуска Podkop или на медленном
# роутере с большим конфигом, поэтому ждём появления, а не одну фиксированную паузу.
if ! pidof sing-box >/dev/null 2>&1; then
    # Сервисный цикл без ядра бессмысленен, и это не повод для перезапуска:
    # основная проверка сама обработает отсутствие sing-box в свой срок.
    [ "$MAIN_DUE" = "0" ] && exit 0

    WAITED=0
    while [ "$WAITED" -lt "$DOWN_WAIT" ]; do
        sleep 2
        WAITED=$((WAITED + 2))
        pidof sing-box >/dev/null 2>&1 && exit 0
    done

    [ "$WAN_CHECK" = "1" ] && { check_wan_alive || exit 0; }

    if [ ! -f "$DOWN_FILE" ]; then
        : > "$DOWN_FILE"
        if [ "$RESTART_PODKOP" = "1" ]; then
            notify "🚨 <b>Внимание: Podkop упал!</b>
Служба sing-box не активна. Перезапускаю Podkop..."
        else
            notify "🚨 <b>Внимание: Podkop упал!</b>
Служба sing-box не активна. Автоперезапуск отключён в настройках watchdog."
        fi
    fi
    [ "$RESTART_PODKOP" = "1" ] || exit 0

    /etc/init.d/podkop restart >/dev/null 2>&1
    WAITED=0
    while [ "$WAITED" -lt "$RESTART_WAIT" ]; do
        sleep 2
        WAITED=$((WAITED + 2))
        if pidof sing-box >/dev/null 2>&1; then
            rm -f "$DOWN_FILE" 2>/dev/null
            [ "$NOTIFY_RECOVERY" = "1" ] && notify "✅ <b>Podkop восстановлен</b>
Служба sing-box снова запущена."
            exit 0
        fi
    done
    exit 0
fi

# Процесс поднялся сам (procd, пользователь, LuCI) — снимаем отметку аварии.
if [ -f "$DOWN_FILE" ]; then
    rm -f "$DOWN_FILE" 2>/dev/null
    [ "$NOTIFY_RECOVERY" = "1" ] && notify "✅ <b>Podkop восстановлен</b>
Служба sing-box снова запущена."
fi

# Защита от переходных процессов: только что запущенное ядро не проверяем.
SB_PID=$(pidof sing-box 2>/dev/null | awk '{print $1}')
if [ -n "$SB_PID" ] && [ -d "/proc/$SB_PID" ]; then
    UPTIME_SYS=$(cut -d'.' -f1 /proc/uptime)
    START_TIME_TICKS=$(awk '{print $22}' "/proc/$SB_PID/stat" 2>/dev/null)
    [ -z "$START_TIME_TICKS" ] && START_TIME_TICKS=0
    CLK_TCK=$(getconf CLK_TCK 2>/dev/null || echo 100)
    PROC_START_SEC=$((START_TIME_TICKS / CLK_TCK))
    PROC_AGE=$((UPTIME_SYS - PROC_START_SEC))
    if [ "$PROC_AGE" -ge 0 ] && [ "$PROC_AGE" -lt "$YOUNG_SEC" ]; then
        exit 0
    fi
fi

# --- REST API --------------------------------------------------------------------
API_HOST=$(uci -q get subparser.settings.podkop_api)
if [ -z "$API_HOST" ]; then
    LAN_IP=$(uci -q get network.lan.ipaddr | cut -d'/' -f1)
    [ -z "$LAN_IP" ] && LAN_IP=$(uci -q get subparser.settings.lan_ip_override)
    [ -z "$LAN_IP" ] && LAN_IP="192.168.1.1"
    # Если Podkop слушает API на заданном адресе, берём его. 0.0.0.0/:: —
    # это «все интерфейсы», подключаться к ним бессмысленно.
    API_LISTEN=$(uci -q get podkop.settings.service_listen_address | cut -d'/' -f1)
    case "$API_LISTEN" in
        ""|0.0.0.0|::|"*") ;;
        *) LAN_IP="$API_LISTEN" ;;
    esac
    API_PORT=$(uci -q get subparser.settings.podkop_api_port)
    [ -z "$API_PORT" ] && API_PORT=$(uci -q get subparser.settings.clash_api_port)
    [ -z "$API_PORT" ] && API_PORT=9090
    API_HOST="${LAN_IP}:${API_PORT}"
fi
case "$API_HOST" in
    http://*|https://*) API_BASE="$API_HOST" ;;
    *) API_BASE="http://${API_HOST}" ;;
esac
API_BASE="${API_BASE%/}"

API_SECRET=$(uci -q get subparser.settings.podkop_api_secret)
[ -z "$API_SECRET" ] && API_SECRET=$(uci -q get subparser.settings.clash_api_secret)
# При включённом YACD Podkop использует yacd_secret_key как secret Clash API.
[ -z "$API_SECRET" ] && API_SECRET=$(uci -q get podkop.settings.yacd_secret_key)

api_get() {
    if [ -n "$API_SECRET" ]; then
        curl -s -m "$2" -H "Authorization: Bearer ${API_SECRET}" "${API_BASE}$1" 2>/dev/null
    else
        curl -s -m "$2" "${API_BASE}$1" 2>/dev/null
    fi
}

# --- Точечная проверка сервисов ------------------------------------------------
# Пользователь задаёт список HTTPS-адресов. Сначала проверяется активный узел.
# При сбое принудительно перепроверяется urltest-группа. Если сервис не ожил,
# на следующем цикле (и не чаще кулдауна) все узлы секции проверяются уже по
# адресу самого сервиса: sing-box сохраняет эти замеры в историю URLTest и
# может выбрать узел, который реально открывает сервис.
check_services() {
    [ -n "$SERVICES" ] || return 0
    [ -n "$HEALTHY_SECTIONS" ] || return 0
    API_BASE="$API_BASE" API_SECRET="$API_SECRET" \
    SECTIONS="$HEALTHY_SECTIONS" SERVICES="$SERVICES" \
    TEST_TIMEOUT="$TEST_TIMEOUT" HTTP_TIMEOUT="$HTTP_TIMEOUT" \
    GROUP_TIMEOUT="$GROUP_TIMEOUT" GROUP_RETRY="$GROUP_RETRY" \
    COOLDOWN="$SERVICE_COOLDOWN" MAX_RUN="$MAX_RUN" \
    DO_NOTIFY="$SERVICE_NOTIFY" TOKEN="$TOKEN" CHAT_ID="$CHAT_ID" \
    python3 - <<'PYEOF'
import html
import json
import os
import time
import urllib.parse
import urllib.request

API_BASE = os.environ.get("API_BASE", "").rstrip("/")
API_SECRET = os.environ.get("API_SECRET", "")
SECTIONS = [s for s in os.environ.get("SECTIONS", "").split() if s]
SERVICES = [s for s in os.environ.get("SERVICES", "").split() if s]
TEST_TIMEOUT = int(os.environ.get("TEST_TIMEOUT", "4000") or 4000)
HTTP_TIMEOUT = int(os.environ.get("HTTP_TIMEOUT", "8") or 8)
GROUP_TIMEOUT = int(os.environ.get("GROUP_TIMEOUT", "3000") or 3000)
GROUP_RETRY = int(os.environ.get("GROUP_RETRY", "3") or 0)
COOLDOWN = int(os.environ.get("COOLDOWN", "10") or 10) * 60
MAX_RUN = int(os.environ.get("MAX_RUN", "600") or 600)
DO_NOTIFY = os.environ.get("DO_NOTIFY", "1") == "1"
TOKEN = os.environ.get("TOKEN", "")
CHAT_ID = os.environ.get("CHAT_ID", "")
STATE_FILE = "/tmp/subparser_service.state"
START = time.time()

if not API_BASE or not SECTIONS or not SERVICES:
    raise SystemExit(0)

try:
    with open(STATE_FILE, "r", encoding="utf-8") as f:
        state = json.load(f)
    if not isinstance(state, dict):
        state = {}
except Exception:
    state = {}


def save_state():
    try:
        tmp = STATE_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(state, f)
        os.replace(tmp, STATE_FILE)
    except Exception:
        pass


def api(path, timeout=None):
    if timeout is None:
        timeout = HTTP_TIMEOUT
    req = urllib.request.Request(API_BASE + path)
    if API_SECRET:
        req.add_header("Authorization", "Bearer " + API_SECRET)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception:
        return None


def quote(value):
    return urllib.parse.quote(str(value), safe="")


def probe(tag, service):
    data = api("/proxies/%s/delay?url=%s&timeout=%d"
               % (quote(tag), quote(service), TEST_TIMEOUT), HTTP_TIMEOUT + 2)
    if not isinstance(data, dict):
        return None
    delay = data.get("delay")
    return delay if isinstance(delay, int) and delay > 0 else None


def selected(tag):
    data = api("/proxies/" + quote(tag))
    if not isinstance(data, dict):
        return None
    return data.get("now")


def resolve_group(proxies, section):
    tag = section + "-urltest-out"
    data = proxies.get(tag)
    if isinstance(data, dict) and str(data.get("type", "")).lower() == "urltest":
        return tag, data
    for key, value in proxies.items():
        if section in key and isinstance(value, dict) and \
                str(value.get("type", "")).lower() == "urltest":
            return key, value
    return None, None


def notify(key, text):
    if not DO_NOTIFY or not TOKEN or not CHAT_ID:
        return
    now = time.time()
    if now - state.get(key, {}).get("last_notify", 0) < COOLDOWN:
        return
    state.setdefault(key, {})["last_notify"] = int(now)
    data = urllib.parse.urlencode({
        "chat_id": CHAT_ID,
        "text": text,
        "parse_mode": "HTML",
    }).encode("utf-8")
    req = urllib.request.Request(
        "https://api.telegram.org/bot%s/sendMessage" % TOKEN, data=data)
    try:
        urllib.request.urlopen(req, timeout=8).read()
    except Exception:
        pass


def deep_probe(section, service, deadline):
    """Проверяет все узлы секции по адресу сервиса."""
    data = api("/proxies")
    if not isinstance(data, dict):
        return False
    proxies = data.get("proxies", {})
    tag, group = resolve_group(proxies, section)
    if not tag:
        return False
    for node in group.get("all", []):
        if node == tag:
            continue
        if time.time() > deadline:
            return False
        probe(node, service)
    return True


def short_name(tag):
    return html.escape(str(tag or "?"))


deadline = START + max(60, int(MAX_RUN * 0.8))
for section in SECTIONS:
    data = api("/proxies")
    if not isinstance(data, dict):
        continue
    tag, group = resolve_group(data.get("proxies", {}), section)
    if not tag:
        continue

    for service in SERVICES:
        if service.lower().startswith("http://"):
            # Clash API принудительно игнорирует HTTP-адреса.
            continue
        key = "%s|%s" % (section, service)
        st = state.setdefault(key, {})
        fails = int(st.get("fails", 0) or 0)

        delay = probe(tag, service)
        if delay:
            if fails >= 2:
                notify(key,
                       "✅ <b>Сервис снова доступен</b>\n"
                       "Секция <b>%s</b>: <code>%s</code>"
                       % (html.escape(section), html.escape(service)))
            st["fails"] = 0
            continue

        fails += 1
        st["fails"] = fails

        # Этап 1: дешёвая принудительная проверка всей urltest-группы.
        before = selected(tag)
        api("/group/%s/delay?url=%s&timeout=%d"
            % (quote(tag), quote(service), GROUP_TIMEOUT), HTTP_TIMEOUT + 2)
        if GROUP_RETRY > 0:
            time.sleep(GROUP_RETRY)
        after = selected(tag)
        if probe(tag, service):
            st["fails"] = 0
            if before and after and before != after:
                notify(key,
                       "🔁 <b>URLTest переключил узел</b>\n"
                       "Секция <b>%s</b>: <code>%s</code> → <code>%s</code>\n"
                       "Причина: недоступен <code>%s</code>"
                       % (html.escape(section), short_name(before),
                          short_name(after), html.escape(service)))
            continue

        # Этап 2: только со второго сбоя и не чаще кулдауна — проверяем все
        # узлы по адресу сервиса, чтобы URLTest выбрал подходящий.
        if fails < 2:
            continue
        now = time.time()
        if now - float(st.get("last_deep", 0) or 0) < COOLDOWN:
            continue
        st["last_deep"] = int(now)

        before = selected(tag)
        deep_probe(section, service, deadline)
        after = selected(tag)
        if probe(tag, service):
            st["fails"] = 0
            notify(key,
                   "🔁 <b>URLTest выбрал узел по сервису</b>\n"
                   "Секция <b>%s</b>: <code>%s</code> → <code>%s</code>\n"
                   "Сервис: <code>%s</code>"
                   % (html.escape(section), short_name(before),
                      short_name(after), html.escape(service)))
        else:
            notify(key,
                   "⚠️ <b>Сервис недоступен</b>\n"
                   "Секция <b>%s</b>: <code>%s</code>\n"
                   "Ни один проверенный узел не открыл адрес."
                   % (html.escape(section), html.escape(service)))

save_state()
PYEOF
}

if ! api_get "/proxies" 3 | grep -q '"proxies"'; then
    # API может быть недоступен при живом sing-box — не считаем это аварией.
    exit 0
fi

# --- Проверка управляемых секций Podkop -----------------------------------------
SECTION_LIST=$(python3 - 2>/dev/null <<'PYEOF'
import sys

sys.path.insert(0, "/usr/lib")
try:
    from subparser.podkop import get_managed_podkop_sections
    print(" ".join(get_managed_podkop_sections()))
except Exception:
    print("main")
PYEOF
)
[ -z "$SECTION_LIST" ] && SECTION_LIST="main"

# --- Сервисный цикл без основной проверки ---------------------------------------
# Основные проверки не наступили, но сервисы пора проверять: не трогаем
# процесс/секции, только убеждаемся, что есть WAN, и проверяем сервисы.
if [ "$MAIN_DUE" = "0" ]; then
    [ "$WAN_CHECK" = "1" ] && { check_wan_alive || exit 0; }
    HEALTHY_SECTIONS="$SECTION_LIST"
    check_services
    printf '%s\n' "$NOW" > "$LAST_SERVICE_FILE" 2>/dev/null || true
    exit 0
fi

# URL проверки кодируется один раз; при сбое python3 используем gstatic.
TEST_URL=$(TEST_URL_RAW="$TEST_URL_RAW" python3 -c \
    'import os, urllib.parse; print(urllib.parse.quote(os.environ.get("TEST_URL_RAW", ""), safe=""))' 2>/dev/null)
[ -n "$TEST_URL" ] || TEST_URL="https%3A%2F%2Fwww.gstatic.com%2Fgenerate_204"

FAILED_SECTIONS=""
HEALTHY_SECTIONS=""
RUN_START=$(date +%s)
ABORTED=0

for SEC in $SECTION_LIST; do
    if [ $(( $(date +%s) - RUN_START )) -ge "$MAX_RUN" ]; then
        # Не укладываемся в бюджет: прерываемся, не делая выводов о сбое.
        ABORTED=1
        break
    fi

    GROUP_TAG="${SEC}-urltest-out"
    PROXIES=$(api_get "/proxies" 3)
    if ! echo "$PROXIES" | grep -q "\"${GROUP_TAG}\""; then
        GROUP_TAG=$(echo "$PROXIES" | grep -o "\"[a-zA-Z0-9_-]*${SEC}[a-zA-Z0-9_-]*urltest[a-zA-Z0-9_-]*\"" | tr -d '"' | head -n 1)
    fi
    if [ -z "$GROUP_TAG" ]; then
        # В секции нет urltest-группы — проверять нечего.
        continue
    fi

    DELAY=$(api_get "/proxies/${GROUP_TAG}/delay?url=${TEST_URL}&timeout=${TEST_TIMEOUT}" "$HTTP_TIMEOUT" | grep -o '"delay":[0-9]*' | cut -d':' -f2)
    if [ -z "$DELAY" ] || [ "$DELAY" -le 0 ]; then
        # Принудительный тест всей группы, затем повторный контрольный замер.
        api_get "/group/${GROUP_TAG}/delay?url=${TEST_URL}&timeout=${GROUP_TIMEOUT}" "$((HTTP_TIMEOUT + 2))" >/dev/null
        [ "$GROUP_RETRY" -gt 0 ] && sleep "$GROUP_RETRY"
        DELAY=$(api_get "/proxies/${GROUP_TAG}/delay?url=${TEST_URL}&timeout=${TEST_TIMEOUT}" "$HTTP_TIMEOUT" | grep -o '"delay":[0-9]*' | cut -d':' -f2)
    fi

    if [ -n "$DELAY" ] && [ "$DELAY" -gt 0 ]; then
        HEALTHY_SECTIONS="${HEALTHY_SECTIONS} ${SEC}"
    else
        FAILED_SECTIONS="${FAILED_SECTIONS} ${SEC}"
    fi
done

[ "$ABORTED" = "1" ] && exit 0

# Точечные проверки сервисов не влияют на признание секции мёртвой: они лишь
# заставляют URLTest перепроверить узлы и при необходимости сменить активный.
# В основной цикл сервисы попадают только по своему расписанию.
if [ "$SERVICE_DUE" = "1" ]; then
    check_services
    printf '%s\n' "$NOW" > "$LAST_SERVICE_FILE" 2>/dev/null || true
fi

# Все секции живы. О восстановлении сообщаем только если авария была
# подтверждена и watchdog уже запускал экстренный отбор узлов: одиночный
# кратковременный провал замера не повод для уведомления.
if [ -z "$FAILED_SECTIONS" ]; then
    if [ -f "$STATE_FILE" ]; then
        PREV_STAGE=$(sed -n '1p' "$STATE_FILE" 2>/dev/null)
        case "$PREV_STAGE" in ''|*[!0-9]*) PREV_STAGE=0 ;; esac
        if [ "$PREV_STAGE" -ge 2 ] && [ "$NOTIFY_RECOVERY" = "1" ]; then
            notify "✅ <b>Проксирование Podkop восстановлено</b>
Все секции снова отвечают на тестовый запрос."
        fi
        rm -f "$STATE_FILE" 2>/dev/null
    fi
    exit 0
fi

# Если упал интернет провайдера — не трогаем прокси.
[ "$WAN_CHECK" = "1" ] && { check_wan_alive || exit 0; }

# --- Двухэтапное подтверждение аварии -------------------------------------------
NOW=$(date +%s)
FAIL_COUNT=0
LAST_RETRY=0
if [ -f "$STATE_FILE" ]; then
    FAIL_COUNT=$(sed -n '1p' "$STATE_FILE" 2>/dev/null)
    LAST_RETRY=$(sed -n '2p' "$STATE_FILE" 2>/dev/null)
fi
case "$FAIL_COUNT" in ''|*[!0-9]*) FAIL_COUNT=0 ;; esac
case "$LAST_RETRY" in ''|*[!0-9]*) LAST_RETRY=0 ;; esac

# Первый сбой — фиксируем и даём ядру один цикл на самовосстановление.
if [ "$FAIL_COUNT" -lt 1 ]; then
    printf "%s\n%s\n%s\n" "1" "$NOW" "$FAILED_SECTIONS" > "$STATE_FILE"
    exit 0
fi

# Кулдаун между экстренными парсингами.
ELAPSED=$((NOW - LAST_RETRY))
if [ "$ELAPSED" -lt $((COOLDOWN_MIN * 60)) ] && [ "$ELAPSED" -ge 0 ]; then
    exit 0
fi

printf "%s\n%s\n%s\n" "2" "$NOW" "$FAILED_SECTIONS" > "$STATE_FILE"

FAILED_NAMES=$(echo "$FAILED_SECTIONS" | sed 's/^ *//; s/ /, /g')
if [ "$EMERGENCY" = "1" ]; then
    notify "⚠️ <b>Сбой проксирования Podkop!</b>
Секции [${FAILED_NAMES}] не отвечают. Запускаю экстренный отбор узлов..."
else
    notify "⚠️ <b>Сбой проксирования Podkop!</b>
Секции [${FAILED_NAMES}] не отвечают. Экстренный отбор отключён в настройках watchdog."
    exit 0
fi

# Lock-файл НЕ удаляем: subparser.py сам берёт flock. Если парсер уже
# запущен, новый экземпляр просто завершится без повреждения конфигурации.
SUBPARSER_TRIGGER=watchdog /usr/bin/python3 /usr/bin/subparser.py --force >/dev/null 2>&1 &
