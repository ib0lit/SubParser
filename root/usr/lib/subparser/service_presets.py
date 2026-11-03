# -*- coding: utf-8 -*-
"""Пресеты сервисов для мониторинга доступности.

URL специально выбраны так, чтобы любой HTTP-ответ (200/3xx/4xx) считался
признаком доступности: Clash API выполняет HEAD-запрос и не проверяет код
ответа. Важно лишь, что соединение и TLS прошли через активный узел.

Списки доменов сверены с itdoginfo/allow-domains (Services/meta.lst,
telegram.lst, youtube.lst, discord.lst, tiktok.lst). Для Steam и Twitch
используются их основные домены.
"""

SERVICE_PRESETS = {
    "meta": {
        "label": "Instagram",
        # Оба адреса обязательны: комментарии и медиа идут через API
        # instagram.com, поэтому сбой Instagram не должен маскироваться
        # доступностью другого сервиса.
        "require_all": True,
        "urls": [
            "https://www.instagram.com/",
            "https://www.instagram.com/api/v1/media/0/comments/",
        ],
    },
    "telegram": {
        "label": "Telegram",
        "urls": [
            "https://t.me/",
            "https://web.telegram.org/",
        ],
    },
    "youtube": {
        "label": "YouTube",
        "urls": [
            "https://www.youtube.com/",
            "https://youtu.be/",
        ],
    },
    "steam": {
        "label": "Steam",
        "urls": [
            "https://store.steampowered.com/",
            "https://steamcommunity.com/",
        ],
    },
    "discord": {
        "label": "Discord",
        "urls": [
            "https://discord.com/",
            "https://discord.gg/",
        ],
    },
    "twitch": {
        "label": "Twitch",
        "urls": [
            "https://www.twitch.tv/",
            "https://gql.twitch.tv/",
        ],
    },
    "tiktok": {
        "label": "TikTok",
        "urls": [
            "https://www.tiktok.com/",
            "https://www.tiktokv.com/",
        ],
    },
}

# Порядок важен для LuCI: он повторяет этот список в интерфейсе.
PRESET_ORDER = [
    "meta", "telegram", "youtube", "steam",
    "discord", "twitch", "tiktok",
]
