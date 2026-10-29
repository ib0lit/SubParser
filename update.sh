#!/bin/sh
set -e

REPO_USER="ib0lit"
REPO_NAME="SubParser"
BRANCH="${SUBPARSER_BRANCH:-}"
if [ -z "$BRANCH" ] && [ -f /etc/subparser_branch ]; then
    BRANCH=$(cat /etc/subparser_branch 2>/dev/null | tr -d ' \t\r\n')
fi
[ -z "$BRANCH" ] && BRANCH="main"
case "$BRANCH" in
    *[!A-Za-z0-9._/-]*)
        echo "[!] Недопустимое имя ветки: $BRANCH"
        exit 1
        ;;
esac
COMMIT_FILE="/etc/subparser_commit"
VERSION_FILE="/etc/subparser_version"
BRANCH_FILE="/etc/subparser_branch"

echo "=== [1/5] Проверка наличия обновлений (ветка: ${BRANCH}) ==="

REMOTE_SHA=$(curl -sL \
  -H "User-Agent: OpenWrt-SubParser-Updater" \
  "https://api.github.com/repos/${REPO_USER}/${REPO_NAME}/commits/${BRANCH}" \
  | grep '"sha":' | head -n 1 | cut -d '"' -f 4)

if [ -z "$REMOTE_SHA" ]; then
    echo "[!] Не удалось определить версию на GitHub. Выполняем принудительное обновление..."
else
    if [ -f "$COMMIT_FILE" ]; then
        LOCAL_SHA=$(cat "$COMMIT_FILE" 2>/dev/null || true)
        if [ "$LOCAL_SHA" = "$REMOTE_SHA" ]; then
            CURRENT_VER=$(cat "$VERSION_FILE" 2>/dev/null || echo "unknown")
            echo ""
            echo "=========================================================="
            echo " [i] Обновлений нет. Установлена актуальная версия: ${CURRENT_VER}"
            echo " SHA коммита: ${LOCAL_SHA:0:7}"
            echo "=========================================================="
            exit 0
        fi
    fi
    echo "[*] Найдена новая версия: ${REMOTE_SHA:0:7}"
fi

echo "=== [2/5] Проверка дискового пространства ==="
MIN_KB_UPDATE=512  # Требуется ~500 KB для безопасной перезаписи файлов и компиляции pyc

TARGET_DIR="/"
if df -k /overlay 2>/dev/null | grep -q "overlay"; then
    TARGET_DIR="/overlay"
fi

FREE_KB=$(df -k "$TARGET_DIR" 2>/dev/null | tail -n 1 | awk '{if (NF==1) {getline; print $(NF-2)} else {print $(NF-2)}}')

case "$FREE_KB" in
    ''|*[!0-9]*) FREE_KB="" ;;
esac

if [ -n "$FREE_KB" ]; then
    FREE_MB=$((FREE_KB / 1024))
    echo "[*] Доступно памяти на ${TARGET_DIR}: ${FREE_MB} MB (${FREE_KB} KB)"

    if [ "$FREE_KB" -lt "$MIN_KB_UPDATE" ]; then
        echo ""
        echo "=========================================================="
        echo " ❌ [ОШИБКА] Критически мало места для обновления!"
        echo " Доступно: ${FREE_KB} KB"
        echo " Требуется минимум: ${MIN_KB_UPDATE} KB (0.5 MB)"
        echo ""
        echo " Обновление отменено во избежание сбоя файловой системы."
        echo "=========================================================="
        exit 1
    fi
    echo "[OK] Памяти достаточно."
else
    echo "[!] Предупреждение: не удалось определить свободное место. Продолжаем..."
fi

echo "=== [3/5] Загрузка компонентов с GitHub ==="
TMP_DIR="/tmp/subparser-update"
rm -rf "$TMP_DIR"
mkdir -p "$TMP_DIR"

TARBALL="${TMP_DIR}/subparser.tar.gz"
if ! curl -fL --retry 3 --connect-timeout 15 -m 180 \
        "https://github.com/${REPO_USER}/${REPO_NAME}/archive/refs/heads/${BRANCH}.tar.gz?nocache=$(date +%s)" \
        -o "$TARBALL"; then
    echo "[!] Не удалось скачать архив ветки ${BRANCH}."
    rm -rf "$TMP_DIR"
    exit 1
fi
if ! gzip -t "$TARBALL" 2>/dev/null; then
    echo "[!] Архив повреждён (проверка gzip не пройдена). Обновление отменено."
    rm -rf "$TMP_DIR"
    exit 1
fi
if ! tar -xzf "$TARBALL" -C "$TMP_DIR"; then
    echo "[!] Не удалось распаковать архив. Обновление отменено."
    rm -rf "$TMP_DIR"
    exit 1
fi
# GitHub заменяет слэши в имени ветки при формировании каталога архива,
# поэтому ищем распакованный корень, а не собираем имя вручную.
SRC_PARENT=$(find "$TMP_DIR" -maxdepth 1 -mindepth 1 -type d | head -n 1)
SRC_PATH="${SRC_PARENT}/root"
if [ ! -d "$SRC_PATH" ]; then
    echo "[!] В архиве не найдена ожидаемая структура ${SRC_PATH}."
    rm -rf "$TMP_DIR"
    exit 1
fi

echo "=== [4/5] Применение обновлений и миграция структуры ==="
# Остановка бота перед заменой файлов
/etc/init.d/subparser-bot stop >/dev/null 2>&1 || true

mkdir -p /etc/hotplug.d/uci /usr/bin /usr/lib/subparser /etc/init.d \
         /usr/share/luci/menu.d /usr/share/rpcd/acl.d /www/luci-static/resources/view

