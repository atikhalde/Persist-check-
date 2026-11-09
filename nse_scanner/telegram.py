"""Telegram sender — one HTTP POST per alert and configured bot."""

from __future__ import annotations

import logging
import os
import time

import requests

log = logging.getLogger("nse_scanner.telegram")

API = "https://api.telegram.org/bot{token}/sendMessage"


def config_from_env() -> tuple[str, str]:
    """Return the required primary bot token and chat ID."""
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    return token, chat


def secondary_config_from_env() -> tuple[str, str]:
    """Return the optional secondary bot token and chat ID."""
    token = os.environ.get("TELEGRAM_BOT_TOKEN_2", "").strip()
    chat = os.environ.get("TELEGRAM_CHAT_ID_2", "").strip()
    return token, chat


def _send_to(token: str, chat: str, text: str, *, parse_mode: str, label: str) -> bool:
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
            response = requests.post(url, json=payload, timeout=12)
            if response.ok:
                return True
            last = f"{response.status_code} {response.text[:200]}"
            log.warning("telegram %s send failed: %s", label, last)
        except Exception as exc:
            # Request exceptions can contain the request URL (including the bot
            # token), so log only the exception type, never its string value.
            last = type(exc).__name__
            log.warning("telegram %s request failed (%s)", label, last)
        time.sleep(0.6 * (i + 1))
    log.error("telegram %s delivery failed: %s", label, last)
    return False


def send(text: str, *, dry_run: bool = False, parse_mode: str = "HTML") -> bool:
    """Send to the required primary bot and, if configured, mirror to bot 2.

    The primary bot remains authoritative for the scanner's existing dedupe
    behavior. A secondary failure is logged but does not make the pipeline
    resend an alert that the primary bot already delivered.
    """
    if dry_run or os.environ.get("DRY_RUN", "0") == "1":
        log.info("DRY-RUN telegram:\n%s", text)
        print(text)
        return True

    token, chat = config_from_env()
    if not token or not chat:
        log.error("TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID missing — printing instead")
        print(text)
        return False

    if not _send_to(token, chat, text, parse_mode=parse_mode, label="primary"):
        return False

    token_2, chat_2 = secondary_config_from_env()
    if token_2 or chat_2:
        if not token_2 or not chat_2:
            log.error("TELEGRAM_BOT_TOKEN_2 and TELEGRAM_CHAT_ID_2 must both be set; secondary bot skipped")
        elif not _send_to(token_2, chat_2, text, parse_mode=parse_mode, label="secondary"):
            log.error("secondary Telegram alert was not delivered; primary alert was delivered")

    return True


def ping() -> bool:
    return send("NSE scanner online. Waiting for 5m bars.", dry_run=False)
