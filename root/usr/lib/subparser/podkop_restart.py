# -*- coding: utf-8 -*-
"""Фоновый перезапуск Podkop после сохранения узлов из дашборда.

Запускается отсоединённым процессом из subparser.podkop. Сначала
перезапускает Podkop, ждёт готовности Clash API, затем восстанавливает
ручной выбор сервера по ссылке (теги `section-N-out` после удаления узлов
сдвигаются). В конце пишет маркер готовности для опроса из LuCI.
"""
import json
import os
import subprocess
import sys

PODKOP_INIT = "/etc/init.d/podkop"
READY_FILE = "/tmp/subparser_restart_ready.json"


def _write_ready(token, ok):
    try:
        with open(READY_FILE, "w", encoding="utf-8") as fh:
            json.dump({"ready": True, "ok": bool(ok), "token": token}, fh)
    except Exception:
        pass


def _read_plan(path):
    if not path:
        return "", []
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except Exception:
        return "", []
    finally:
        try:
            os.remove(path)
        except Exception:
            pass
    if not isinstance(data, dict):
        return "", []
    return str(data.get("token", "")), data.get("restore", []) or []


def _restart():
    try:
        proc = subprocess.Popen([PODKOP_INIT, "restart"],
                                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL, start_new_session=True,
                                close_fds=True)
    except Exception:
        return False
    try:
        proc.wait(timeout=25)
    except Exception:
        # init-скрипт может ждать procd дольше — не убиваем его, просто
        # переходим к ожиданию Clash API.
        pass
    return True


def main():
    token, plan = _read_plan(sys.argv[1] if len(sys.argv) > 1 else "")
    try:
        sys.path.insert(0, "/usr/lib")
        from subparser.podkop import _wait_for_api, restore_selection
    except Exception:
        _write_ready(token, False)
        return

    _restart()
    api_ready = _wait_for_api(12)

    for item in plan:
        try:
            if not isinstance(item, dict) or item.get("mode") != "manual":
                continue
            link = item.get("link")
            section = str(item.get("section", "") or "")
            links = item.get("links") or []
            if section and link and links:
                restore_selection(section, "manual", link, links)
        except Exception:
            continue

    _write_ready(token, api_ready)


if __name__ == "__main__":
    main()
