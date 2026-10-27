#!/bin/sh
# ============================================================================
#  install-slim.sh — замена sing-box на slim-сборку sing-box-extended
#  для роутеров с малым объёмом флеш-памяти
#
#  Репозиторий: https://github.com/ib0lit/SubParser (каталог slim/)
#  Релиз:       slim-v1.14.3-extended-2.7.3
#  Сборка:      sing-box-extended 1.14.3 (shtorm-7/sing-box-extended, extended)
#  Теги:        with_quic,with_utls,with_clash_api,badlinkname,tfogo_checklinkname0
#
#  Использование:
#    sh install-slim.sh                  # установить
#    sh install-slim.sh --check          # только проверить, ничего не менять
#    sh install-slim.sh --yes            # без вопроса-подтверждения
#    sh install-slim.sh --force          # игнорировать предупреждения (размер/check)
#    sh install-slim.sh --skip-hash      # не проверять SHA256 (для своих сборок)
#    sh install-slim.sh --rollback FILE  # восстановить бинарник из FILE
#    sh install-slim.sh --url URL        # другой базовый URL с архивами
#
#  Одной командой с GitHub:
#    sh -c "$(curl -fsSL https://raw.githubusercontent.com/ib0lit/SubParser/main/slim/install-slim.sh)"
# ============================================================================

set -eu

VERSION_STR="1.14.3-extended-slim"
REPO="ib0lit/SubParser"
BRANCH="${SUBPARSER_BRANCH:-main}"
RELEASE_TAG="slim-v1.14.3-extended-2.7.3"
BASE_URL="${SLIM_URL:-https://github.com/${REPO}/releases/download/${RELEASE_TAG}}"

TARGET="/usr/bin/sing-box"
TMP_BIN="/tmp/sing-box-slim"
TMP_TGZ="/tmp/sing-box-slim.tar.gz"
TMP_SUMS="/tmp/sing-box-slim.sha256"
TMP_LOG="/tmp/sing-box-slim-check.log"

DO_CHECK=0
ASSUME_YES=0
FORCE=0
SKIP_HASH=0
ROLLBACK=""

die()  { printf '\033[31mОШИБКА:\033[0m %s\n' "$*" >&2; exit 1; }
info() { printf '\033[36m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[33mВНИМАНИЕ:\033[0m %s\n' "$*"; }
ok()   { printf '\033[32m  ✓\033[0m %s\n' "$*"; }

usage() { sed -n '2,26p' "$0" 2>/dev/null | sed 's/^# \{0,1\}//' || true; exit 0; }

while [ $# -gt 0 ]; do
    case "$1" in
        --check)     DO_CHECK=1 ;;
        --yes|-y)    ASSUME_YES=1 ;;
        --force)     FORCE=1 ;;
        --skip-hash) SKIP_HASH=1 ;;
        --rollback)  shift; ROLLBACK="${1:-}" ;;
        --url)       shift; BASE_URL="${1:-}" ;;
        -h|--help)   usage ;;
        *)           die "неизвестный аргумент: $1 (см. --help)" ;;
    esac
    shift
done

cleanup() { rm -f "$TMP_BIN" "$TMP_TGZ" "$TMP_SUMS" "$TMP_LOG"; }
trap cleanup EXIT INT TERM

[ "$(id -u)" = "0" ] || die "запускать нужно от root"

# ---------------------------------------------------------------- архитектура
case "$(uname -m)" in
    x86_64|amd64)        ARCH=amd64 ;;
    i386|i486|i586|i686) ARCH=386 ;;
    aarch64|arm64)       ARCH=arm64 ;;
    armv8l|armv7l)       ARCH=armv7 ;;
    armv6l)              ARCH=armv6 ;;
    armv5tel|armv5l)     ARCH=armv5 ;;
    mips)                ARCH=mips ;;
    mipsel)              ARCH=mipsle ;;
    mips64)              ARCH=mips64 ;;
    mips64el)            ARCH=mips64le ;;
    riscv64)             ARCH=riscv64 ;;
    *) die "неизвестная архитектура '$(uname -m)'. Поддерживаются: amd64, 386, arm64, armv7, armv6, armv5, mips, mipsle, mips64, mips64le, riscv64" ;;
