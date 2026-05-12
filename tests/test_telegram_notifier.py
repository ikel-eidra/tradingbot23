"""Tests for Telegram dashboard command helpers."""

import unittest

from bot import config
from bot.modules.telegram_notifier import TelegramDashboardPoller


class FakeResponse:
    def raise_for_status(self):
        return None

    def json(self):
        return {"ok": True, "result": []}


class FakeSession:
    def __init__(self):
        self.posts = []

    def get(self, url, params, timeout):
        return FakeResponse()

    def post(self, url, json, timeout):
        self.posts.append({"url": url, "json": json, "timeout": timeout})
        return FakeResponse()


class TestTelegramDashboardPoller(unittest.TestCase):
    def setUp(self):
        self.old_token = config.TELEGRAM_BOT_TOKEN
        self.old_chat = config.TELEGRAM_CHAT_ID
        config.TELEGRAM_BOT_TOKEN = "token"
        config.TELEGRAM_CHAT_ID = "123"

    def tearDown(self):
        config.TELEGRAM_BOT_TOKEN = self.old_token
        config.TELEGRAM_CHAT_ID = self.old_chat

    def test_dashboard_command_replies_with_keyboard(self):
        session = FakeSession()
        poller = TelegramDashboardPoller(
            futures_callback=lambda: "FUTURES",
            p2p_callback=lambda: "P2P",
            info_callback=lambda: "INFO",
            session=session,
        )

        poller._handle_update({"message": {"text": "/dashboard", "chat": {"id": 123}}})

        self.assertEqual(len(session.posts), 1)
        payload = session.posts[0]["json"]
        self.assertEqual(payload["text"], "FUTURES")
        self.assertIn("reply_markup", payload)

    def test_callback_query_replies_to_p2p_button(self):
        session = FakeSession()
        poller = TelegramDashboardPoller(
            futures_callback=lambda: "FUTURES",
            p2p_callback=lambda: "P2P",
            info_callback=lambda: "INFO",
            session=session,
        )

        poller._handle_update({
            "callback_query": {
                "id": "abc",
                "data": "dashboard:p2p",
                "message": {"chat": {"id": "123"}},
            }
        })

        self.assertEqual(len(session.posts), 2)
        self.assertIn("answerCallbackQuery", session.posts[0]["url"])
        self.assertEqual(session.posts[1]["json"]["text"], "P2P")

    def test_control_command_uses_control_callback(self):
        session = FakeSession()
        poller = TelegramDashboardPoller(
            futures_callback=lambda: "FUTURES",
            p2p_callback=lambda: "P2P",
            info_callback=lambda: "INFO",
            control_callback=lambda action: f"CONTROL:{action}",
            session=session,
        )

        poller._handle_update({"message": {"text": "/today", "chat": {"id": 123}}})

        self.assertEqual(len(session.posts), 1)
        self.assertEqual(session.posts[0]["json"]["text"], "CONTROL:today")

    def test_ignores_unconfigured_chat(self):
        session = FakeSession()
        poller = TelegramDashboardPoller(
            futures_callback=lambda: "FUTURES",
            p2p_callback=lambda: "P2P",
            info_callback=lambda: "INFO",
            session=session,
        )

        poller._handle_update({"message": {"text": "/dashboard", "chat": {"id": 999}}})

        self.assertEqual(session.posts, [])


if __name__ == "__main__":
    unittest.main()
