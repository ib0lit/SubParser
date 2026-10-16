#!/bin/sh
set -e

REPO_USER="ib0lit"
REPO_NAME="SubParser"
BRANCH="main"
COMMIT_FILE="/etc/subparser_commit"
VERSION_FILE="/etc/subparser_version"

echo "=== [1/7] Проверка дискового пространства устройства ==="

MIN_KB_WITHOUT_PYTHON=20480  # ~20 MB
MIN_KB_WITH_PYTHON=4096      # ~4 MB

# 1. Надежный выбор точки монтирования постоянной памяти (/overlay или /)
TARGET_DIR="/"
if df -k /overlay 2>/dev/null | grep -q "overlay"; then
    TARGET_DIR="/overlay"
fi

# 2. Безопасное извлечение свободного места с защитой от переноса строк BusyBox df
FREE_KB=$(df -k "$TARGET_DIR" 2>/dev/null | tail -n 1 | awk '{if (NF==1) {getline; print $(NF-2)} else {print $(NF-2)}}')

# Защита от нечислового значения
case "$FREE_KB" in
    ''|*[!0-9]*) FREE_KB="" ;;
esac

if [ -z "$FREE_KB" ]; then
    echo "[!] Предупреждение: не удалось определить свободное место. Пропуск проверки."
else
    if command -v python3 >/dev/null 2>&1; then
        REQUIRED_KB=$MIN_KB_WITH_PYTHON
        REQ_MSG="4 MB (python3 уже установлен)"
    else
        REQUIRED_KB=$MIN_KB_WITHOUT_PYTHON
        REQ_MSG="20 MB (требуется установка python3 и библиотек)"
    fi

    FREE_MB=$((FREE_KB / 1024))
    echo "[*] Доступно памяти на ${TARGET_DIR}: ${FREE_MB} MB"

    if [ "$FREE_KB" -lt "$REQUIRED_KB" ]; then
        echo ""
        echo "=========================================================="
        echo " ❌ [ОШИБКА] Недостаточно свободного места на устройстве!"
        echo " Доступно: ${FREE_MB} MB"
        echo " Требуется минимум: ${REQ_MSG}"
        echo ""
        echo " Установка прервана во избежание переполнения памяти роутера."
        echo " Освободите место и повторите установку."
        echo "=========================================================="
        exit 1
    fi
    echo "[OK] Памяти достаточно для установки."
fi

echo "=== [2/7] Определение пакетного менеджера и зависимостей ==="
set +e

install_pkg() {
    local pkg="$1"
    if command -v apk >/dev/null 2>&1; then
        apk add "$pkg" >/dev/null 2>&1 || true
    elif command -v opkg >/dev/null 2>&1; then
        opkg install "$pkg" >/dev/null 2>&1 || true
    fi
}

if command -v apk >/dev/null 2>&1; then
    echo "[*] Обнаружен менеджер apk (OpenWrt 25+)"
    apk update
    apk add python3 curl ca-certificates
    apk add conntrack-tools >/dev/null 2>&1 || apk add conntrack >/dev/null 2>&1 || true
elif command -v opkg >/dev/null 2>&1; then
    echo "[*] Обнаружен менеджер opkg (OpenWrt 19-24)"
    opkg update
    opkg install python3 curl ca-certificates ca-bundle
    opkg install conntrack-tools >/dev/null 2>&1 || opkg install conntrack >/dev/null 2>&1 || true
else
    echo "[!] Предупреждение: пакетный менеджер не найден. Пропуск шага."
fi

set -e

echo "=== [3/7] Загрузка компонентов с GitHub ==="
TMP_DIR="/tmp/subparser-install"
rm -rf "$TMP_DIR"
mkdir -p "$TMP_DIR"

curl -sL "https://github.com/${REPO_USER}/${REPO_NAME}/archive/refs/heads/${BRANCH}.tar.gz?nocache=$(date +%s)" | tar -xz -C "$TMP_DIR"
SRC_PATH="${TMP_DIR}/${REPO_NAME}-${BRANCH}/root"

