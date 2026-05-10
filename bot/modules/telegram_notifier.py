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

import requests

from bot import config

logger = logging.getLogger(__name__)

_EMOJI = {
    "tp_hit":    "✅",
    "sl_hit":    "❌",
    "expired":   "⏰",
    "liquidated":"💀",
    "open":      "🟢",
    "fill":      "🔵",
    "summary":   "📊",
    "error":     "⚠️",
}


def _send(text: str) -> None:
    """Fire-and-forget Telegram message. Runs in a background thread."""
    if not config.TELEGRAM_BOT_TOKEN or not config.TELEGRAM_CHAT_ID:
        return

    def _post():
        try:
            requests.post(
                f"https://api.telegram.org/bot{config.TELEGRAM_BOT_TOKEN}/sendMessage",
                json={
                    "chat_id":    config.TELEGRAM_CHAT_ID,
                    "text":       text,
                    "parse_mode": "HTML",
                },
                timeout=8,
            )
        except Exception as e:
            logger.debug("Telegram send failed: %s", e)

    threading.Thread(target=_post, daemon=True).start()


def alert_opened(symbol: str, entry_price: float, tp_price: float,
                 sl_price: float, margin_usd: float, leverage: int,
                 change_24h: float, filled: bool = False) -> None:
    emoji = _EMOJI["fill"] if filled else _EMOJI["open"]
    tag   = "FILLED (no dip)" if filled else "DIP ENTRY"
    mode  = config.TRADING_MODE.upper()
    _send(
        f"{emoji} <b>TradingBot23 [{mode}] — {tag}</b>\n"
        f"Coin: <b>{symbol}</b>\n"
        f"Entry: <b>${entry_price:,.4f}</b>  ({change_24h:+.2f}% 24h)\n"
        f"Margin: <b>${margin_usd:.2f}</b>  |  Leverage: <b>{leverage}x</b>\n"
        f"TP: ${tp_price:,.4f}  |  LIQ: ${sl_price:,.4f}"
    )


def alert_closed(symbol: str, entry_price: float, exit_price: float,
                 pnl_pct: float, pnl_usd: float, reason: str,
                 portfolio: float) -> None:
    emoji = _EMOJI.get(reason, "🔔")
    mode  = config.TRADING_MODE.upper()
    _send(
        f"{emoji} <b>TradingBot23 [{mode}] — CLOSED</b>\n"
        f"Coin: <b>{symbol}</b>  |  Reason: <b>{reason.upper()}</b>\n"
        f"Entry: ${entry_price:,.4f}  →  Exit: ${exit_price:,.4f}\n"
        f"Net P&L: <b>{pnl_pct:+.2f}%  ({pnl_usd:+.2f} USD)</b>\n"
        f"Portfolio: <b>${portfolio:,.2f}</b>"
    )


def alert_crash(btc_change: float, positions_protected: int) -> None:
    mode = config.TRADING_MODE.upper()
    _send(
        f"🚨 <b>TradingBot23 [{mode}] — CRASH MODE ACTIVATED</b>\n"
        f"BTC 24h change: <b>{btc_change:+.2f}%</b>\n"
        f"All new entries BLOCKED\n"
        f"Emergency SL armed on <b>{positions_protected}</b> open position(s)\n"
        f"Will resume when BTC recovers above -5% 24h"
    )


def alert_crash_recovery(btc_change: float) -> None:
    mode = config.TRADING_MODE.upper()
    _send(
        f"✅ <b>TradingBot23 [{mode}] — CRASH MODE LIFTED</b>\n"
        f"BTC 24h change recovered to <b>{btc_change:+.2f}%</b>\n"
        f"Normal trading resumed"
    )


def alert_contribution(amount: float, cash: float, month: str) -> None:
    mode = config.TRADING_MODE.upper()
    _send(
        f"💵 <b>TradingBot23 [{mode}] — MONTHLY CONTRIBUTION</b>\n"
        f"Month: <b>{month}</b>\n"
        f"Added: <b>${amount:,.2f}</b>\n"
        f"Cash balance: <b>${cash:,.2f}</b>"
    )


def alert_summary(portfolio: float, cash: float, open_pos: int,
                  total_trades: int, win_rate: float, total_pnl_usd: float) -> None:
    mode = config.TRADING_MODE.upper()
    _send(
        f"{_EMOJI['summary']} <b>TradingBot23 [{mode}] — Daily Summary</b>\n"
        f"Portfolio: <b>${portfolio:,.2f}</b>  |  Cash: ${cash:,.2f}\n"
        f"Open positions: {open_pos}\n"
        f"Total trades: {total_trades}  |  Win rate: {win_rate:.1f}%\n"
        f"Total P&L: <b>{total_pnl_usd:+.2f} USD</b>"
    )
