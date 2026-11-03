# -*- coding: utf-8 -*-
"""Rich-сообщения Telegram и их обычный HTML-fallback.

Rich-разметка Bot API поддерживает заголовки, таблицы, списки, врезки,
сворачиваемые блоки и кнопки внутри текста. Обычный ``parse_mode=HTML``
всё это не умеет, поэтому :class:`RichScreen` одновременно собирает две
версии одного экрана: богатую и классическую.
"""
import html


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


def esc(value):
    """Экранирует пользовательские данные для rich- и обычного HTML."""
    return html.escape(str(value), quote=False)


def esc_attr(value):
    """Экранирует значение HTML-атрибута (кавычки обязательны)."""
    return html.escape(str(value), quote=True)


def join_rich(*parts, sep="\n\n"):
    """Склеивает RichText и обычные строки, сохраняя оба представления."""
    rich = []
    plain = []
    for part in parts:
        if isinstance(part, RichText):
            rich.append(part.rich.strip("\n"))
            plain.append(part.fallback.strip("\n"))
        else:
            rich.append(str(part).strip("\n"))
            plain.append(str(part).strip("\n"))
    return RichText(sep.join(rich), sep.join(plain))


class RichScreen:
    """Собирает rich-HTML и обычный HTML для одного экрана.

    Методы принимают уже готовый inline-HTML (``<b>``, ``<i>``, ``<code>``,
    ``<a>``), который поддерживают оба формата. Пользовательские строки
    нужно предварительно прогнать через :func:`esc`.
    """

    def __init__(self):
        self._rich = []
        self._plain = []
        self._fallback_kb = []

    def _add(self, rich, plain):
        if rich:
            self._rich.append(rich)
        if plain:
            self._plain.append(plain)
        return self

    # --- блоки текста ---------------------------------------------------
    def h(self, text, level=3):
        level = max(1, min(6, int(level)))
        return self._add(f"<h{level}>{text}</h{level}>", f"<b>{text}</b>")

    def p(self, text):
        return self._add(f"<p>{text}</p>", text)

    def raw(self, rich, plain=""):
        return self._add(rich, plain)

    def hr(self):
        return self._add("<hr/>", "────────────")

    def bullets(self, items, ordered=False):
        tag = "ol" if ordered else "ul"
        rich_items = "".join(f"<li>{item}</li>" for item in items)
        plain_lines = []
        for idx, item in enumerate(items, 1):
            plain_lines.append(f"{idx}. {item}" if ordered else f"• {item}")
        return self._add(f"<{tag}>{rich_items}</{tag}>", "\n".join(plain_lines))

    def quote(self, text, expandable=False):
        attr = " expandable" if expandable else ""
        return self._add(f"<blockquote{attr}>{text}</blockquote>",
                         f"<blockquote>{text}</blockquote>")

    def details(self, summary, rich_content, plain_content=None, open=False):
        attr = " open" if open else ""
        plain = f"<b>{summary}</b>"
        if plain_content:
            plain += f"\n{plain_content}"
        return self._add(
            f"<details{attr}><summary>{summary}</summary>{rich_content}</details>",
            plain,
        )

    def button_row(self, buttons, align="center"):
        """Кнопки действий.

        В rich-режиме отображаются как `<tg-button-row>` прямо внутри текста.
        Также сохраняются в `fallback_kb`, чтобы автоматически примениться
        в `inline_keyboard`, если rich-режим недоступен.
        """
        rich_buttons = []
        plain_lines = []
        kb_row = []
        for btn in buttons:
            text = btn.get("text", "")
            style = f' style="{esc_attr(btn["style"])}"' if btn.get("style") else ""
            kind = btn.get("type", "callback_data")
            if kind == "url":
                url = esc_attr(btn.get("url", ""))
                rich_buttons.append(
                    f'<tg-button type="url" url="{url}"{style}>{text}</tg-button>'
                )
                kb_row.append({"text": text, "url": btn.get("url", "")})
            elif kind == "copy_text":
                value = esc_attr(btn.get("text_to_copy", ""))
                rich_buttons.append(
                    f'<tg-button type="copy_text" text="{value}"{style}>'
                    f"{text}</tg-button>"
                )
            elif kind == "callback_data":
                data = esc_attr(btn.get("data", ""))
                rich_buttons.append(
                    f'<tg-button type="callback_data" data="{data}"{style}>'
                    f"{text}</tg-button>"
                )
                kb_row.append({"text": text, "callback_data": btn.get("data", "")})
                if btn.get("fallback"):
                    plain_lines.append(btn["fallback"])
        if kb_row:
            self._fallback_kb.append(kb_row)
        rich = (f'<tg-button-row align="{esc_attr(align)}">'
                f'{"".join(rich_buttons)}</tg-button-row>')
        return self._add(rich, "\n".join(plain_lines))

    @property
    def fallback_keyboard(self):
        return {"inline_keyboard": self._fallback_kb} if self._fallback_kb else None

    # --- таблицы --------------------------------------------------------
    def kv(self, rows, caption=None):
        """Таблица «параметр — значение» и список в fallback."""
        rich_head = ""
        if caption:
            rich_head = f"<caption>{caption}</caption>"
        rich_rows = "".join(
            f"<tr><td>{label}</td><td>{value}</td></tr>" for label, value in rows
        )
        plain = "\n".join(f"• <b>{label}:</b> {value}" for label, value in rows)
        return self._add(
            f"<table bordered striped compact>{rich_head}{rich_rows}</table>",
            plain,
        )

    def table(self, headers, rows, caption=None, fallback=None):
        """Произвольная таблица. ``fallback`` — готовые строки для HTML."""
        rich_head = ""
        if caption:
            rich_head += f"<caption>{caption}</caption>"
        if headers:
            rich_head += "<tr>" + "".join(f"<th>{h}</th>" for h in headers) + "</tr>"
        rich_rows = "".join(
            "<tr>" + "".join(f"<td>{cell}</td>" for cell in row) + "</tr>"
            for row in rows
        )
        if fallback is None:
            fallback = ["• " + " · ".join(str(cell) for cell in row) for row in rows]
        return self._add(
            f"<table bordered striped compact>{rich_head}{rich_rows}</table>",
            "\n".join(fallback),
        )

    def bar(self, pct, width=10):
        """Текстовая шкала заполнения (0–100)."""
        pct = max(0, min(100, int(pct)))
        filled = pct * width // 100
        return f"<code>{'█' * filled}{'░' * (width - filled)}</code> {pct}%"

    def keyboard_to_buttons(self, kb):
        """Конвертирует стандартную inline_keyboard в <tg-button-row>."""
        if not kb:
            return self
        raw_rows = kb.get("inline_keyboard", []) if isinstance(kb, dict) else kb
        for row in raw_rows:
            btns = []
            for btn in row:
                if not isinstance(btn, dict):
                    continue
                text = btn.get("text", "")
                if "url" in btn:
                    btns.append({"type": "url", "text": text, "url": btn["url"]})
                elif "callback_data" in btn:
                    data = btn["callback_data"]
                    style = ""
                    if data == "do_reboot" or data.startswith("sub_del_do:"):
                        style = "danger"
                    elif data in ("run_parser", "do_update"):
                        style = "success"
                    btns.append({"type": "callback_data", "text": text, "data": data, "style": style})
            if btns:
                self.button_row(btns)
        return self

    # --- результат ------------------------------------------------------
    def build(self, keyboard=None):
        if keyboard:
            self.keyboard_to_buttons(keyboard)
        rich = "\n".join(self._rich).strip()
        plain = "\n".join(self._plain).strip()
        return RichText(rich, plain)
