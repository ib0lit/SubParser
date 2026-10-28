#!/bin/sh
set -e

echo "=== [1/5] Остановка и отключение служб ==="
/etc/init.d/subparser stop >/dev/null 2>&1 || true
/etc/init.d/subparser disable >/dev/null 2>&1 || true

/etc/init.d/subparser-bot stop >/dev/null 2>&1 || true
/etc/init.d/subparser-bot disable >/dev/null 2>&1 || true

killall -9 subparser.py subparser-bot.py subparser-watchdog.sh >/dev/null 2>&1 || true

# Очистка динамической таблицы файрвола bot_block
nft delete table inet bot_block >/dev/null 2>&1 || true

echo "=== [2/5] Удаление задач планировщика (cron) ==="
CRON_TMP="/tmp/cron_subparser_uninst.tmp"
crontab -l 2>/dev/null | grep -v "subparser" > "$CRON_TMP" || true
crontab "$CRON_TMP" 2>/dev/null || true
rm -f "$CRON_TMP"
/etc/init.d/cron restart >/dev/null 2>&1 || true

echo "=== [3/5] Удаление файлов приложения ==="
rm -f /usr/bin/subparser*
rm -rf /usr/lib/subparser
rm -f /etc/init.d/subparser /etc/init.d/subparser-bot
rm -f /etc/hotplug.d/uci/99-subparser 2>/dev/null || true
rm -f /etc/subparser_version /etc/subparser_pkg_version /etc/subparser_commit /etc/subparser_branch 2>/dev/null || true
rm -rf /etc/subparser 2>/dev/null || true
rm -f /tmp/subparser* /tmp/bot_blocked_macs.json /tmp/subparser_bot_state.json 2>/dev/null || true
rm -f /var/run/subparser.lock 2>/dev/null || true

echo "=== [4/5] Удаление файлов интерфейса LuCI ==="
rm -f /usr/share/luci/menu.d/luci-app-subparser.json
rm -f /usr/share/rpcd/acl.d/luci-app-subparser.json
rm -f /usr/share/rpcd/acl.d/subparser.json 2>/dev/null || true
rm -f /www/luci-static/resources/view/subparser.js

# Удаление конфигурации
rm -f /etc/config/subparser

echo "=== [5/5] Очистка кэша веб-интерфейса ==="
rm -rf /tmp/luci-indexcache /tmp/luci-modulecache
/etc/init.d/rpcd reload >/dev/null 2>&1 || true
/etc/init.d/uhttpd restart >/dev/null 2>&1 || true

echo ""
echo "=========================================================="
echo " [OK] SubParser полностью удален с маршрутизатора!"
echo " Меню LuCI, расписание cron и фоновые процессы очищены."
echo "=========================================================="