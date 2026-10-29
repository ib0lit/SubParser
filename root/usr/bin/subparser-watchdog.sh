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

# Cron запускает скрипт каждую минуту, а фактическую частоту задаёт
# watchdog_interval. Так настройка применяется без перезаписи crontab.
INTERVAL=$(num "$(cfg watchdog_interval 5)" 5 1 60)
NOW=$(date +%s)
if [ -f "$LAST_RUN_FILE" ]; then
    LAST_RUN=$(cat "$LAST_RUN_FILE" 2>/dev/null)
    case "$LAST_RUN" in ''|*[!0-9]*) LAST_RUN=0 ;; esac
    if [ "$LAST_RUN" -gt 0 ] && [ $((NOW - LAST_RUN)) -ge 0 ] &&
       [ $((NOW - LAST_RUN)) -lt $((INTERVAL * 60)) ]; then
        exit 0
    fi
fi
printf '%s\n' "$NOW" > "$LAST_RUN_FILE"

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
[ "$SUB_LIMITS" = "1" ] && check_sub_limits

# --- Проверка процесса sing-box --------------------------------------------------
# Процесс может отсутствовать во время перезапуска Podkop или на медленном
# роутере с большим конфигом, поэтому ждём появления, а не одну фиксированную паузу.
if ! pidof sing-box >/dev/null 2>&1; then
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
