"""Optional local Ohverlay webhook notifications for TradingBot23.

This module only sends short messages to a localhost Ohverlay webhook. It does
not import Ohverlay, does not expose secrets, and never raises into trading
logic.
"""

import html
import logging
import re
import threading
from urllib.parse import urlparse

import requests

from bot import config

logger = logging.getLogger(__name__)

_TAG_RE = re.compile(r"<[^>]+>")
_SPACE_RE = re.compile(r"[ \t\r\f\v]+")
_SECRET_RE = re.compile(
    r"(?i)\b(api[_ -]?key|secret|token|password)\b\s*[:=]\s*[^\s<>&]+"
)
_LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1"}


def enabled() -> bool:
    return bool(getattr(config, "OHVERLAY_ENABLED", False))


def _local_url(url: str) -> bool:
    try:
        parsed = urlparse(url)
    except Exception:
        return False
    return parsed.scheme in {"http", "https"} and parsed.hostname in _LOCAL_HOSTS


def _plain_text(text: str) -> str:
    cleaned = html.unescape(_TAG_RE.sub("", str(text or "")))
    cleaned = _SECRET_RE.sub(lambda m: f"{m.group(1)}=[redacted]", cleaned)
    cleaned = "\n".join(_SPACE_RE.sub(" ", line).strip() for line in cleaned.splitlines())
    cleaned = "\n".join(line for line in cleaned.splitlines() if line)
    max_chars = max(80, int(getattr(config, "OHVERLAY_MAX_CHARS", 420)))
    return cleaned if len(cleaned) <= max_chars else cleaned[: max_chars - 3] + "..."


def _payload(text: str, title: str | None = None, source: str = "tradingbot23") -> dict:
    parts = []
    if title:
        parts.append(_plain_text(title))
    body = _plain_text(text)
    if body:
        parts.append(body)
    return {
        "text": "\n".join(parts),
        "sender": getattr(config, "OHVERLAY_SENDER", "TradingBot23"),
        "source": source,
    }


def send(text: str, title: str | None = None, source: str = "tradingbot23") -> None:
    """Send one optional Ohverlay bubble notification in the background."""
    if not enabled():
        return
    url = getattr(config, "OHVERLAY_WEBHOOK_URL", "http://127.0.0.1:7277/message")
    if not _local_url(url):
        logger.debug("Ohverlay webhook URL is not local; skipping: %s", url)
        return
    payload = _payload(text, title=title, source=source)
    if not payload["text"]:
        return

    def _post() -> None:
        try:
            requests.post(
                url,
                json=payload,
                timeout=float(getattr(config, "OHVERLAY_TIMEOUT_SECS", 2.0)),
            )
        except Exception as exc:
            logger.debug("Ohverlay notification failed: %s", exc)

    threading.Thread(target=_post, daemon=True).start()


def send_event(title: str, lines: list[str], source: str = "tradingbot23") -> None:
    send("\n".join(lines), title=title, source=source)
