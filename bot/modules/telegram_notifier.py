"""Telegram alert notifications for TradingBot23.

Sends trade alerts to a Telegram chat. Never crashes the bot —
all errors are silently logged and ignored.

Setup:
  1. Message @BotFather on Telegram -> /newbot -> copy the token
  2. Start your bot, then get your chat_id from:
     https://api.telegram.org/bot<TOKEN>/getUpdates
  3. Add to .env:
     TELEGRAM_BOT_TOKEN=your_token_here
     TELEGRAM_CHAT_ID=your_chat_id_here
"""

import logging
import threading
from collections.abc import Callable
from html import escape
from typing import Any

import requests

from bot import config
from bot.modules import ohverlay_notifier as ov

logger = logging.getLogger(__name__)
DashboardCallback = Callable[[], str]
ControlCallback = Callable[[str], str]

_EMOJI = {
    "tp_hit":    "✅",
    "sl_hit":    "❌",
    "crash_sl":  "🚨",
    "expired":   "⏰",
    "liquidated":"💀",
    "open":      "🟢",
    "fill":      "🔵",
    "summary":   "📊",
    "error":     "⚠️",
}


def escape_html(value: Any) -> str:
    return escape(str(value), quote=False)


def dashboard_keyboard() -> dict:
    rows = [
        [
            {"text": "Futures Dashboard", "callback_data": "dashboard:futures"},
            {"text": "P2P Arb", "callback_data": "dashboard:p2p"},
        ],
        [
            {"text": "Today P&L", "callback_data": "control:today"},
            {"text": "Export Report", "callback_data": "control:export"},
        ],
    ]
    if config.TELEGRAM_ALLOW_CONTROL:
        rows.append([
            {"text": "Pause Bot", "callback_data": "control:pause"},
            {"text": "Resume Bot", "callback_data": "control:resume"},
        ])
    rows.append([{"text": "Info", "callback_data": "dashboard:info"}])
    return {"inline_keyboard": rows}


def _api_url(method: str) -> str:
    return f"https://api.telegram.org/bot{config.TELEGRAM_BOT_TOKEN}/{method}"


def _post_message(text: str, reply_markup: dict | None = None, chat_id: str | int | None = None) -> None:
    target_chat = chat_id or config.TELEGRAM_CHAT_ID
    if not config.TELEGRAM_BOT_TOKEN or not target_chat:
        return
    payload = {
        "chat_id": target_chat,
        "text": text,
        "parse_mode": "HTML",
    }
    if reply_markup:
        payload["reply_markup"] = reply_markup
    requests.post(_api_url("sendMessage"), json=payload, timeout=8)


def _send(text: str, reply_markup: dict | None = None, chat_id: str | int | None = None) -> None:
    """Fire-and-forget Telegram message. Runs in a background thread."""
    if not config.TELEGRAM_BOT_TOKEN or not (chat_id or config.TELEGRAM_CHAT_ID):
        return

    def _post():
        try:
            _post_message(text, reply_markup=reply_markup, chat_id=chat_id)
        except Exception as e:
            logger.debug("Telegram send failed: %s", e)

    threading.Thread(target=_post, daemon=True).start()


def send_dashboard_menu(text: str | None = None) -> None:
    _send(
        text or "TradingBot23 dashboard controls are ready.",
        reply_markup=dashboard_keyboard(),
    )


def send_dashboard_text(text: str) -> None:
    _send(text, reply_markup=dashboard_keyboard())


def alert_p2p_signal(text: str) -> None:
    ov.send(text, title="TradingBot23 P2P Assist", source="tradingbot23-p2p")
    _send(
        f"📣 <b>TradingBot23 P2P Assist</b>\n{text}",
        reply_markup=dashboard_keyboard(),
    )