esac

# эталонные контрольные суммы архивов (см. slim/sha256sums.txt)
case "$ARCH" in
    amd64)    EXPECT_SHA=4217216472c070331c3f2ae2d70f46071f2e9eb579cd296663dc5d92a65d53ed ;;
    386)      EXPECT_SHA=d5150ba980e693551b573b75a404fafd17993961fa719523179769f00883181b ;;
    arm64)    EXPECT_SHA=61f9fcc99b12efdcf6e4dbdec5b2037a90ccea2448844eb30ab74265c8542980 ;;
    armv7)    EXPECT_SHA=08ad5e2d442d654b22ddf4028d6228b6a23a9b912a17aae8e46ca02f08c90bb7 ;;
    armv6)    EXPECT_SHA=f67be7c2426b77e0b15b2d02bb73d3254fd886128d572730f6d47dccbd422ee9 ;;
    armv5)    EXPECT_SHA=a2699f92a3339e9b0dc4e7cd045564eadac1def795c15de0471d82378b568261 ;;
    mips)     EXPECT_SHA=f892d73683e53c43e20fc5bcb34d4d08911a8176fc00e88a1f11e654dbd92245 ;;
    mipsle)   EXPECT_SHA=4ca983e3fe0d09d235bb3b20501b076f9994d5c0b9ac546cfea018730ad69eb4 ;;
    mips64)   EXPECT_SHA=65032bcc62d55734aa6c906182f23e9e30a9dd070ba23c9f4e54ed600e889dfa ;;
    mips64le) EXPECT_SHA=5be8aa8d32a92f49ec45d8f9157757f41bb003d3775a24652f0c2ccceb95de39 ;;
    riscv64)  EXPECT_SHA=ad270635632bee2a6ba7524e7570c20cb2780da8a519f6b9fa37ee5c453ad1e2 ;;
    *)        EXPECT_SHA="" ;;
esac

TGZ_NAME="sing-box-slim-${ARCH}.tar.gz"
BIN_NAME="sing-box-slim-${ARCH}"

info "архитектура: $(uname -m) → ${ARCH}"

# ------------------------------------------------------------------- сервис
if [ -x /etc/init.d/podkop ]; then
    SERVICE="podkop"
    CFG="$(uci -q get podkop.settings.config_path 2>/dev/null || true)"
    [ -n "$CFG" ] || CFG="/etc/sing-box/config.json"
elif [ -x /etc/init.d/sing-box ]; then
    SERVICE="sing-box"
    CFG="/etc/sing-box/config.json"
else
    SERVICE=""
    CFG="/etc/sing-box/config.json"
    warn "не найден /etc/init.d/podkop или /etc/init.d/sing-box — сервис не будет перезапущен"
fi
[ -n "$SERVICE" ] && info "сервис: ${SERVICE}, конфиг: ${CFG}"

# ------------------------------------------------------------------ откат
if [ -n "$ROLLBACK" ]; then
    [ -f "$ROLLBACK" ] || die "файл для отката не найден: $ROLLBACK"
    info "откат на $ROLLBACK ($(wc -c < "$ROLLBACK") Б)"
    if [ "$ASSUME_YES" -ne 1 ]; then
        printf 'Продолжить? [y/N] '
        read -r a
        case "$a" in y|Y|yes|YES) ;; *) die "отменено" ;; esac
    fi
    if [ -n "$SERVICE" ]; then info "останавливаю ${SERVICE}"; service "$SERVICE" stop || true; sleep 2; fi
    cat "$ROLLBACK" > "$TARGET"
    chmod 755 "$TARGET"
    sync
    ok "установлено: $(wc -c < "$TARGET") Б"
    [ -n "$SERVICE" ] && { service "$SERVICE" start || true; sleep 5; }
    sing-box version
    exit 0
fi

# ------------------------------------------------------------- архив с сборкой
SRC_DIR="$(dirname "$0" 2>/dev/null || echo .)"
LOCAL_TGZ=""
[ -n "${SLIM_DIR:-}" ] && [ -f "${SLIM_DIR}/${TGZ_NAME}" ] && LOCAL_TGZ="${SLIM_DIR}/${TGZ_NAME}"
if [ -z "$LOCAL_TGZ" ] && [ -f "${SRC_DIR}/${TGZ_NAME}" ]; then
    LOCAL_TGZ="${SRC_DIR}/${TGZ_NAME}"
