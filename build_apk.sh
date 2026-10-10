#!/bin/sh
set -e

PKG_NAME="luci-app-subparser"
PKG_VER="1.0.0"
PKG_REL="1"
OUT_DIR="./dist"

mkdir -p "$OUT_DIR"
APK_FILE="${PKG_NAME}-${PKG_VER}-r${PKG_REL}.apk"

# Подготовка локального сборочного каталога
BUILD_DIR="./.build_apk_tmp"
rm -rf "$BUILD_DIR"
mkdir -p "$BUILD_DIR"

cat << 'EOF' > "$BUILD_DIR/post-install"
#!/bin/sh
/etc/init.d/subparser enable >/dev/null 2>&1 || true
/etc/init.d/subparser-bot enable >/dev/null 2>&1 || true
CRON_TMP="/tmp/cron_subparser_apk.tmp"
crontab -l 2>/dev/null | grep -v "subparser-watchdog.sh" > "$CRON_TMP" || true
echo "*/5 * * * * /usr/bin/subparser-watchdog.sh >/dev/null 2>&1" >> "$CRON_TMP"
crontab "$CRON_TMP" 2>/dev/null || true
rm -f "$CRON_TMP"
/etc/init.d/cron restart >/dev/null 2>&1 || true
rm -rf /tmp/luci-indexcache /tmp/luci-modulecache
/etc/init.d/rpcd reload >/dev/null 2>&1 || true
exit 0
EOF

cat << 'EOF' > "$BUILD_DIR/pre-deinstall"
#!/bin/sh
/etc/init.d/subparser stop >/dev/null 2>&1 || true
/etc/init.d/subparser disable >/dev/null 2>&1 || true
/etc/init.d/subparser-bot stop >/dev/null 2>&1 || true
/etc/init.d/subparser-bot disable >/dev/null 2>&1 || true
killall -9 subparser.py subparser-bot.py subparser-watchdog.sh >/dev/null 2>&1 || true
CRON_TMP="/tmp/cron_subparser_prerm.tmp"
crontab -l 2>/dev/null | grep -v "subparser" > "$CRON_TMP" || true
crontab "$CRON_TMP" 2>/dev/null || true
rm -f "$CRON_TMP"
/etc/init.d/cron restart >/dev/null 2>&1 || true
exit 0
EOF

cp "$BUILD_DIR/post-install" "$BUILD_DIR/${PKG_NAME}.post-install"
cp "$BUILD_DIR/pre-deinstall" "$BUILD_DIR/${PKG_NAME}.pre-deinstall"

cat << EOF > "$BUILD_DIR/APKBUILD"
pkgname="${PKG_NAME}"
pkgver="${PKG_VER}"
pkgrel="${PKG_REL}"
pkgdesc="LuCI interface and proxy parser for Podkop"
url="https://github.com/ib0lit/SubParser"
arch="noarch"
license="MIT"
depends="python3 curl ca-certificates conntrack"
provides="/bin/sh"
install="${PKG_NAME}.post-install ${PKG_NAME}.pre-deinstall"
options="!check !openrc !autodeps"

package() {
  mkdir -p "\$pkgdir"
  cp -a /work/root/* "\$pkgdir/"
  chmod 755 "\$pkgdir"/etc/init.d/*
  chmod 755 "\$pkgdir"/usr/bin/*
}
EOF

# Скрипт сборщика внутри Alpine
cat << 'EOF' > "$BUILD_DIR/docker_build.sh"
#!/bin/sh
set -e
apk update && apk add abuild apk-tools sudo
mkdir -p /root/.abuild /etc/apk/keys
abuild-keygen -a -n
cp /root/.abuild/*.pub /etc/apk/keys/
cd /work/.build_apk_tmp
export REPODEST=/work/dist_tmp
abuild -F -d
EOF
chmod +x "$BUILD_DIR/docker_build.sh"

rm -rf "./dist_tmp"
mkdir -p "./dist_tmp"

docker run --rm -v "$(pwd)":/work -w /work alpine:edge /work/.build_apk_tmp/docker_build.sh

# Перемещаем готовый apk файл
find "./dist_tmp" -type f -name "*.apk" -not -name "APKINDEX*" -exec cp {} "$OUT_DIR/$APK_FILE" \;
rm -rf "$BUILD_DIR" "./dist_tmp"

ls -lh "$OUT_DIR/$APK_FILE"