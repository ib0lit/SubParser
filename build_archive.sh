#!/bin/sh
# Портативный архив для установки на любой роутер без пакетного менеджера:
#   sh install.sh --local
set -eu

ROOT_DIR="root"
OUT_DIR="dist"
PKG_VERSION="${PKG_VERSION:-$(cat "$ROOT_DIR/etc/subparser_pkg_version" 2>/dev/null | tr -d ' \t\r\n')}"
[ -n "$PKG_VERSION" ] || PKG_VERSION="$(cat "$ROOT_DIR/etc/subparser_version" 2>/dev/null | tr -d ' \t\r\n')"
[ -n "$PKG_VERSION" ] || PKG_VERSION="0.0.0"
CORE_VERSION="$(cat "$ROOT_DIR/etc/subparser_version" 2>/dev/null | tr -d ' \t\r\n')"
[ -n "$CORE_VERSION" ] || CORE_VERSION="unknown"

BUILD_DIR="$(mktemp -d "${TMPDIR:-/tmp}/subparser-archive.XXXXXX")"
trap 'rm -rf "$BUILD_DIR"' EXIT INT TERM

STAGE="$BUILD_DIR/subparser-$PKG_VERSION"
mkdir -p "$STAGE" "$OUT_DIR"

echo "[*] Подготовка портативного архива SubParser $PKG_VERSION..."
cp -a "$ROOT_DIR" "$STAGE/root"
find "$STAGE/root" -type d -name '__pycache__' -prune -exec rm -rf {} + 2>/dev/null || true
find "$STAGE/root" -type f -name '*.pyc' -delete 2>/dev/null || true

cp install.sh update.sh uninstall.sh README.md "$STAGE/"

COMMIT=""
if command -v git >/dev/null 2>&1 && [ -d .git ]; then
    COMMIT="$(git rev-parse HEAD 2>/dev/null || true)"
fi

cat > "$STAGE/RELEASE" <<EOF
package_version=$PKG_VERSION
core_version=$CORE_VERSION
commit=$COMMIT
built_at=$(date -u '+%Y-%m-%dT%H:%M:%SZ')
EOF
if [ -n "$COMMIT" ]; then
    printf '%s\n' "$COMMIT" > "$STAGE/COMMIT"
fi

ARCHIVE="$OUT_DIR/subparser-$PKG_VERSION.tar.gz"
echo "[*] Упаковка: $ARCHIVE"
tar -czf "$ARCHIVE" -C "$BUILD_DIR" "subparser-$PKG_VERSION"

echo "[OK] Архив создан: $ARCHIVE ($(du -h "$ARCHIVE" | awk '{print $1}'))"
echo "     package: $PKG_VERSION, core: $CORE_VERSION"