def default_info_text() -> str:
    return (
        "<b>TradingBot23 Telegram</b>\n"
        "/dashboard - live futures paper dashboard\n"
        "/p2p - live USDT/PHP P2P assist snapshot\n"
        "/today - today's closed P&L and P2P paper summary\n"
        "/export - write a local operations report\n"
        "/info - automation scope and safety notes\n\n"
        + ("/pause or /resume - control the desktop paper loop\n\n" if config.TELEGRAM_ALLOW_CONTROL else "Remote pause/resume is disabled by default.\n\n")
        + "P2P Paper Sim uses live listings and can send paper-event alerts. "
        "P2P Live Assist can scan, score, alert, and log watch routes. "
        "Fiat payment, payment-completed confirmation, and crypto release stay manual."
    )


class TelegramDashboardPoller:
    """Small Telegram command listener for dashboard and P2P snapshots."""

    def __init__(
        self,
        futures_callback: DashboardCallback,
        p2p_callback: DashboardCallback,
        info_callback: DashboardCallback | None = None,
        control_callback: ControlCallback | None = None,
        poll_interval: float = 2.0,
        session: requests.Session | None = None,
    ):
        self.futures_callback = futures_callback
        self.p2p_callback = p2p_callback
        self.info_callback = info_callback or default_info_text
        self.control_callback = control_callback
        self.poll_interval = poll_interval
        self.session = session or requests.Session()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._offset: int | None = None

    @property
    def enabled(self) -> bool:
        return bool(config.TELEGRAM_BOT_TOKEN and config.TELEGRAM_CHAT_ID)

    def start(self) -> None:
        if not self.enabled or self._thread:
            return
        self._prime_offset()
        self._thread = threading.Thread(target=self._poll_loop, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=1)

    def _prime_offset(self) -> None:
        """Skip queued old Telegram commands when the desktop app starts."""
        try:
            response = self.session.get(_api_url("getUpdates"), params={"timeout": 0}, timeout=8)
            response.raise_for_status()
            updates = response.json().get("result", [])
            update_ids = [u.get("update_id") for u in updates if isinstance(u.get("update_id"), int)]
            if update_ids:
                self._offset = max(update_ids) + 1
        except Exception as exc:
            logger.debug("Telegram dashboard offset prime failed: %s", exc)

    def _poll_loop(self) -> None:
        while not self._stop.is_set():
            try:
                params = {"timeout": 20}
                if self._offset is not None:
                    params["offset"] = self._offset
                response = self.session.get(_api_url("getUpdates"), params=params, timeout=25)
                response.raise_for_status()
                body = response.json()
                for update in body.get("result", []):
                    update_id = update.get("update_id")
                    if isinstance(update_id, int):
                        self._offset = update_id + 1
                    self._handle_update(update)
            except Exception as exc:
                logger.debug("Telegram dashboard poll failed: %s", exc)
                self._stop.wait(self.poll_interval)

    def _handle_update(self, update: dict) -> None:
        if "callback_query" in update:
            self._handle_callback(update["callback_query"])
            return
        message = update.get("message") or {}
        text = str(message.get("text") or "").strip().lower()
        chat = message.get("chat") or {}
        chat_id = chat.get("id")
        if not self._chat_allowed(chat_id):
            return
        if text in {"/start", "/menu", "menu"}:
            self._reply(chat_id, "TradingBot23 dashboard controls:", dashboard_keyboard())
        elif text in {"/dashboard", "/status", "dashboard", "status"}:
            self._reply(chat_id, self._safe_callback(self.futures_callback), dashboard_keyboard())
        elif text in {"/p2p", "p2p"}:
            self._reply(chat_id, self._safe_callback(self.p2p_callback), dashboard_keyboard())
        elif text in {"/today", "today"}:
            self._reply(chat_id, self._safe_control("today"), dashboard_keyboard())
        elif text in {"/pause", "pause"}:
            self._reply(chat_id, self._safe_control("pause"), dashboard_keyboard())
        elif text in {"/resume", "resume"}:
            self._reply(chat_id, self._safe_control("resume"), dashboard_keyboard())
        elif text in {"/export", "export"}:
            self._reply(chat_id, self._safe_control("export"), dashboard_keyboard())
        elif text in {"/info", "info", "/help", "help"}:
            self._reply(chat_id, self._safe_callback(self.info_callback), dashboard_keyboard())

    def _handle_callback(self, query: dict) -> None:
        message = query.get("message") or {}
        chat = message.get("chat") or {}
        chat_id = chat.get("id")
        if not self._chat_allowed(chat_id):
            return
        data = query.get("data")
        if data == "dashboard:futures":
            text = self._safe_callback(self.futures_callback)
        elif data == "dashboard:p2p":
            text = self._safe_callback(self.p2p_callback)
        elif data == "dashboard:info":
            text = self._safe_callback(self.info_callback)
        elif isinstance(data, str) and data.startswith("control:"):
            text = self._safe_control(data.split(":", 1)[1])
        else:
            text = "Unknown dashboard action."
        try:
            callback_id = query.get("id")
            if callback_id:
                self.session.post(_api_url("answerCallbackQuery"), json={"callback_query_id": callback_id}, timeout=8)
        except Exception as exc:
            logger.debug("Telegram callback answer failed: %s", exc)
        self._reply(chat_id, text, dashboard_keyboard())

    def _reply(self, chat_id: int | str, text: str, reply_markup: dict | None = None) -> None:
        try:
            payload = {"chat_id": chat_id, "text": text, "parse_mode": "HTML"}
            if reply_markup:
                payload["reply_markup"] = reply_markup
            self.session.post(_api_url("sendMessage"), json=payload, timeout=8)
        except Exception as exc:
            logger.debug("Telegram dashboard reply failed: %s", exc)

    def _chat_allowed(self, chat_id: int | str | None) -> bool:
        return chat_id is not None and str(chat_id) == str(config.TELEGRAM_CHAT_ID)

    @staticmethod
    def _safe_callback(callback: DashboardCallback) -> str:
        try:
            return callback()
        except Exception as exc:
            logger.exception("Telegram dashboard callback failed")
            return f"⚠️ Dashboard data unavailable: {escape_html(exc)}"

    def _safe_control(self, action: str) -> str:
        if not self.control_callback:
            return "Telegram controls are not attached to this dashboard instance."
        try:
            return self.control_callback(action)
        except Exception as exc:
            logger.exception("Telegram control callback failed")
            return f"⚠️ Control action failed: {escape_html(exc)}"


