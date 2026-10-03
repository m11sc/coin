#!/usr/bin/env python3
"""
Добавляет боту кнопку «📊 Статистика» в меню (слева от поля ввода).
Кнопка открывает Mini App — страницу docs/index.html на GitHub Pages — прямо внутри Telegram.

Запускается один раз: вкладка Actions → setup-menu → Run workflow.
Нужны TG_TOKEN и WEBAPP_URL (адрес страницы, например https://m11sc.github.io/crypto-signals/).
"""

import os
import sys

import requests

TOKEN = os.getenv("TG_TOKEN", "")
URL = os.getenv("WEBAPP_URL", "")
CHAT_ID = os.getenv("TG_CHAT_ID", "")
TEXT = os.getenv("MENU_TEXT") or "📊 Статистика"


def call(method, **payload):
    r = requests.post(f"https://api.telegram.org/bot{TOKEN}/{method}", json=payload, timeout=10)
    data = r.json()
    if not data.get("ok"):
        raise RuntimeError(f"{method}: {data.get('description')}")
    return data["result"]


def main():
    if not TOKEN or not URL.startswith("https://"):
        sys.exit("Нужны TG_TOKEN и WEBAPP_URL (адрес должен начинаться с https://)")
    button = {"type": "web_app", "text": TEXT, "web_app": {"url": URL}}
    call("setChatMenuButton", menu_button=button)            # для всех личных чатов с ботом
    print(f"Кнопка меню «{TEXT}» → {URL}")
    # Если сигналы приходят в личку (id без минуса) — ставим кнопку и в этот чат явно
    if CHAT_ID and not CHAT_ID.startswith("-"):
        call("setChatMenuButton", chat_id=int(CHAT_ID), menu_button=button)
        print("Кнопка добавлена в чат", CHAT_ID)
    else:
        print("Сигналы идут в группу или канал: открывайте статистику из личного чата с ботом.")


if __name__ == "__main__":
    main()
