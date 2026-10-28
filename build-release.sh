#!/bin/sh
# Собирает все артефакты релиза в dist/:
#   - IPK для opkg (OpenWrt <= 24.10)
#   - APK для apk-tools 3 (OpenWrt >= 25.12)
#   - портативный tar.gz с локальным установщиком
#   - SHA256SUMS
set -eu

cd "$(dirname "$0")"

rm -rf dist
mkdir -p dist

PKG_VERSION="$(cat root/etc/subparser_pkg_version 2>/dev/null | tr -d ' \t\r\n')"
[ -n "$PKG_VERSION" ] || PKG_VERSION="$(cat root/etc/subparser_version 2>/dev/null | tr -d ' \t\r\n')"
[ -n "$PKG_VERSION" ] || PKG_VERSION="0.0.0"
export PKG_VERSION

echo "=== SubParser $PKG_VERSION ==="
echo "    core: $(cat root/etc/subparser_version 2>/dev/null || echo unknown)"
echo

sh ./build_ipk.sh
echo
sh ./build_apk.sh
echo
sh ./build_archive.sh
echo

( cd dist && sha256sum * > SHA256SUMS )

echo "=== Готово: dist/ ==="
ls -lh dist
