#!/bin/sh
# Сборка APK для apk-tools 3 (OpenWrt 25.12+).
#
# На системах без apk-tools 3 скрипт автоматически перезапускается в Alpine
# edge через Docker. В GitHub Actions и на Linux с установленным apk-tools 3
# сборка идёт напрямую.
set -eu

# Если apk-tools 3 нет — используем Docker, чтобы не тащить SDK OpenWrt.
if [ "${SUBPARSER_IN_DOCKER:-0}" != "1" ]; then
    # apk mkpkg --help печатает справку и завершается с ошибкой, поэтому проверяем вывод.
    if ! command -v apk >/dev/null 2>&1 \
        || ! apk mkpkg --help 2>&1 | grep -q 'Usage: apk mkpkg'; then
        if command -v docker >/dev/null 2>&1; then
            echo "[*] apk-tools 3 не найден, перезапуск сборки в alpine:edge..."
            exec docker run --rm \
                -e SUBPARSER_IN_DOCKER=1 \
                -e PKG_VERSION="${PKG_VERSION:-}" \
                -e PKG_RELEASE="${PKG_RELEASE:-1}" \
                -v "$PWD:/work" -w /work \
                alpine:edge sh ./build_apk.sh
        fi
        echo "[!] Нужен apk-tools 3 (команда 'apk mkpkg') или Docker." >&2
        exit 1
    fi
fi

PKG_NAME="luci-app-subparser"
ROOT_DIR="root"
OUT_DIR="dist"
PKG_RELEASE="${PKG_RELEASE:-1}"

PKG_VERSION="${PKG_VERSION:-$(cat "$ROOT_DIR/etc/subparser_pkg_version" 2>/dev/null | tr -d ' \t\r\n')}"
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

BUILD_DIR="$(mktemp -d "${TMPDIR:-/tmp}/subparser-apk.XXXXXX")"
trap 'rm -rf "$BUILD_DIR"' EXIT INT TERM

DATA_DIR="$BUILD_DIR/data"
SCRIPTS_DIR="$BUILD_DIR/scripts"
mkdir -p "$DATA_DIR" "$SCRIPTS_DIR" "$OUT_DIR"

echo "[*] Копирование дерева $ROOT_DIR..."
cp -a "$ROOT_DIR/." "$DATA_DIR/"

find "$DATA_DIR" -type d -name '__pycache__' -prune -exec rm -rf {} + 2>/dev/null || true
find "$DATA_DIR" -type f -name '*.pyc' -delete 2>/dev/null || true

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
chmod 600 "$DATA_DIR/etc/config/subparser"

# apk mkpkg сохраняет uid/gid файлов, поэтому в пакете должен быть root:root.
if ! chown -R 0:0 "$DATA_DIR" 2>/dev/null; then
    echo "[!] Не удалось выставить владельца root:root. Запустите сборку через Docker или от root." >&2
    exit 1
fi

# apk-tools 3 хранит conffiles в служебных файлах пакета (так делает и
# OpenWrt package-pack.mk). Благодаря этому конфиг не затирается при upgrade.
mkdir -p "$DATA_DIR/lib/apk/packages"
# OpenWrt-хелперы default_postinst/default_prerm читают список файлов пакета,
# чтобы включать/останавливать init-скрипты. Формируем его до conffiles —
# так же, как это делает OpenWrt package-pack.mk.
(
    cd "$DATA_DIR"
    find . \( -type f -o -type l \) | sed 's|^\./|/|' | sort
) > "$DATA_DIR/lib/apk/packages/${PKG_NAME}.list"

printf '%s\n' '/etc/config/subparser' > "$DATA_DIR/lib/apk/packages/${PKG_NAME}.conffiles"
CONF_SUM="$(sha256sum "$DATA_DIR/etc/config/subparser" | awk '{print $1}')"
printf '%s %s\n' '/etc/config/subparser' "$CONF_SUM" \
    > "$DATA_DIR/lib/apk/packages/${PKG_NAME}.conffiles_static"

# Фиксируем mtime, чтобы apk mkpkg собрал воспроизводимый пакет.
if [ -z "${SOURCE_DATE_EPOCH:-}" ]; then
    SOURCE_DATE_EPOCH=$(git log -1 --format=%ct 2>/dev/null || true)
fi
case "${SOURCE_DATE_EPOCH:-}" in
    ''|*[!0-9]*) SOURCE_DATE_EPOCH=0 ;;
