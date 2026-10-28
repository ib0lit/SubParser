#!/bin/sh
# SubParser watchdog: следит за Podkop и при длительной аварии запускает
# внеочередной отбор узлов. Скрипт не удаляет lock-файл парсера: блокировку
# держит flock внутри subparser.py.

ENABLED=$(uci -q get subparser.settings.enabled)
[ "$ENABLED" = "1" ] || exit 0

LOCK_FILE="/var/run/subparser.lock"
STATE_FILE="/tmp/subparser_watchdog.state"

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
check_sub_limits

# --- Проверка процесса sing-box --------------------------------------------------
SB_WAS_DOWN=0
if ! pidof sing-box >/dev/null 2>&1; then
    sleep 10
    if ! pidof sing-box >/dev/null 2>&1; then
        check_wan_alive || exit 0
        SB_WAS_DOWN=1
        notify "🚨 <b>Внимание: Podkop упал!</b>
Служба sing-box не активна. Перезапускаю Podkop..."
        /etc/init.d/podkop restart >/dev/null 2>&1
        sleep 15
        if pidof sing-box >/dev/null 2>&1; then
            notify "✅ <b>Podkop восстановлен</b>
Служба sing-box снова запущена."
            exit 0
        fi
        exit 0
    fi
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
    if [ "$PROC_AGE" -ge 0 ] && [ "$PROC_AGE" -lt 35 ]; then
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

if ! api_get "/proxies" 2 | grep -q '"proxies"'; then
    # API может быть недоступен при живом sing-box — не считаем это аварией.
    exit 0
fi

# --- Проверка управляемых секций Podkop -----------------------------------------
SECTION_LIST=$(python3 - <<'PYEOF'
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

TEST_URL="https%3A%2F%2Fwww.gstatic.com%2Fgenerate_204"
FAILED_SECTIONS=""
HEALTHY_SECTIONS=""

for SEC in $SECTION_LIST; do
    GROUP_TAG="${SEC}-urltest-out"
    PROXIES=$(api_get "/proxies" 3)
    if ! echo "$PROXIES" | grep -q "\"${GROUP_TAG}\""; then
        GROUP_TAG=$(echo "$PROXIES" | grep -o "\"[a-zA-Z0-9_-]*${SEC}[a-zA-Z0-9_-]*urltest[a-zA-Z0-9_-]*\"" | tr -d '"' | head -n 1)
    fi
    if [ -z "$GROUP_TAG" ]; then
        # В секции нет urltest-группы — проверять нечего.
        continue
    fi

    DELAY=$(api_get "/proxies/${GROUP_TAG}/delay?url=${TEST_URL}&timeout=4000" 8 | grep -o '"delay":[0-9]*' | cut -d':' -f2)
    if [ -z "$DELAY" ] || [ "$DELAY" -le 0 ]; then
        # Принудительный тест всей группы, затем повторный контрольный замер.
        api_get "/group/${GROUP_TAG}/delay?url=${TEST_URL}&timeout=3000" 10 >/dev/null
        sleep 3
        DELAY=$(api_get "/proxies/${GROUP_TAG}/delay?url=${TEST_URL}&timeout=4000" 8 | grep -o '"delay":[0-9]*' | cut -d':' -f2)
    fi

    if [ -n "$DELAY" ] && [ "$DELAY" -gt 0 ]; then
        HEALTHY_SECTIONS="${HEALTHY_SECTIONS} ${SEC}"
    else
        FAILED_SECTIONS="${FAILED_SECTIONS} ${SEC}"
    fi
done

# Все секции живы. О восстановлении сообщаем только если авария была
# подтверждена и watchdog уже запускал экстренный отбор узлов: одиночный
# кратковременный провал замера не повод для уведомления.
if [ -z "$FAILED_SECTIONS" ]; then
    if [ -f "$STATE_FILE" ]; then
        PREV_STAGE=$(sed -n '1p' "$STATE_FILE" 2>/dev/null)
        case "$PREV_STAGE" in ''|*[!0-9]*) PREV_STAGE=0 ;; esac
        if [ "$PREV_STAGE" -ge 2 ]; then
            notify "✅ <b>Проксирование Podkop восстановлено</b>
Все секции снова отвечают на тестовый запрос."
        fi
        rm -f "$STATE_FILE" 2>/dev/null
    fi
    exit 0
fi

# Если упал интернет провайдера — не трогаем прокси.
check_wan_alive || exit 0

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

# Кулдаун 45 минут между экстренными парсингами.
ELAPSED=$((NOW - LAST_RETRY))
if [ "$ELAPSED" -lt 2700 ] && [ "$ELAPSED" -ge 0 ]; then
    exit 0
fi

printf "%s\n%s\n%s\n" "2" "$NOW" "$FAILED_SECTIONS" > "$STATE_FILE"

FAILED_NAMES=$(echo "$FAILED_SECTIONS" | sed 's/^ *//; s/ /, /g')
notify "⚠️ <b>Сбой проксирования Podkop!</b>
Секции [${FAILED_NAMES}] не отвечают. Запускаю экстренный отбор узлов..."

# Lock-файл НЕ удаляем: subparser.py сам берёт flock. Если парсер уже
# запущен, новый экземпляр просто завершится без повреждения конфигурации.
SUBPARSER_TRIGGER=watchdog /usr/bin/python3 /usr/bin/subparser.py --force >/dev/null 2>&1 &