echo "=== [4/7] Развертывание системных файлов и конфигурации ==="
mkdir -p /etc/config /etc/init.d /usr/bin /usr/lib/subparser \
         /usr/share/luci/menu.d /usr/share/rpcd/acl.d /www/luci-static/resources/view /etc/hotplug.d/uci

if [ ! -f /etc/config/subparser ]; then
    cp -f "${SRC_PATH}/etc/config/subparser" /etc/config/subparser
else
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
fi

# Копирование модулей Python и системных утилит
rm -rf /usr/lib/subparser/*
cp -rf "${SRC_PATH}/usr/lib/subparser/"* /usr/lib/subparser/ 2>/dev/null || true

cp -f "${SRC_PATH}/etc/init.d/"* /etc/init.d/ 2>/dev/null || true
cp -f "${SRC_PATH}/usr/bin/"* /usr/bin/ 2>/dev/null || true
cp -f "${SRC_PATH}/usr/share/luci/menu.d/"* /usr/share/luci/menu.d/ 2>/dev/null || true
cp -f "${SRC_PATH}/usr/share/rpcd/acl.d/"* /usr/share/rpcd/acl.d/ 2>/dev/null || true
cp -f "${SRC_PATH}/www/luci-static/resources/view/"* /www/luci-static/resources/view/ 2>/dev/null || true
[ -f "${SRC_PATH}/etc/subparser_version" ] && cp -f "${SRC_PATH}/etc/subparser_version" /etc/subparser_version
[ -d "${SRC_PATH}/etc/hotplug.d/uci" ] && cp -f "${SRC_PATH}/etc/hotplug.d/uci/"* /etc/hotplug.d/uci/ 2>/dev/null || true

# Удаление устаревшего ACL-дубликата
rm -f /usr/share/rpcd/acl.d/subparser.json 2>/dev/null || true

echo "=== [5/7] Настройка прав доступа и компиляция ==="
chmod +x /etc/init.d/subparser /etc/init.d/subparser-bot 2>/dev/null || true
chmod +x /usr/bin/subparser* 2>/dev/null || true

# Предкомпиляция Python-файлов
python3 -m py_compile /usr/lib/subparser/*.py /usr/bin/subparser-bot.py /usr/bin/subparser.py >/dev/null 2>&1 || true

echo "=== [6/7] Регистрация задач и автозапуск ==="
CRON_TMP="/tmp/cron_subparser_inst.tmp"
crontab -l 2>/dev/null | grep -v "subparser" > "$CRON_TMP" || true
echo "*/5 * * * * /usr/bin/subparser-watchdog.sh >/dev/null 2>&1" >> "$CRON_TMP"
crontab "$CRON_TMP" 2>/dev/null || true
rm -f "$CRON_TMP"
/etc/init.d/cron restart >/dev/null 2>&1 || true

REMOTE_SHA=$(curl -sL \
  -H "User-Agent: OpenWrt-SubParser-Installer" \
  "https://api.github.com/repos/${REPO_USER}/${REPO_NAME}/commits/${BRANCH}" \
  | grep '"sha":' | head -n 1 | cut -d '"' -f 4)

if [ -n "$REMOTE_SHA" ]; then
    echo "$REMOTE_SHA" > "$COMMIT_FILE"
fi

/etc/init.d/subparser enable
/etc/init.d/subparser-bot enable

EN=$(uci -q get subparser.settings.enabled)
if [ "$EN" = "1" ]; then
    /etc/init.d/subparser start
    /etc/init.d/subparser-bot start
fi

echo "=== [7/7] Обновление кэша LuCI ==="
rm -rf /tmp/luci-indexcache /tmp/luci-modulecache
/etc/init.d/rpcd reload >/dev/null 2>&1 || true

rm -rf "$TMP_DIR"

echo ""
echo "=========================================================="
echo " [OK] SubParser успешно установлен!"
echo " Откройте веб-интерфейс: LuCI -> 'Службы' -> 'SubParser'"
echo "=========================================================="