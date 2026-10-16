#!/bin/sh
set -e

REPO_USER="ib0lit"
REPO_NAME="SubParser"
BRANCH="main"
COMMIT_FILE="/etc/subparser_commit"
VERSION_FILE="/etc/subparser_version"

echo "=== [1/5] Проверка наличия обновлений ==="

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
            CURRENT_VER=$(cat "$VERSION_FILE" 2>/dev/null || echo "2.1.8")
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

curl -sL "https://github.com/${REPO_USER}/${REPO_NAME}/archive/refs/heads/${BRANCH}.tar.gz?nocache=$(date +%s)" | tar -xz -C "$TMP_DIR"
SRC_PATH="${TMP_DIR}/${REPO_NAME}-${BRANCH}/root"

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

if [ -z "$(uci -q get subparser.settings.target_section)" ]; then
    uci -q add_list subparser.settings.target_section='main'
fi

uci -q delete subparser.settings.cron_interval 2>/dev/null || true
uci -q delete subparser.settings.custom_interval_min 2>/dev/null || true
uci commit subparser

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

NEW_VER=$(cat "$VERSION_FILE" 2>/dev/null || echo "2.1.8")
echo ""
echo "=========================================================="
echo " [OK] SubParser успешно обновлен до версии ${NEW_VER}!"
echo " Все подписки, настройки и токен бота сохранены."
echo "=========================================================="