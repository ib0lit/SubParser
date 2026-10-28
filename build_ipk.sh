#!/bin/sh
# Сборка IPK для opkg (OpenWrt 19.x - 24.10).
set -eu

PKG_NAME="luci-app-subparser"
ROOT_DIR="root"
OUT_DIR="dist"
PKG_RELEASE="${PKG_RELEASE:-1}"

PKG_VERSION="${PKG_VERSION:-$(cat "$ROOT_DIR/etc/subparser_pkg_version" 2>/dev/null | tr -d ' \t\r\n')}"
# Совместимость: если отдельного файла версии пакета ещё нет, берём версию ядра.
[ -n "$PKG_VERSION" ] || PKG_VERSION="$(cat "$ROOT_DIR/etc/subparser_version" 2>/dev/null | tr -d ' \t\r\n')"
[ -n "$PKG_VERSION" ] || PKG_VERSION="0.0.0"

case "$PKG_VERSION" in
    *[!A-Za-z0-9._+~-]*)
        echo "[!] Недопустимая версия пакета: $PKG_VERSION" >&2
        exit 1
        ;;
esac
case "$PKG_RELEASE" in
    ''|*[!0-9]*)
        echo "[!] Недопустимый номер релиза: $PKG_RELEASE" >&2
        exit 1
        ;;
esac

[ -d "$ROOT_DIR" ] || {
    echo "[!] Не найден каталог $ROOT_DIR (запускайте скрипт из корня репозитория)." >&2
    exit 1
}

BUILD_DIR="$(mktemp -d "${TMPDIR:-/tmp}/subparser-ipk.XXXXXX")"
trap 'rm -rf "$BUILD_DIR"' EXIT INT TERM

DATA_DIR="$BUILD_DIR/data"
CONTROL_DIR="$BUILD_DIR/control"
mkdir -p "$DATA_DIR" "$CONTROL_DIR" "$OUT_DIR"

echo "[*] Копирование дерева $ROOT_DIR..."
cp -a "$ROOT_DIR/." "$DATA_DIR/"

# Python-кэш не должен попадать в пакет: он привязан к версии Python и архитектуре.
find "$DATA_DIR" -type d -name '__pycache__' -prune -exec rm -rf {} + 2>/dev/null || true
find "$DATA_DIR" -type f -name '*.pyc' -delete 2>/dev/null || true

# Базовые права: файлы только для чтения, каталоги 755.
find "$DATA_DIR" -type d -exec chmod 755 {} +
find "$DATA_DIR" -type f -exec chmod 644 {} +

chmod 755 \
    "$DATA_DIR/usr/bin/subparser.py" \
    "$DATA_DIR/usr/bin/subparser-bot.py" \
    "$DATA_DIR/usr/bin/subparser-helper.sh" \
    "$DATA_DIR/usr/bin/subparser-watchdog.sh" \
    "$DATA_DIR/etc/init.d/subparser" \
    "$DATA_DIR/etc/init.d/subparser-bot" \
    "$DATA_DIR/etc/hotplug.d/uci/99-subparser"
# В конфиге хранится токен Telegram-бота.
chmod 600 "$DATA_DIR/etc/config/subparser"

# Фиксируем mtime, чтобы повторная сборка из того же коммита давала тот же пакет.
if [ -z "${SOURCE_DATE_EPOCH:-}" ]; then
    SOURCE_DATE_EPOCH=$(git log -1 --format=%ct 2>/dev/null || true)
fi
case "${SOURCE_DATE_EPOCH:-}" in
    ''|*[!0-9]*) SOURCE_DATE_EPOCH=0 ;;
esac
export SOURCE_DATE_EPOCH
find "$DATA_DIR" -exec touch -h -d "@$SOURCE_DATE_EPOCH" {} + 2>/dev/null || true

INSTALLED_SIZE="$(du -sk "$DATA_DIR" | awk '{print $1}')"

echo "[*] Генерация control-файлов..."
cat > "$CONTROL_DIR/control" <<EOF
Package: $PKG_NAME
Version: $PKG_VERSION-$PKG_RELEASE
Depends: python3, curl, ca-bundle, conntrack, luci-base
Section: luci
Priority: optional
Architecture: all
Installed-Size: $INSTALLED_SIZE
Maintainer: ib0lit
Source: https://github.com/ib0lit/SubParser
License: MIT
Description: LuCI interface and multi-threaded proxy parser for Podkop (SubParser)
EOF

# Конфиг с подписками и токеном не перезаписывается при обновлении пакета.
printf '%s\n' '/etc/config/subparser' > "$CONTROL_DIR/conffiles"

cat > "$CONTROL_DIR/postinst" <<'EOF'
#!/bin/sh
[ "${IPKG_NO_SCRIPT}" = "1" ] && exit 0
[ -s "${IPKG_INSTROOT}/lib/functions.sh" ] || exit 0
. "${IPKG_INSTROOT}/lib/functions.sh"
default_postinst "$0" "$@"
# При сборке образа (IPKG_INSTROOT) файлы только раскладываются.
[ -n "${IPKG_INSTROOT}" ] && exit 0

