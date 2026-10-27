#!/bin/sh
set -e

PKG_NAME="luci-app-subparser"
# Единый источник версии — root/etc/subparser_version
PKG_VERSION="$(cat root/etc/subparser_version 2>/dev/null | tr -d ' \t\r\n')"
[ -z "$PKG_VERSION" ] && PKG_VERSION="0.0.0"
PKG_VERSION="${PKG_VERSION}-1"

BUILD_DIR="/tmp/ipk_build_$$"
OUT_DIR="./dist"

rm -rf "$BUILD_DIR" "$OUT_DIR"
mkdir -p "$BUILD_DIR/data" "$BUILD_DIR/control" "$OUT_DIR"

echo "[*] Копирование дерева root..."
cp -a root/* "$BUILD_DIR/data/"

# Выставляем права на исполняемые файлы
chmod +x "$BUILD_DIR/data/usr/bin/"* "$BUILD_DIR/data/etc/init.d/"* 2>/dev/null || true

echo "[*] Генерация control-файлов пакета..."
cat << EOF > "$BUILD_DIR/control/control"
Package: $PKG_NAME
Version: $PKG_VERSION
Depends: python3, curl, ca-certificates, ca-bundle, conntrack-tools
Section: luci
Architecture: all
Maintainer: ib0lit
Description: LuCI interface and multi-threaded proxy parser for Podkop (SubParser)
EOF

cat << 'EOF' > "$BUILD_DIR/control/postinst"
#!/bin/sh
[ -n "${IPKG_INSTROOT}" ] && exit 0

/etc/init.d/subparser enable >/dev/null 2>&1 || true
/etc/init.d/subparser-bot enable >/dev/null 2>&1 || true
chmod 600 /etc/config/subparser 2>/dev/null || true

# Регистрация watchdog в cron
CRON_TMP="/tmp/cron_subparser_ipk.tmp"
crontab -l 2>/dev/null | grep -v "subparser-watchdog.sh" > "$CRON_TMP" || true
echo "*/5 * * * * /usr/bin/subparser-watchdog.sh >/dev/null 2>&1" >> "$CRON_TMP"
crontab "$CRON_TMP" 2>/dev/null || true
rm -f "$CRON_TMP"
/etc/init.d/cron restart >/dev/null 2>&1 || true

# Сброс кэша интерфейса LuCI
rm -rf /tmp/luci-indexcache /tmp/luci-modulecache
/etc/init.d/rpcd reload >/dev/null 2>&1 || true
exit 0
EOF
chmod 755 "$BUILD_DIR/control/postinst"

cat << 'EOF' > "$BUILD_DIR/control/prerm"
#!/bin/sh
[ -n "${IPKG_INSTROOT}" ] && exit 0

/etc/init.d/subparser stop >/dev/null 2>&1 || true
/etc/init.d/subparser disable >/dev/null 2>&1 || true
/etc/init.d/subparser-bot stop >/dev/null 2>&1 || true
/etc/init.d/subparser-bot disable >/dev/null 2>&1 || true

killall -9 subparser.py subparser-bot.py subparser-watchdog.sh >/dev/null 2>&1 || true

# Очистка cron
CRON_TMP="/tmp/cron_subparser_prerm.tmp"
crontab -l 2>/dev/null | grep -v "subparser" > "$CRON_TMP" || true
crontab "$CRON_TMP" 2>/dev/null || true
rm -f "$CRON_TMP"
/etc/init.d/cron restart >/dev/null 2>&1 || true
exit 0
EOF
chmod 755 "$BUILD_DIR/control/prerm"

echo "2.0" > "$BUILD_DIR/debian-binary"

echo "[*] Упаковка tar-архивов..."
tar -czf "$BUILD_DIR/data.tar.gz" -C "$BUILD_DIR/data" .
tar -czf "$BUILD_DIR/control.tar.gz" -C "$BUILD_DIR/control" .

IPK_FILE="${OUT_DIR}/${PKG_NAME}_${PKG_VERSION}_all.ipk"
echo "[*] Сборка итогового пакета: $IPK_FILE"
tar -czf "$IPK_FILE" -C "$BUILD_DIR" debian-binary control.tar.gz data.tar.gz

rm -rf "$BUILD_DIR"
echo "[OK] Пакет успешно создан: $IPK_FILE"