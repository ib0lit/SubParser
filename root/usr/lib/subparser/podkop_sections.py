# -*- coding: utf-8 -*-
"""Лёгкое определение секций Podkop.

Модуль намеренно не импортирует `subparser.podkop`: тому нужны `urllib`,
`concurrent.futures` и другие тяжёлые модули, из-за которых запуск Python на
слабых роутерах занимал больше 2 секунд. LuCI запрашивает список секций при
каждом открытии страницы, поэтому здесь остаётся только `subprocess`.
"""
import subprocess

# Podkop хранит секции как `config section 'main'`. В старых версиях тип был
# `podkop`. Тип нужно проверять явно: поиск подстроки `=podkop` находил только
# `podkop.settings=settings`, поэтому все секции, кроме main, терялись.
PODKOP_SECTION_TYPES = ("section", "podkop")
PODKOP_SECTION_OPTIONS = ("urltest_proxy_links", "connection_type", "proxy_string")


def parse_podkop_sections(raw_show: str) -> list:
    """Имена секций Podkop из вывода `uci show podkop`."""
    detected = []

    def add(name):
        if name and name != "settings" and name not in detected:
            detected.append(name)

    for line in (raw_show or "").splitlines():
        line = line.strip()
        if not line.startswith("podkop.") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        parts = key.split(".")
        if len(parts) == 2:
            if value.strip().strip("'\"") in PODKOP_SECTION_TYPES:
                add(parts[1])
        elif len(parts) >= 3 and parts[2] in PODKOP_SECTION_OPTIONS:
            # Секция без явного типа (или нестандартный тип) — определяем по опциям.
            add(parts[1])
    return detected


def get_all_podkop_sections():
    try:
        raw_secs = subprocess.check_output(["uci", "-q", "show", "podkop"],
                                           text=True, stderr=subprocess.DEVNULL)
    except Exception:
        return ["main"]
    return parse_podkop_sections(raw_secs) or ["main"]
