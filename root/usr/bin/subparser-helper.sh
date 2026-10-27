#!/bin/sh
ACTION="$1"
SECTION="$2"
[ -z "$SECTION" ] && SECTION="main"

# Имя секции UCI не должно содержать ничего, кроме безопасных символов.
case "$SECTION" in
    *[!A-Za-z0-9_-]*)
        echo '{"status": "error", "message": "Недопустимое имя секции"}'
        exit 1
        ;;
esac

LAN_IP=$(uci -q get network.lan.ipaddr | cut -d'/' -f1)
[ -z "$LAN_IP" ] && LAN_IP="192.168.1.1"
API_HOST="${LAN_IP}:9090"

PID_FILE="/tmp/subparser_sync.pid"
LOG_FILE="/tmp/subparser_sync.log"
LOCK_FILE="/var/run/subparser.lock"

case "$ACTION" in
    get_podkop_sections)
        # Только чтение. Раньше здесь молча перезаписывался target_section,
        # из-за чего открытие вкладки LuCI меняло настройки парсера.
        SECTIONS=$(uci -q show podkop 2>/dev/null | sed -n 's/^podkop\.\([^.=]*\)=podkop.*/\1/p' | sort -u)
        [ -z "$SECTIONS" ] && SECTIONS="main"
        SECTIONS="$SECTIONS" python3 - <<'PYEOF'
import json, os
items = os.environ.get("SECTIONS", "main").split()
print(json.dumps(items, ensure_ascii=False))
PYEOF
        ;;

    test_tg)
        TOKEN=$(uci -q get subparser.settings.tg_bot_token)
        CHAT_ID=$(uci -q get subparser.settings.tg_chat_id)
        if [ -z "$TOKEN" ] || [ -z "$CHAT_ID" ]; then
            echo '{"status": "error", "message": "Токен или Chat ID не заполнены в UCI!"}'
            exit 0
        fi
        MSG="🔔 <b>SubParser</b>: Тестовое сообщение успешно доставлено!"
        RES=$(curl -s -m 8 -X POST "https://api.telegram.org/bot${TOKEN}/sendMessage" \
            --data-urlencode "chat_id=${CHAT_ID}" \
            --data-urlencode "text=${MSG}" \
            --data-urlencode "parse_mode=HTML" 2>&1)
        if echo "$RES" | grep -q '"ok":true'; then
            echo '{"status": "ok"}'
        else
            printf '%s' "$RES" | python3 -c 'import json,sys; print(json.dumps({"status": "error", "message": sys.stdin.read()[:300]}, ensure_ascii=False))'
        fi
        ;;

    bg_sync)
        if [ -f "$PID_FILE" ] && kill -0 "$(cat "$PID_FILE")" 2>/dev/null; then
            echo '{"status": "running"}'
            exit 0
        fi

        if [ -f "$LOG_FILE" ]; then
            tail -n 200 "$LOG_FILE" > "${LOG_FILE}.tmp" 2>/dev/null
            mv "${LOG_FILE}.tmp" "$LOG_FILE"
        fi

        echo "=== Старт синхронизации: $(date) ===" >> "$LOG_FILE"

        (
            flock -n 9 || {
                echo "[!] Синхронизация уже запущена другим процессом." >> "$LOG_FILE"
                exit 1
            }
            /usr/bin/python3 /usr/bin/subparser.py --force >> "$LOG_FILE" 2>&1
            rm -f "$PID_FILE"
        ) 9>"$LOCK_FILE" </dev/null >/dev/null 2>&1 &

        echo $! > "$PID_FILE"
        echo '{"status": "started"}'
        ;;

    check_sync)
        if [ -f "$PID_FILE" ] && kill -0 "$(cat "$PID_FILE")" 2>/dev/null; then
            echo '{"status": "running"}'
        else
            rm -f "$PID_FILE"
            if [ -f "$LOG_FILE" ] && awk '
                /=== Старт синхронизации/ { found=1; win="" }
                found { win = win $0 "\n" }
                END {
                    if (win ~ /Podkop успешно обновлен/) exit 0
                    if (win ~ /Состав узлов не изменился/) exit 0
                    exit 1
                }' "$LOG_FILE"; then
                echo '{"status": "done"}'
            else
                echo '{"status": "error"}'
            fi
        fi
        ;;

    update_cron)
        /etc/init.d/subparser restart >/dev/null 2>&1
        echo '{"status": "ok"}'
        ;;

    get_links)
        LINKS=$(uci -q get podkop.${SECTION}.urltest_proxy_links)
        printf '%s' "$LINKS" | python3 -c 'import json,sys; print(json.dumps(sys.stdin.read().split(), ensure_ascii=False))'
        ;;

    save_selected)
        SRC_FILE="/tmp/subparser_selected.txt"
        if [ ! -s "$SRC_FILE" ]; then
            echo '{"status": "error", "message": "Файл пуст или не найден"}'
            exit 1
        fi
        TMP_BATCH=$(mktemp /tmp/subparser_uci.XXXXXX) || exit 1
        printf 'delete podkop.%s.urltest_proxy_links\n' "$SECTION" > "$TMP_BATCH"
        while IFS= read -r link || [ -n "$link" ]; do
            link_clean=$(printf '%s' "$link" | tr -d '\r\n')
            [ -z "$link_clean" ] && continue
            esc=$(printf '%s' "$link_clean" | sed -e 's/\\/\\\\/g' -e 's/"/\\"/g')
            printf 'add_list podkop.%s.urltest_proxy_links="%s"\n' "$SECTION" "$esc" >> "$TMP_BATCH"
        done < "$SRC_FILE"
        if uci -q batch < "$TMP_BATCH" && uci -q commit podkop; then
            rm -f "$SRC_FILE" "$TMP_BATCH"
            /etc/init.d/podkop restart >/dev/null 2>&1
            echo '{"status": "ok"}'
        else
            rm -f "$TMP_BATCH"
            echo '{"status": "error", "message": "Не удалось записать конфигурацию Podkop"}'
            exit 1
        fi
        ;;

    ping_group)
        URL="https://www.gstatic.com/generate_204"
        RES=$(curl -s -m 5 "http://${API_HOST}/group/${SECTION}-urltest-out/delay?url=${URL}&timeout=3000" 2>/dev/null)
        [ -n "$RES" ] && echo "$RES" || echo '{}'
        ;;

    ping_node)
        TAG="$2"
        URL="https://www.gstatic.com/generate_204"
        RES=$(curl -s -m 4 "http://${API_HOST}/proxies/${TAG}/delay?url=${URL}&timeout=3000" 2>/dev/null)
        [ -n "$RES" ] && echo "$RES" || echo '{"delay": -1}'
        ;;

    *)
        echo '{"error": "unknown action"}'
        exit 1
        ;;
esac
