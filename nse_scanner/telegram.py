"""Telegram sender — one HTTP POST per alert, no batching delay."""

from __future__ import annotations

import logging
import os
import time

import requests

log = logging.getLogger("nse_scanner.telegram")

API = "https://api.telegram.org/bot{token}/sendMessage"


def config_from_env() -> tuple[str, str]:
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    return token, chat


def send(text: str, *, dry_run: bool = False, parse_mode: str = "HTML") -> bool:
    token, chat = config_from_env()
    if dry_run or os.environ.get("DRY_RUN", "0") == "1":
        log.info("DRY-RUN telegram:\n%s", text)
        print(text)
        return True
    if not token or not chat:
        log.error("TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID missing — printing instead")
        print(text)
        return False
    payload = {
        "chat_id": chat,
        "text": text,
        "parse_mode": parse_mode,
        "disable_web_page_preview": True,
    }
    url = API.format(token=token)
    last = None
    for i in range(3):
        try:
            r = requests.post(url, json=payload, timeout=12)
            if r.ok:
                return True
            last = f"{r.status_code} {r.text[:200]}"
            log.warning("telegram %s", last)
        except Exception as e:
            last = str(e)
            log.warning("telegram error %s", e)
        time.sleep(0.6 * (i + 1))
    log.error("telegram failed: %s", last)
    return False


def ping() -> bool:
    return send("NSE scanner online. Waiting for 5m bars.", dry_run=False)