# Очистка старых pyc файлов и развертывание библиотеки
rm -rf /usr/lib/subparser/__pycache__
cp -rf "${SRC_PATH}/usr/lib/subparser/"* /usr/lib/subparser/

cp -f "${SRC_PATH}/usr/bin/"* /usr/bin/
cp -f "${SRC_PATH}/etc/init.d/"* /etc/init.d/
cp -f "${SRC_PATH}/usr/share/luci/menu.d/"* /usr/share/luci/menu.d/
cp -f "${SRC_PATH}/usr/share/rpcd/acl.d/"* /usr/share/rpcd/acl.d/
cp -f "${SRC_PATH}/www/luci-static/resources/view/"* /www/luci-static/resources/view/
[ -f "${SRC_PATH}/etc/subparser_version" ] && cp -f "${SRC_PATH}/etc/subparser_version" /etc/subparser_version
[ -d "${SRC_PATH}/etc/hotplug.d/uci" ] && cp -f "${SRC_PATH}/etc/hotplug.d/uci/"* /etc/hotplug.d/uci/ 2>/dev/null || true

# Удаление устаревшего ACL-дубликата
rm -f /usr/share/rpcd/acl.d/subparser.json 2>/dev/null || true

chmod +x /usr/bin/subparser* /etc/init.d/subparser*

# Предкомпиляция Python-файлов
python3 -m py_compile /usr/lib/subparser/*.py /usr/bin/subparser-bot.py /usr/bin/subparser.py >/dev/null 2>&1 || true

# Регистрация watchdog в cron
CRON_TMP="/tmp/cron_subparser_upd.tmp"
crontab -l 2>/dev/null | grep -v "subparser-watchdog.sh" > "$CRON_TMP" || true
echo "*/5 * * * * /usr/bin/subparser-watchdog.sh >/dev/null 2>&1" >> "$CRON_TMP"
crontab "$CRON_TMP" 2>/dev/null || true
rm -f "$CRON_TMP"
/etc/init.d/cron restart >/dev/null 2>&1 || true

# Мягкая миграция UCI-конфига
set_default_uci() {
    local opt="$1"
    local val="$2"
    if [ -z "$(uci -q get subparser.settings.${opt})" ]; then
        uci -q set subparser.settings.${opt}="${val}"
    fi
}

set_default_uci "custom_hour" "3"
set_default_uci "custom_minute" "0"
set_default_uci "ping_threshold" "350"
set_default_uci "max_jitter" "150"
set_default_uci "max_best_nodes" "0"
set_default_uci "filter_ru" "1"
set_default_uci "per_section_config" "0"
set_default_uci "update_mode" "replace"
set_default_uci "sec_mode_main" "replace"
set_default_uci "sec_en_main" "1"
set_default_uci "override_fp" "as_is"
set_default_uci "allow_insecure_tls" "0"
set_default_uci "allow_local_diag" "0"
set_default_uci "allow_unstable_fallback" "0"
set_default_uci "stress_top" "40"
set_default_uci "hysteresis_pct" "10"
set_default_uci "update_branch" "$BRANCH"
set_default_uci "speedtest_proxy" "socks5h://127.0.0.1:4534"
set_default_uci "watchdog_enabled" "1"
set_default_uci "watchdog_interval" "5"
set_default_uci "watchdog_sub_limits" "1"
set_default_uci "watchdog_wan_check" "1"
set_default_uci "watchdog_down_wait" "10"
set_default_uci "watchdog_restart_wait" "15"
set_default_uci "watchdog_young_sec" "35"
set_default_uci "watchdog_cooldown" "45"
set_default_uci "watchdog_http_timeout" "8"
set_default_uci "watchdog_test_timeout" "4000"
set_default_uci "watchdog_group_timeout" "3000"
set_default_uci "watchdog_group_retry" "3"
set_default_uci "watchdog_max_run" "600"
set_default_uci "watchdog_test_url" "https://www.gstatic.com/generate_204"
set_default_uci "watchdog_restart_podkop" "1"
set_default_uci "watchdog_emergency" "1"
set_default_uci "watchdog_notify_recovery" "1"

if [ -z "$(uci -q get subparser.settings.target_section)" ]; then
    uci -q add_list subparser.settings.target_section='main'
fi

uci -q delete subparser.settings.cron_interval 2>/dev/null || true
uci -q delete subparser.settings.custom_interval_min 2>/dev/null || true
uci -q delete subparser.settings.cache_ttl 2>/dev/null || true
uci commit subparser

# Конфиг содержит токен бота — не даём читать его всем подряд.
chmod 600 /etc/config/subparser 2>/dev/null || true

printf '%s\n' "$BRANCH" > "$BRANCH_FILE"

if [ -n "$REMOTE_SHA" ]; then
    echo "$REMOTE_SHA" > "$COMMIT_FILE"
fi

echo "=== [5/5] Перезапуск служб и очистка кэша LuCI ==="
/etc/init.d/subparser restart >/dev/null 2>&1 || true

EN=$(uci -q get subparser.settings.enabled)
if [ "$EN" = "1" ]; then
    /etc/init.d/subparser-bot restart >/dev/null 2>&1 || true
else
    /etc/init.d/subparser-bot stop >/dev/null 2>&1 || true
fi

rm -rf /tmp/luci-indexcache /tmp/luci-modulecache
/etc/init.d/rpcd reload >/dev/null 2>&1 || true

rm -rf "$TMP_DIR"

NEW_VER=$(cat "$VERSION_FILE" 2>/dev/null || echo "2.1.9")
echo ""
echo "=========================================================="
echo " [OK] SubParser успешно обновлен до версии ${NEW_VER}!"
echo " Все подписки, настройки и токен бота сохранены."
echo "=========================================================="