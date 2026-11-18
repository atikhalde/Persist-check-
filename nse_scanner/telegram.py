"""Telegram sender — one HTTP POST per alert and configured bot."""

from __future__ import annotations

import logging
import os
import re
import time

import requests

log = logging.getLogger("nse_scanner.telegram")

API = "https://api.telegram.org/bot{token}/sendMessage"

_PARSE_ERR = "can't parse entities"
_TAGS = re.compile(r"</?(?:b|strong|i|em|u|s|code|pre)>", re.IGNORECASE)


#: Alerts bot 2 refused since this process started (usually ``chat not found``).
#:
#: A secondary failure is logged and then forgotten — the run stays green, the
#: primary alert is delivered, and nobody learns that the mirror chat got nothing.
#: 18 Nov: every single message of the morning was refused for this reason and the
#: only trace was one log line per alert. The count reaches the day's summary.
_secondary_failures = 0


def secondary_failures() -> int:
    """How many alerts the secondary bot failed to deliver (0 = it is fine)."""
    return _secondary_failures


def reset_secondary_failures() -> None:
    """Forget the running count (a new session or a fresh process)."""
    global _secondary_failures
    _secondary_failures = 0


def esc(text) -> str:
    """Escape text that goes *inside* an HTML message.

    18 Nov: every WAIT block of the morning was dropped by Telegram —
    ``Bad Request: can't parse entities: Unsupported start tag ""`` — because the
    pick reason says ``conf 30 < 55`` and ``persist 43 < 50``: one bare ``<`` and
    the whole message is refused, four retries and then silence. Nine messages
    that morning, including the four the audit was waiting for.
    """
    return (str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


def _plain(text: str) -> str:
    """The same message with the scanner's own markup stripped, for the fallback."""
    out = _TAGS.sub("", text)
    return out.replace("&lt;", "<").replace("&gt;", ">").replace("&amp;", "&")


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
    fell_back = False
    for i in range(3):
        try:
            response = requests.post(url, json=payload, timeout=12)
            if response.ok:
                return True
            last = f"{response.status_code} {response.text[:200]}"
            # A markup mistake must not cost the alert. Send the same words as
            # plain text — the reader loses the bold, not the warning.
            if parse_mode and not fell_back and _PARSE_ERR in response.text:
                fell_back = True
                plain = {k: v for k, v in payload.items() if k != "parse_mode"}
                plain["text"] = _plain(text)
                log.warning("telegram %s rejected the markup (%s); retrying as plain text",
                            label, last)
                again = requests.post(url, json=plain, timeout=12)
                if again.ok:
                    return True
                last = f"{again.status_code} {again.text[:200]}"
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

    global _secondary_failures
    token_2, chat_2 = secondary_config_from_env()
    if token_2 or chat_2:
        if not token_2 or not chat_2:
            _secondary_failures += 1
            log.error("TELEGRAM_BOT_TOKEN_2 and TELEGRAM_CHAT_ID_2 must both be set; secondary bot skipped")
        elif not _send_to(token_2, chat_2, text, parse_mode=parse_mode, label="secondary"):
            _secondary_failures += 1
            log.error("secondary Telegram alert was not delivered (check TELEGRAM_CHAT_ID_2: "
                      "bot 2 must be a member of that chat); primary alert was delivered")

    return True


def third_config_from_env() -> tuple[str, str]:
    """Return the 1-minute scan's own bot token and chat ID.

    Bot 3 is a separate channel on purpose: the projected (pre-close) alerts of
    ``scan1m`` must never be mixed into the close-confirmed stream of bots 1/2.
    """
    token = os.environ.get("TELEGRAM_BOT_TOKEN_3", "").strip()
    chat = os.environ.get("TELEGRAM_CHAT_ID_3", "").strip()
    return token, chat


def send_bot3(text: str, *, dry_run: bool = False, parse_mode: str = "HTML") -> bool:
    """Send ONLY to bot 3 — no mirroring in either direction.

    ``send`` (bots 1 + 2) is left exactly as it is, and this function never
    falls back to it: a missing bot-3 configuration must not leak early alerts
    into the confirmed channel.
    """
    if dry_run or os.environ.get("DRY_RUN", "0") == "1":
        log.info("DRY-RUN telegram (bot 3):\n%s", text)
        print(text)
        return True

    token, chat = third_config_from_env()
    if not token or not chat:
        log.error("TELEGRAM_BOT_TOKEN_3 / TELEGRAM_CHAT_ID_3 missing — printing instead")
        print(text)
        return False
    return _send_to(token, chat, text, parse_mode=parse_mode, label="bot3")


def ping() -> bool:
    return send("NSE scanner online. Waiting for 5m bars.", dry_run=False)