esac
export SOURCE_DATE_EPOCH
find "$DATA_DIR" -exec touch -h -d "@$SOURCE_DATE_EPOCH" {} + 2>/dev/null || true

cat > "$SCRIPTS_DIR/post-install" <<'EOF'
#!/bin/sh
[ "${IPKG_NO_SCRIPT}" = "1" ] && exit 0
[ -s "${IPKG_INSTROOT}/lib/functions.sh" ] || exit 0
. "${IPKG_INSTROOT}/lib/functions.sh"
export root="${IPKG_INSTROOT}"
export pkgname="luci-app-subparser"
default_postinst
[ -n "${IPKG_INSTROOT}" ] && exit 0

chmod 600 /etc/config/subparser 2>/dev/null || true

/etc/init.d/subparser enable >/dev/null 2>&1 || true
/etc/init.d/subparser-bot enable >/dev/null 2>&1 || true

CRON_TMP="/tmp/cron_subparser_pkg.tmp"
crontab -l 2>/dev/null | grep -v "subparser-watchdog.sh" > "$CRON_TMP" || true
echo "*/5 * * * * /usr/bin/subparser-watchdog.sh >/dev/null 2>&1" >> "$CRON_TMP"
crontab "$CRON_TMP" 2>/dev/null || true
rm -f "$CRON_TMP"
/etc/init.d/cron restart >/dev/null 2>&1 || true

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

cat > "$SCRIPTS_DIR/pre-deinstall" <<'EOF'
#!/bin/sh
[ -s "${IPKG_INSTROOT}/lib/functions.sh" ] || exit 0
. "${IPKG_INSTROOT}/lib/functions.sh"
export root="${IPKG_INSTROOT}"
export pkgname="luci-app-subparser"
default_prerm
[ -n "${IPKG_INSTROOT}" ] && exit 0

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

# При upgrade apk вызывает pre-upgrade/post-upgrade, а не install/deinstall.
# На upgrade службы нужно остановить, но не отключать — иначе после сбоя
# post-upgrade они останутся выключенными.
cat > "$SCRIPTS_DIR/pre-upgrade" <<'EOF'
#!/bin/sh
[ -s "${IPKG_INSTROOT}/lib/functions.sh" ] || exit 0
. "${IPKG_INSTROOT}/lib/functions.sh"
export root="${IPKG_INSTROOT}"
export pkgname="luci-app-subparser"
export PKG_UPGRADE=1
default_prerm
[ -n "${IPKG_INSTROOT}" ] && exit 0

killall -9 subparser.py subparser-bot.py subparser-watchdog.sh >/dev/null 2>&1 || true
nft delete table inet bot_block >/dev/null 2>&1 || true
exit 0
EOF
cp "$SCRIPTS_DIR/post-install" "$SCRIPTS_DIR/post-upgrade"
chmod 755 "$SCRIPTS_DIR"/*

APK_VERSION="${PKG_VERSION}-r${PKG_RELEASE}"
APK_FILE="$OUT_DIR/${PKG_NAME}-${APK_VERSION}.apk"
OUT_ABS="$(cd "$OUT_DIR" && pwd)"

echo "[*] Сборка APK через apk mkpkg: $APK_FILE"
apk mkpkg \
    --info "name:${PKG_NAME}" \
    --info "version:${APK_VERSION}" \
    --info "description:LuCI interface and multi-threaded proxy parser for Podkop (SubParser)" \
    --info "arch:noarch" \
    --info "license:MIT" \
    --info "origin:${PKG_NAME}" \
    --info "url:https://github.com/ib0lit/SubParser" \
    --info "maintainer:ib0lit" \
    --info "depends:python3 curl ca-bundle conntrack luci-base" \
    --info "tags:openwrt:section=luci" \
    --script "post-install:$SCRIPTS_DIR/post-install" \
    --script "post-upgrade:$SCRIPTS_DIR/post-upgrade" \
    --script "pre-upgrade:$SCRIPTS_DIR/pre-upgrade" \
    --script "pre-deinstall:$SCRIPTS_DIR/pre-deinstall" \
    --files "$DATA_DIR" \
    --output "$OUT_ABS/${PKG_NAME}-${APK_VERSION}.apk"

echo "[OK] APK создан: $APK_FILE ($(du -h "$APK_FILE" | awk '{print $1}'))"
echo "     package: $APK_VERSION, core: $(cat "$ROOT_DIR/etc/subparser_version" 2>/dev/null || echo unknown)"
