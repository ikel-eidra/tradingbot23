"""Tests for optional Ohverlay localhost notifications."""

from unittest.mock import patch

import pytest

from bot import config
from bot.modules import ohverlay_notifier as ov


@pytest.fixture(autouse=True)
def restore_ohverlay_config():
    values = {
        "OHVERLAY_ENABLED": config.OHVERLAY_ENABLED,
        "OHVERLAY_WEBHOOK_URL": config.OHVERLAY_WEBHOOK_URL,
        "OHVERLAY_SENDER": config.OHVERLAY_SENDER,
        "OHVERLAY_TIMEOUT_SECS": config.OHVERLAY_TIMEOUT_SECS,
        "OHVERLAY_MAX_CHARS": config.OHVERLAY_MAX_CHARS,
    }
    yield
    for key, value in values.items():
        setattr(config, key, value)


class ImmediateThread:
    def __init__(self, target, daemon):
        self.target = target
        self.daemon = daemon

    def start(self):
        self.target()


def test_ohverlay_send_skips_when_disabled():
    config.OHVERLAY_ENABLED = False

    with patch("bot.modules.ohverlay_notifier.requests.post") as post:
        ov.send("hello")

    post.assert_not_called()


def test_ohverlay_send_posts_plain_local_payload():
    config.OHVERLAY_ENABLED = True
    config.OHVERLAY_WEBHOOK_URL = "http://127.0.0.1:7277/message"
    config.OHVERLAY_SENDER = "TradingBot23"
    config.OHVERLAY_TIMEOUT_SECS = 1.0
    config.OHVERLAY_MAX_CHARS = 420

    with patch("bot.modules.ohverlay_notifier.threading.Thread", ImmediateThread):
        with patch("bot.modules.ohverlay_notifier.requests.post") as post:
            ov.send("<b>BTC</b> token=secret opened", title="Trade")

    post.assert_called_once()
    _, kwargs = post.call_args
    assert kwargs["json"]["sender"] == "TradingBot23"
    assert kwargs["json"]["source"] == "tradingbot23"
    assert "BTC" in kwargs["json"]["text"]
    assert "<b>" not in kwargs["json"]["text"]
    assert "secret" not in kwargs["json"]["text"]


def test_ohverlay_send_rejects_remote_url():
    config.OHVERLAY_ENABLED = True
    config.OHVERLAY_WEBHOOK_URL = "https://example.com/message"

    with patch("bot.modules.ohverlay_notifier.requests.post") as post:
        ov.send("hello")

    post.assert_not_called()