def alert_opened(symbol: str, entry_price: float, tp_price: float,
                 liq_price: float, margin_usd: float, leverage: int,
                 change_24h: float, filled: bool = False) -> None:
    emoji = _EMOJI["fill"] if filled else _EMOJI["open"]
    tag   = "FILLED (no dip)" if filled else "DIP ENTRY"
    mode  = config.TRADING_MODE.upper()
    ov.send_event(
        f"TradingBot23 {tag}",
        [
            f"{symbol} opened at ${entry_price:,.4f} ({change_24h:+.2f}% 24h)",
            f"Margin ${margin_usd:.2f} | {leverage}x | TP ${tp_price:,.4f}",
            f"Cross LIQ ${liq_price:,.4f}",
        ],
        source="tradingbot23-futures",
    )
    _send(
        f"{emoji} <b>TradingBot23 [{mode}] — {tag}</b>\n"
        f"Coin: <b>{symbol}</b>\n"
        f"Entry: <b>${entry_price:,.4f}</b>  ({change_24h:+.2f}% 24h)\n"
        f"Margin: <b>${margin_usd:.2f}</b>  |  Leverage: <b>{leverage}x</b>\n"
        f"TP: ${tp_price:,.4f}  |  Cross LIQ: ${liq_price:,.4f}"
    )


def alert_closed(symbol: str, entry_price: float, exit_price: float,
                 pnl_pct: float, pnl_usd: float, reason: str,
                 portfolio: float) -> None:
    emoji = _EMOJI.get(reason, "🔔")
    mode  = config.TRADING_MODE.upper()
    ov.send_event(
        "TradingBot23 Closed",
        [
            f"{symbol} closed | {reason.upper()}",
            f"Entry ${entry_price:,.4f} -> Exit ${exit_price:,.4f}",
            f"Net P&L {pnl_pct:+.2f}% ({pnl_usd:+.2f} USD)",
            f"Portfolio ${portfolio:,.2f}",
        ],
        source="tradingbot23-futures",
    )
    _send(
        f"{emoji} <b>TradingBot23 [{mode}] — CLOSED</b>\n"
        f"Coin: <b>{symbol}</b>  |  Reason: <b>{reason.upper()}</b>\n"
        f"Entry: ${entry_price:,.4f}  →  Exit: ${exit_price:,.4f}\n"
        f"Net P&L: <b>{pnl_pct:+.2f}%  ({pnl_usd:+.2f} USD)</b>\n"
        f"Portfolio: <b>${portfolio:,.2f}</b>"
    )


