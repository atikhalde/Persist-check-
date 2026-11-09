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