fi

if [ -n "$LOCAL_TGZ" ]; then
    info "архив найден локально: $LOCAL_TGZ"
    cp "$LOCAL_TGZ" "$TMP_TGZ"
    if [ -f "${SRC_DIR}/sha256sums.txt" ]; then cp "${SRC_DIR}/sha256sums.txt" "$TMP_SUMS"; fi
    if [ -n "${SLIM_DIR:-}" ] && [ -f "${SLIM_DIR}/sha256sums.txt" ]; then cp "${SLIM_DIR}/sha256sums.txt" "$TMP_SUMS"; fi
else
    command -v curl >/dev/null 2>&1 || die "нужен curl (apk add curl / opkg install curl), либо положите архив рядом со скриптом"
    info "скачиваю ${TGZ_NAME} ..."
    curl -fsSL -o "$TMP_TGZ" "${BASE_URL}/${TGZ_NAME}" || die "не удалось скачать ${BASE_URL}/${TGZ_NAME}"
fi

[ -s "$TMP_TGZ" ] || die "архив пустой"

# -------------------------------------------------------------- контроль суммы
if [ "$SKIP_HASH" -eq 1 ]; then
    warn "проверка SHA256 отключена (--skip-hash)"
else
    ACTUAL="$(sha256sum "$TMP_TGZ" | awk '{print $1}')"
    if [ -n "$EXPECT_SHA" ]; then
        [ "$ACTUAL" = "$EXPECT_SHA" ] || die "SHA256 не совпал:
  ожидалось: $EXPECT_SHA
  получено : $ACTUAL
Архив повреждён или подменён. Скачайте заново."
        ok "sha256 совпал"
    else
        EXPECT="$(awk -v f="$TGZ_NAME" '$2 == f || $2 == "*"f {print $1}' "$TMP_SUMS" 2>/dev/null || true)"
        if [ -n "$EXPECT" ]; then
            [ "$ACTUAL" = "$EXPECT" ] || die "SHA256 не совпал с sha256sums.txt"
            ok "sha256 совпал (sha256sums.txt)"
        else
            warn "нет эталонной суммы для ${ARCH} — проверка пропущена"
        fi
    fi
fi

# -------------------------------------------------------------- распаковка
info "распаковываю"
tar -xzf "$TMP_TGZ" -C /tmp || die "не удалось распаковать архив"
[ -f "/tmp/${BIN_NAME}" ] || die "в архиве нет ${BIN_NAME}"
cp "/tmp/${BIN_NAME}" "$TMP_BIN"
rm -f "/tmp/${BIN_NAME}"
chmod 755 "$TMP_BIN"

GOT_VERSION="$("$TMP_BIN" version 2>/dev/null | head -n1 | awk '{print $3}')"
[ "$GOT_VERSION" = "$VERSION_STR" ] || warn "версия в архиве: $GOT_VERSION (ожидалась $VERSION_STR)"
ok "$("$TMP_BIN" version | head -n1)"

# --------------------------------------------------------- проверка конфигурации
if [ -f "$CFG" ]; then
    info "проверяю конфиг новым бинарником: $CFG"
    if "$TMP_BIN" check -c "$CFG" >"$TMP_LOG" 2>&1; then
        ok "конфиг валиден"
    else
        grep -vi 'deprecated' "$TMP_LOG" | tail -n 10
        if grep -q 'rulesets' "$TMP_LOG" 2>/dev/null && [ "$SERVICE" = "podkop" ]; then
            die "в конфиге отсутствуют Podkop-rulesets в /tmp/sing-box/rulesets.
Podkop не запущен. Запустите его, дождитесь загрузки списков и повторите:
  service podkop start && sleep 60"
        fi
        if [ "$FORCE" -eq 1 ]; then
            warn "check не прошёл, но указан --force — продолжаю"
        else
            die "конфиг не прошёл проверку этой сборкой. Ничего не изменено.
