# -*- coding: utf-8 -*-
"""Текст для Telegram Rich Messages с обычным HTML на случай отката."""


class RichText(str):
    """Rich-HTML строка, которая помнит, как показать её без rich-режима.

    Значение объекта — rich-разметка. В ``fallback`` лежит обычный HTML,
    совместимый с ``parse_mode=HTML``: он нужен, если rich-сообщения
    отключены в LuCI или клиент/API не принял rich-разметку.
    """

    def __new__(cls, rich, fallback=None):
        obj = super().__new__(cls, rich)
        obj.rich = str(rich)
        obj.fallback = str(fallback if fallback is not None else rich)
        return obj