def alert_crash(
    btc_change: float,
    positions_protected: int,
    *,
    emergency_sl_enabled: bool = True,
) -> None:
    mode = config.TRADING_MODE.upper()
    emergency_line = (
        f"Emergency SL armed on {positions_protected} position(s)."
        if emergency_sl_enabled
        else "Emergency SL is OFF; existing positions keep normal exits."
    )
    ov.send_event(
        "TradingBot23 Crash Mode",
        [
            f"BTC 24h change {btc_change:+.2f}%",
            "New futures entries blocked.",
            emergency_line,
        ],
        source="tradingbot23-risk",
    )
    emergency_text = (
        f"Emergency SL armed on <b>{positions_protected}</b> open position(s)"
        if emergency_sl_enabled
        else "Emergency SL is <b>OFF</b>; existing positions keep normal exits/cross liquidation"
    )
    _send(
        f"🚨 <b>TradingBot23 [{mode}] — CRASH MODE ACTIVATED</b>\n"
        f"BTC 24h change: <b>{btc_change:+.2f}%</b>\n"
        f"All new entries BLOCKED\n"
        f"{emergency_text}\n"
        f"Will resume when BTC recovers above {config.CRASH_BTC_RECOVERY_PCT * 100:+.1f}% 24h"
    )


def alert_crash_recovery(btc_change: float) -> None:
    mode = config.TRADING_MODE.upper()
    ov.send_event(
        "TradingBot23 Crash Lifted",
        [
            f"BTC 24h recovered to {btc_change:+.2f}%",
            "Normal futures scanning resumed.",
        ],
        source="tradingbot23-risk",
    )
    _send(
        f"✅ <b>TradingBot23 [{mode}] — CRASH MODE LIFTED</b>\n"
        f"BTC 24h change recovered to <b>{btc_change:+.2f}%</b>\n"
        f"Normal trading resumed"
    )


def alert_contribution(amount: float, cash: float, month: str) -> None:
    mode = config.TRADING_MODE.upper()
    ov.send_event(
        "TradingBot23 Contribution",
        [
            f"{month}: added ${amount:,.2f}",
            f"Cash balance ${cash:,.2f}",
        ],
        source="tradingbot23-futures",
    )
    _send(
        f"💵 <b>TradingBot23 [{mode}] — MONTHLY CONTRIBUTION</b>\n"
        f"Month: <b>{month}</b>\n"
        f"Added: <b>${amount:,.2f}</b>\n"
        f"Cash balance: <b>${cash:,.2f}</b>"
    )


def alert_summary(portfolio: float, cash: float, open_pos: int,
                  total_trades: int, win_rate: float, total_pnl_usd: float) -> None:
    mode = config.TRADING_MODE.upper()
    ov.send_event(
        "TradingBot23 Daily Summary",
        [
            f"Portfolio ${portfolio:,.2f} | Cash ${cash:,.2f}",
            f"Open {open_pos} | Trades {total_trades} | Win {win_rate:.1f}%",
            f"Total P&L {total_pnl_usd:+.2f} USD",
        ],
        source="tradingbot23-summary",
    )
    _send(
        f"{_EMOJI['summary']} <b>TradingBot23 [{mode}] — Daily Summary</b>\n"
        f"Portfolio: <b>${portfolio:,.2f}</b>  |  Cash: ${cash:,.2f}\n"
        f"Open positions: {open_pos}\n"
        f"Total trades: {total_trades}  |  Win rate: {win_rate:.1f}%\n"
        f"Total P&L: <b>{total_pnl_usd:+.2f} USD</b>"
    )