Если в конфиге есть функция вне slim-сборки (wireguard, tailscale,
openvpn, cloudflared, TUN/gVisor) — нужна пересборка с этим тегом."
        fi
    fi
else
    warn "конфиг $CFG не найден — проверка пропущена"
fi

if [ "$DO_CHECK" -eq 1 ]; then
    info "--check: проверка пройдена, изменения не вносились"
    exit 0
fi

# ------------------------------------------------------------------ размеры
OLD_SIZE=0
[ -f "$TARGET" ] && OLD_SIZE="$(wc -c < "$TARGET")"
NEW_SIZE="$(wc -c < "$TMP_BIN")"

printf '\n'
info "текущий бинарник : ${TARGET} (${OLD_SIZE} Б)"
info "новый бинарник   : ${NEW_SIZE} Б"

if [ -f "$TARGET" ] && [ "$OLD_SIZE" -lt "$NEW_SIZE" ] && [ "$FORCE" -ne 1 ]; then
    die "новый бинарник больше текущего — может не хватить места в /overlay.
Освободите место (apk del ненужных пакетов) или запустите с --force"
fi

DF_FREE="$(df -k /overlay 2>/dev/null | awk 'NR==2 {print $4}')"
[ -n "$DF_FREE" ] && info "свободно в /overlay: $((DF_FREE / 1024)) МБ"

# ---------------------------------------------------------- запущен ли сервис
WAS_RUNNING=0
if pgrep -f 'sing-box run' >/dev/null 2>&1; then WAS_RUNNING=1; fi

# ------------------------------------------------------------------ бэкап
printf '\n'
warn "Сохраните текущий бинарник на ПК — на роутере для копии может не быть места:"
printf '    scp root@%s:%s ./sing-box-orig\n' "$(uci -q get network.lan.ipaddr 2>/dev/null | cut -d/ -f1 || echo 192.168.1.1)" "$TARGET"
printf '\n'

if [ "$ASSUME_YES" -ne 1 ]; then
    printf 'Заменить sing-box на slim-сборку? [y/N] '
    read -r ANSWER
    case "$ANSWER" in y|Y|yes|YES) ;; *) die "отменено пользователем" ;; esac
fi

# ------------------------------------------------------------------ установка
if [ -n "$SERVICE" ] && [ "$WAS_RUNNING" -eq 1 ]; then
    info "останавливаю ${SERVICE}"
    service "$SERVICE" stop || true
    sleep 2
fi

info "устанавливаю"
cat "$TMP_BIN" > "$TARGET"
chmod 755 "$TARGET"
sync

NEW_HASH="$(sha256sum "$TARGET" | awk '{print $1}')"
SRC_HASH="$(sha256sum "$TMP_BIN" | awk '{print $1}')"
[ "$NEW_HASH" = "$SRC_HASH" ] || die "хэш установленного файла не совпал — восстановите бэкап через --rollback"

ok "установлено: $("$TARGET" version | head -n1)"

if [ -n "$SERVICE" ]; then
    if [ "$WAS_RUNNING" -eq 1 ]; then
        info "запускаю ${SERVICE}"
        service "$SERVICE" start || true
        sleep 8
    else
        warn "sing-box не был запущен — сервис не стартую. Запустите вручную: service ${SERVICE} start"
    fi
fi

# ------------------------------------------------------------------- итог
printf '\n'
printf 'Текущий sing-box:\n'
"$TARGET" version
printf '\n'
if [ -x /usr/bin/podkop ] && [ "$WAS_RUNNING" -eq 1 ]; then
    printf 'Статус Podkop:\n'
    podkop check_sing_box 2>/dev/null || true
fi
printf '\n'
df -h /overlay 2>/dev/null || true

printf '\n\033[32mГотово.\033[0m Проверьте вручную:\n'
printf '  nft list table inet PodkopTable | grep -c tproxy   # ожидается 2\n'
printf '  wget -qO- http://<LAN_IP>:9090/version              # JSON от Clash API\n'
printf '  LuCI → Podkop: дашборд и задержки узлов\n'
printf '  reboot, затем повторить проверки\n'
printf '\nОткат: sh install-slim.sh --rollback /tmp/sing-box-orig\n'