chmod 600 /etc/config/subparser 2>/dev/null || true

/etc/init.d/subparser enable >/dev/null 2>&1 || true
/etc/init.d/subparser-bot enable >/dev/null 2>&1 || true

# Watchdog должен работать всегда, даже если автопарсинг выключен.
CRON_TMP="/tmp/cron_subparser_pkg.tmp"
crontab -l 2>/dev/null | grep -v "subparser-watchdog.sh" > "$CRON_TMP" || true
echo "*/5 * * * * /usr/bin/subparser-watchdog.sh >/dev/null 2>&1" >> "$CRON_TMP"
crontab "$CRON_TMP" 2>/dev/null || true
rm -f "$CRON_TMP"
/etc/init.d/cron restart >/dev/null 2>&1 || true

# Пересоздаём расписание из UCI и запускаем бота только если служба включена.
/etc/init.d/subparser restart >/dev/null 2>&1 || true
if [ "$(uci -q get subparser.settings.enabled)" = "1" ]; then
    /etc/init.d/subparser-bot restart >/dev/null 2>&1 || true
else
    /etc/init.d/subparser-bot stop >/dev/null 2>&1 || true
fi

rm -rf /tmp/luci-indexcache /tmp/luci-modulecache
/etc/init.d/rpcd reload >/dev/null 2>&1 || true
exit 0
EOF

cat > "$CONTROL_DIR/prerm" <<'EOF'
#!/bin/sh
[ "${IPKG_NO_SCRIPT}" = "1" ] && exit 0
[ -s "${IPKG_INSTROOT}/lib/functions.sh" ] || exit 0
. "${IPKG_INSTROOT}/lib/functions.sh"
default_prerm "$0" "$@"
[ -n "${IPKG_INSTROOT}" ] && exit 0
# При обновлении службы останавливает сам opkg; отключать их не нужно.
[ "${PKG_UPGRADE:-0}" = "1" ] && exit 0

/etc/init.d/subparser-bot stop >/dev/null 2>&1 || true
/etc/init.d/subparser-bot disable >/dev/null 2>&1 || true
/etc/init.d/subparser stop >/dev/null 2>&1 || true
/etc/init.d/subparser disable >/dev/null 2>&1 || true

killall -9 subparser.py subparser-bot.py subparser-watchdog.sh >/dev/null 2>&1 || true
nft delete table inet bot_block >/dev/null 2>&1 || true

CRON_TMP="/tmp/cron_subparser_pkg.tmp"
crontab -l 2>/dev/null \
    | grep -v "subparser.py" \
    | grep -v "subparser-helper.sh bg_sync" \
    | grep -v "subparser-watchdog.sh" > "$CRON_TMP" || true
crontab "$CRON_TMP" 2>/dev/null || true
rm -f "$CRON_TMP"
/etc/init.d/cron restart >/dev/null 2>&1 || true

rm -rf /tmp/luci-indexcache /tmp/luci-modulecache
rm -rf /var/run/subparser.lock /tmp/subparser_bot_state.json /tmp/bot_blocked_macs.json 2>/dev/null || true
exit 0
EOF

chmod 755 "$CONTROL_DIR/postinst" "$CONTROL_DIR/prerm"
find "$CONTROL_DIR" -exec touch -h -d "@$SOURCE_DATE_EPOCH" {} + 2>/dev/null || true
printf '2.0\n' > "$BUILD_DIR/debian-binary"

echo "[*] Упаковка tar-архивов..."
( cd "$DATA_DIR" && tar -czf "$BUILD_DIR/data.tar.gz" . )
( cd "$CONTROL_DIR" && tar -czf "$BUILD_DIR/control.tar.gz" . )

# Внешний tar тоже должен содержать фиксированные mtime.
touch -h -d "@$SOURCE_DATE_EPOCH" \
    "$BUILD_DIR/debian-binary" "$BUILD_DIR/control.tar.gz" "$BUILD_DIR/data.tar.gz"

IPK_FILE="$OUT_DIR/${PKG_NAME}_${PKG_VERSION}-${PKG_RELEASE}_all.ipk"
OUT_ABS="$(cd "$OUT_DIR" && pwd)"
echo "[*] Сборка итогового пакета: $IPK_FILE"
( cd "$BUILD_DIR" && tar -czf "$OUT_ABS/${PKG_NAME}_${PKG_VERSION}-${PKG_RELEASE}_all.ipk" \
    debian-binary control.tar.gz data.tar.gz )

echo "[OK] IPK создан: $IPK_FILE ($(du -h "$IPK_FILE" | awk '{print $1}'))"
echo "     package: $PKG_VERSION-$PKG_RELEASE, core: $(cat "$ROOT_DIR/etc/subparser_version" 2>/dev/null || echo unknown)"
