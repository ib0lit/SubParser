# -*- coding: utf-8 -*-
"""Разбор `uci export subparser` и краткий список подписок для LuCI.

Модуль не импортирует `subparser.subs`, потому что тот тянет `urllib` и
`subparser.config`. Для выпадающих списков в LuCI нужны только имя секции,
отображаемое имя и флаг включения.
"""
import shlex
import subprocess

CONFIG_NAME = "subparser"


def parse_uci_export(raw):
    """Разбирает `uci export` в словарь секций без запуска `uci` на каждую опцию."""
    sections = {}
    order = []
    current = None
    anon_count = {}
    for line in (raw or "").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        try:
            parts = shlex.split(line, comments=False)
        except ValueError:
            continue
        if not parts:
            continue
        if parts[0] == "config" and len(parts) >= 2:
            stype = parts[1]
            if len(parts) >= 3:
                name = parts[2]
            else:
                # LuCI-секции подписок анонимные: `uci export` печатает
                # `config subscription` без имени. Сохраняем совместимое
                # обращение `@subscription[N]`, как у `uci show`.
                idx = anon_count.get(stype, 0)
                anon_count[stype] = idx + 1
                name = f"@{stype}[{idx}]"
            if name not in sections:
                sections[name] = {"type": stype, "options": {}, "lists": {}}
                order.append(name)
            current = sections[name]
        elif parts[0] == "option" and len(parts) >= 3 and current is not None:
            current["options"][parts[1]] = parts[2]
        elif parts[0] == "list" and len(parts) >= 3 and current is not None:
            current["lists"].setdefault(parts[1], []).append(parts[2])
    return sections, order


def get_subscription_list():
    """Краткий список подписок: sec_name, name, enabled."""
    result = []
    try:
        raw = subprocess.run(["uci", "-q", "export", CONFIG_NAME],
                             stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                             text=True).stdout
        sections, order = parse_uci_export(raw)
        for s_name in order:
            sec = sections.get(s_name) or {}
            if sec.get("type") != "subscription":
                continue
            opts = sec.get("options") or {}
            result.append({
                "sec_name": s_name,
                "name": opts.get("name") or f"Подписка #{len(result) + 1}",
                "enabled": opts.get("enabled", "1"),
            })
    except Exception:
        pass
    return result
