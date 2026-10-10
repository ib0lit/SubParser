#!/bin/sh
set -e

PKG_NAME="luci-app-subparser"
PKG_VER="1.0.0"
PKG_REL="1"
OUT_DIR="./dist"

mkdir -p "$OUT_DIR"
APK_FILE="${PKG_NAME}-${PKG_VER}-r${PKG_REL}.apk"

echo "[*] Сборка пакета через abuild..."

docker run --rm -v "$(pwd)":/work -w /work alpine:edge sh -e -c '
  apk update && apk add abuild apk-tools sudo

  mkdir -p /root/.abuild /etc/apk/keys
  abuild-keygen -a -n
  cp /root/.abuild/*.pub /etc/apk/keys/

  BUILDDIR="/tmp/subparser_apk_build"
  OUTDIR="/tmp/out"
  rm -rf "$BUILDDIR" "$OUTDIR"
  mkdir -p "$BUILDDIR" "$OUTDIR"

  cat << "EOF" > "$BUILDDIR/'"$PKG_NAME"'.post-install"
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

  cat << "EOF" > "$BUILDDIR/'"$PKG_NAME"'.pre-deinstall"
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

  cat << EOF > "$BUILDDIR/APKBUILD"
pkgname="'"$PKG_NAME"'"
pkgver="'"$PKG_VER"'"
pkgrel="'"$PKG_REL"'"
pkgdesc="LuCI interface and proxy parser for Podkop"
url="https://github.com/ib0lit/SubParser"
arch="noarch"
license="MIT"
depends="python3 curl ca-certificates conntrack"
install="'"$PKG_NAME"'.post-install '"$PKG_NAME"'.pre-deinstall"
options="!check !openrc"

package() {
  mkdir -p "\$pkgdir"
  cp -a /work/root/* "\$pkgdir/"
  chmod 755 "\$pkgdir"/etc/init.d/*
  chmod 755 "\$pkgdir"/usr/bin/*
}
EOF

  cd "$BUILDDIR"
  export REPODEST="$OUTDIR"
  abuild -F -d

  # Копируем найденный файл пакета в dist
  BUILT_APK=$(find "$OUTDIR" -name "*.apk" -not -name "APKINDEX*" | head -n 1)
  if [ -z "$BUILT_APK" ]; then
    echo "ERROR: APK не найден в $OUTDIR"
    exit 1
  fi

  cp "$BUILT_APK" /work/'"$OUT_DIR/$APK_FILE"'
  rm -rf "$BUILDDIR" "$OUTDIR"
'

ls -lh "$OUT_DIR/$APK_FILE"