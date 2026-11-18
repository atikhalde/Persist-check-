"""Telegram primary/secondary delivery tests (no network access)."""

import os
from unittest import TestCase
from unittest.mock import patch

from nse_scanner import telegram


class Response:
    def __init__(self, ok: bool, status_code: int = 200, text: str = ""):
        self.ok = ok
        self.status_code = status_code
        self.text = text


class TelegramDeliveryTests(TestCase):
    def test_alert_is_mirrored_to_both_bots(self):
        env = {
            "TELEGRAM_BOT_TOKEN": "primary-token",
            "TELEGRAM_CHAT_ID": "primary-chat",
            "TELEGRAM_BOT_TOKEN_2": "secondary-token",
            "TELEGRAM_CHAT_ID_2": "secondary-chat",
        }
        with patch.dict(os.environ, env, clear=True):
            with patch.object(telegram.requests, "post", side_effect=[Response(True), Response(True)]) as post:
                self.assertTrue(telegram.send("alert", parse_mode="HTML"))

        self.assertEqual(post.call_count, 2)
        self.assertEqual(post.call_args_list[0].args[0],
                         "https://api.telegram.org/botprimary-token/sendMessage")
        self.assertEqual(post.call_args_list[1].args[0],
                         "https://api.telegram.org/botsecondary-token/sendMessage")
        expected_payload = {
            "chat_id": "primary-chat",
            "text": "alert",
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }
        self.assertEqual(post.call_args_list[0].kwargs["json"], expected_payload)
        expected_payload["chat_id"] = "secondary-chat"
        self.assertEqual(post.call_args_list[1].kwargs["json"], expected_payload)

    def test_primary_only_configuration_keeps_existing_behavior(self):
        env = {"TELEGRAM_BOT_TOKEN": "primary-token", "TELEGRAM_CHAT_ID": "primary-chat"}
        with patch.dict(os.environ, env, clear=True):
            with patch.object(telegram.requests, "post", return_value=Response(True)) as post:
                self.assertTrue(telegram.send("alert"))
        post.assert_called_once()
        self.assertIn("primary-token", post.call_args.args[0])

    def test_secondary_failure_does_not_repeat_primary_alert(self):
        env = {
            "TELEGRAM_BOT_TOKEN": "primary-token",
            "TELEGRAM_CHAT_ID": "primary-chat",
            "TELEGRAM_BOT_TOKEN_2": "secondary-token",
            "TELEGRAM_CHAT_ID_2": "secondary-chat",
        }
        failures = [Response(False, 503, "unavailable") for _ in range(3)]
        with patch.dict(os.environ, env, clear=True):
            with patch.object(telegram.requests, "post", side_effect=[Response(True), *failures]) as post, \
                    patch.object(telegram.time, "sleep"):
                # Return value follows the required primary bot so existing
                # pipeline state/deduplication remains unchanged.
                self.assertTrue(telegram.send("alert"))

        self.assertEqual(post.call_count, 4)
        self.assertIn("primary-token", post.call_args_list[0].args[0])
        self.assertTrue(all("secondary-token" in call.args[0] for call in post.call_args_list[1:]))

    def test_incomplete_secondary_pair_is_skipped_without_blocking_primary(self):
        env = {
            "TELEGRAM_BOT_TOKEN": "primary-token",
            "TELEGRAM_CHAT_ID": "primary-chat",
            "TELEGRAM_BOT_TOKEN_2": "secondary-token",
        }
        with patch.dict(os.environ, env, clear=True):
            with patch.object(telegram.requests, "post", return_value=Response(True)) as post:
                self.assertTrue(telegram.send("alert"))
        post.assert_called_once()

    def test_a_markup_mistake_does_not_lose_the_alert(self):
        """18 Nov: nine alerts were dropped, four retries each, in silence.

        The pick reason says ``conf 30 < 55``; one bare ``<`` made Telegram refuse
        the whole message — including the four WAIT blocks the audit was waiting
        for. The words now go out as plain text instead of not at all.
        """
        bad = ('{"ok":false,"error_code":400,"description":"Bad Request: can\'t parse '
               'entities: Unsupported start tag \"\" at byte offset 400"}')
        env = {"TELEGRAM_BOT_TOKEN": "t", "TELEGRAM_CHAT_ID": "c"}
        with patch.dict(os.environ, env, clear=True), \
                patch.object(telegram.requests, "post",
                             side_effect=[Response(False, 400, bad), Response(True)]) as post, \
                patch.object(telegram.time, "sleep"):
            self.assertTrue(telegram.send("<b>MARKET STAND ASIDE</b>\nconf 30 < 55"))

        self.assertEqual(post.call_count, 2)
        plain = post.call_args_list[1].kwargs["json"]
        self.assertNotIn("parse_mode", plain)                 # markup off
        self.assertEqual(plain["text"], "MARKET STAND ASIDE\nconf 30 < 55")

    def test_the_escape_keeps_a_less_than_sign_readable(self):
        self.assertEqual(telegram.esc("conf 30 < 55"), "conf 30 &lt; 55")
        self.assertEqual(telegram.esc("A & B"), "A &amp; B")
        self.assertEqual(telegram.esc(None), "None")

    def test_primary_failure_keeps_existing_retry_and_failure_behavior(self):
        env = {
            "TELEGRAM_BOT_TOKEN": "primary-token",
            "TELEGRAM_CHAT_ID": "primary-chat",
            "TELEGRAM_BOT_TOKEN_2": "secondary-token",
            "TELEGRAM_CHAT_ID_2": "secondary-chat",
        }
        with patch.dict(os.environ, env, clear=True):
            with patch.object(telegram.requests, "post", return_value=Response(False, 503, "unavailable")) as post, \
                    patch.object(telegram.time, "sleep"):
                self.assertFalse(telegram.send("alert"))
        self.assertEqual(post.call_count, 3)
        self.assertTrue(all("primary-token" in call.args[0] for call in post.call_args_list))

    def test_a_secondary_chat_that_refuses_every_alert_is_counted(self):
        """18 Nov: ``chat not found`` on every mirror, and the run still looked clean.

        The primary alert is delivered and must stay delivered; what changes is that
        the refusal is now remembered, so the day's summary can name it.
        """
        telegram.reset_secondary_failures()
        self.addCleanup(telegram.reset_secondary_failures)
        env = {
            "TELEGRAM_BOT_TOKEN": "primary-token",
            "TELEGRAM_CHAT_ID": "primary-chat",
            "TELEGRAM_BOT_TOKEN_2": "secondary-token",
            "TELEGRAM_CHAT_ID_2": "secondary-chat",
        }
        not_found = Response(False, 400, '{"description":"Bad Request: chat not found"}')
        posts = []

        def post(url, **kwargs):
            posts.append(url)
            return Response(True) if "primary-token" in url else not_found

        with patch.dict(os.environ, env, clear=True), \
                patch.object(telegram.requests, "post", side_effect=post), \
                patch.object(telegram.time, "sleep"):
            for _ in range(3):
                self.assertTrue(telegram.send("alert"))       # primary still wins

        self.assertEqual(sum("primary-token" in u for u in posts), 3)
        self.assertEqual(sum("secondary-token" in u for u in posts), 9)   # 3 retries each
        self.assertEqual(telegram.secondary_failures(), 3)               # 3 alerts lost

    def test_a_half_configured_secondary_bot_counts_as_undelivered(self):
        telegram.reset_secondary_failures()
        self.addCleanup(telegram.reset_secondary_failures)
        env = {"TELEGRAM_BOT_TOKEN": "t", "TELEGRAM_CHAT_ID": "c",
               "TELEGRAM_BOT_TOKEN_2": "secondary-token"}
        with patch.dict(os.environ, env, clear=True), \
                patch.object(telegram.requests, "post", return_value=Response(True)):
            self.assertTrue(telegram.send("alert"))
        self.assertEqual(telegram.secondary_failures(), 1)
