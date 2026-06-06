"""Binance USDT-M Perpetual Futures trader (PAPER MODE ONLY).

This module simulates leveraged perpetual futures trading using current prices
from Binance, but executes no real orders. It is intentionally paper-only —
to enable real-money futures trading you must explicitly extend this class with
proper risk controls (margin checks, isolated/cross mode, OCO, etc.).

Modeled futures mechanics:
- Leverage amplifies both gains and losses
- Funding rate is paid every 8h (modeled as a daily drag on PNL)
- Cross-margin liquidation uses free cash plus all open-position equity
- Fees are charged on NOTIONAL value, not margin (so leverage multiplies fee drag)
"""

import csv
import json
import logging
import shutil
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum

from binance.client import Client as BinanceClient
from binance.exceptions import BinanceAPIException

from bot import config
from bot.modules import accounting
from bot.modules import telegram_notifier as tg
from bot.modules.event_ledger import append_event

logger = logging.getLogger(__name__)
MAINTENANCE_MARGIN_RATE = 0.005

_CSV_HEADER = [
    "open_time", "close_time", "symbol", "engine",
    "entry_price", "exit_price", "amount_usd", "notional", "leverage",
    "pnl_pct", "pnl_usd", "entry_fee", "exit_fee", "funding_paid",
    "pnl_model", "reason", "entry_change_24h",
]

_RESET_SESSION_HEADER = [
    "reset_time", "session_id", "archive_dir",
    "starting_capital_usd", "cash_balance",
    "archived_closed_trades", "archived_open_positions",
    "archived_realized_pnl_usd", "archived_open_margin_usd",
    "reason",
]


def _history_csv():
    return config.DATA_DIR / "trade_history.csv"


def _open_positions_json():
    return config.DATA_DIR / "open_positions.json"


def _reset_sessions_csv():
    return config.DATA_DIR / "futures_reset_sessions.csv"


def _append_trade_csv(row: dict) -> None:
    path = _history_csv()
    write_header = not path.exists()
    with open(path, "a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=_CSV_HEADER)
        if write_header:
            w.writeheader()
        w.writerow(row)


def _append_reset_session(row: dict) -> None:
    path = _reset_sessions_csv()
    path.parent.mkdir(parents=True, exist_ok=True)
    write_header = not path.exists()
    with open(path, "a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=_RESET_SESSION_HEADER)
        if write_header:
            w.writeheader()
        w.writerow({field: row.get(field, "") for field in _RESET_SESSION_HEADER})


def recent_reset_sessions(limit: int = 10) -> list[dict]:
    path = _reset_sessions_csv()
    if not path.exists():
        return []
    with open(path, "r", newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    return rows[-limit:]


def _migrate_trade_history_fee_model(path) -> None:
    """Normalize legacy rows whose pnl_usd omitted entry fees."""
    if not path.exists():
        return
    try:
        with open(path, "r", newline="", encoding="utf-8") as handle:
            reader = csv.DictReader(handle)
            rows = list(reader)
            old_fields = list(reader.fieldnames or [])
    except Exception:
        logger.debug("Could not inspect trade history for fee migration", exc_info=True)
        return

    if not rows:
        return

    changed = False
    for row in rows:
        if row.get("pnl_model") == "net_includes_entry_fee":
            continue
        try:
            notional = float(row.get("notional", 0.0) or 0.0)
            amount_usd = float(row.get("amount_usd", 0.0) or 0.0)
            entry_price = float(row.get("entry_price", 0.0) or 0.0)
            exit_price = float(row.get("exit_price", 0.0) or 0.0)
            legacy_pnl = float(row.get("pnl_usd", 0.0) or 0.0)
        except ValueError:
            continue
        entry_fee = notional * config.FUTURES_FEE_PCT
        quantity = notional / entry_price if entry_price > 0 else 0.0
        exit_fee = quantity * exit_price * config.FUTURES_FEE_PCT if exit_price > 0 else 0.0
        net_pnl = legacy_pnl - entry_fee
        row["entry_fee"] = f"{entry_fee:.8f}"
        row["exit_fee"] = f"{exit_fee:.8f}"
        row["pnl_usd"] = f"{net_pnl:.8f}"
        row["pnl_pct"] = f"{(net_pnl / amount_usd * 100):.8f}" if amount_usd > 0 else row.get("pnl_pct", "0")
        row["pnl_model"] = "net_includes_entry_fee"
        changed = True

    if not changed:
        return

    backup = path.with_suffix(".pre_fee_migration.csv")
    try:
        if not backup.exists():
            backup.write_text(path.read_text(encoding="utf-8"), encoding="utf-8")
        fieldnames = list(dict.fromkeys([*_CSV_HEADER, *old_fields]))
        with open(path, "w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=fieldnames)
            writer.writeheader()
            for row in rows:
                writer.writerow({field: row.get(field, "") for field in fieldnames})
        logger.info("Migrated trade history P&L to include entry fees: %s", path)
    except Exception:
        logger.exception("Failed to migrate trade history fee model")


def _hold_days_between(open_time: str, close_time: str) -> float:
    try:
        opened = datetime.fromisoformat(open_time)
        closed = datetime.fromisoformat(close_time)
        return max((closed - opened).total_seconds() / 86400, 0.0)
    except Exception:
        return 0.0


def _gross_tp_move_for_leverage(leverage: float, hold_days: float = 0.0) -> float:
    fee_drag = 2 * config.FUTURES_FEE_PCT * leverage
    funding_drag = config.FUNDING_RATE_DAILY * leverage * max(hold_days, 0.0)
    return (config.FUTURES_NET_TP_PCT + fee_drag + funding_drag) / leverage


def _recompute_net_pnl(
    entry_price: float,
    exit_price: float,
    amount_usd: float,
    notional: float,
    open_time: str,
    close_time: str,
) -> tuple[float, float, float, float]:
    quantity = notional / entry_price if entry_price > 0 else 0.0
    entry_fee = notional * config.FUTURES_FEE_PCT
    exit_fee = quantity * exit_price * config.FUTURES_FEE_PCT if exit_price > 0 else 0.0
    hold_days = _hold_days_between(open_time, close_time)
    funding_paid = notional * config.FUNDING_RATE_DAILY * hold_days
    gross_pnl = (exit_price - entry_price) * quantity
    pnl_usd = gross_pnl - entry_fee - exit_fee - funding_paid
    pnl_pct = (pnl_usd / amount_usd * 100) if amount_usd > 0 else 0.0
    return pnl_usd, pnl_pct, exit_fee, funding_paid


def _repair_positive_liquidation_history(path) -> None:
    """Correct legacy rows where upside TP winners were mislabeled liquidations."""
    if not path.exists():
        return
    try:
        with open(path, "r", newline="", encoding="utf-8") as handle:
            reader = csv.DictReader(handle)
            rows = list(reader)
            old_fields = list(reader.fieldnames or [])
    except Exception:
        logger.debug("Could not inspect trade history for positive-liq repair", exc_info=True)
        return

    changed = False
    for row in rows:
        if row.get("engine") != "futures" or row.get("reason") != FuturesPositionStatus.LIQUIDATED.value:
            continue
        try:
            entry_price = float(row.get("entry_price", 0.0) or 0.0)
            exit_price = float(row.get("exit_price", 0.0) or 0.0)
            amount_usd = float(row.get("amount_usd", 0.0) or 0.0)
            notional = float(row.get("notional", 0.0) or 0.0)
            leverage = float(row.get("leverage", 1.0) or 1.0)
            pnl_usd = float(row.get("pnl_usd", 0.0) or 0.0)
        except ValueError:
            continue

        if entry_price <= 0 or amount_usd <= 0 or notional <= 0:
            continue
        if exit_price <= entry_price or pnl_usd <= 0:
            continue

        hold_days = _hold_days_between(row.get("open_time", ""), row.get("close_time", ""))
        repaired_exit = entry_price * (1 + _gross_tp_move_for_leverage(leverage, hold_days))
        repaired_pnl, repaired_pct, repaired_exit_fee, repaired_funding = _recompute_net_pnl(
            entry_price,
            repaired_exit,
            amount_usd,
            notional,
            row.get("open_time", ""),
            row.get("close_time", ""),
        )
        row["exit_price"] = f"{repaired_exit:.8f}"
        row["pnl_pct"] = f"{repaired_pct:.8f}"
        row["pnl_usd"] = f"{repaired_pnl:.8f}"
        row["exit_fee"] = f"{repaired_exit_fee:.8f}"
        row["funding_paid"] = f"{repaired_funding:.8f}"
        row["pnl_model"] = "net_includes_entry_fee"
        row["reason"] = FuturesPositionStatus.TP_HIT.value
        changed = True

    if not changed:
        return

    backup = path.with_suffix(".pre_positive_liq_repair.csv")
    try:
        if not backup.exists():
            backup.write_text(path.read_text(encoding="utf-8"), encoding="utf-8")
        fieldnames = list(dict.fromkeys([*_CSV_HEADER, *old_fields]))
        with open(path, "w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=fieldnames)
            writer.writeheader()
            for row in rows:
                writer.writerow({field: row.get(field, "") for field in fieldnames})
        logger.warning("Repaired positive liquidation rows in trade history: %s", path)
    except Exception:
        logger.exception("Failed to repair positive liquidation history")


def _repair_tp_hit_history(path) -> None:
    """Normalize legacy TP rows to the configured net TP after fees/funding."""
    if not path.exists():
        return
    try:
        with open(path, "r", newline="", encoding="utf-8") as handle:
            reader = csv.DictReader(handle)
            rows = list(reader)
            old_fields = list(reader.fieldnames or [])
    except Exception:
        logger.debug("Could not inspect trade history for TP repair", exc_info=True)
        return

    target_pct = config.FUTURES_NET_TP_PCT * 100
    changed = False
    for row in rows:
        if row.get("engine") != "futures" or row.get("reason") != FuturesPositionStatus.TP_HIT.value:
            continue
        try:
            entry_price = float(row.get("entry_price", 0.0) or 0.0)
            amount_usd = float(row.get("amount_usd", 0.0) or 0.0)
            notional = float(row.get("notional", 0.0) or 0.0)
            leverage = float(row.get("leverage", 1.0) or 1.0)
            pnl_pct = float(row.get("pnl_pct", 0.0) or 0.0)
        except ValueError:
            continue

        if entry_price <= 0 or amount_usd <= 0 or notional <= 0:
            continue
        if abs(pnl_pct - target_pct) <= 0.08:
            continue

        hold_days = _hold_days_between(row.get("open_time", ""), row.get("close_time", ""))
        repaired_exit = entry_price * (1 + _gross_tp_move_for_leverage(leverage, hold_days))
        repaired_pnl, repaired_pct, repaired_exit_fee, repaired_funding = _recompute_net_pnl(
            entry_price,
            repaired_exit,
            amount_usd,
            notional,
            row.get("open_time", ""),
            row.get("close_time", ""),
        )
        row["exit_price"] = f"{repaired_exit:.8f}"
        row["pnl_pct"] = f"{repaired_pct:.8f}"
        row["pnl_usd"] = f"{repaired_pnl:.8f}"
        row["exit_fee"] = f"{repaired_exit_fee:.8f}"
        row["funding_paid"] = f"{repaired_funding:.8f}"
        row["pnl_model"] = "net_includes_entry_fee"
        changed = True

    if not changed:
        return

    backup = path.with_suffix(".pre_tp_repair.csv")
    try:
        if not backup.exists():
            backup.write_text(path.read_text(encoding="utf-8"), encoding="utf-8")
        fieldnames = list(dict.fromkeys([*_CSV_HEADER, *old_fields]))
        with open(path, "w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=fieldnames)
            writer.writeheader()
            for row in rows:
                writer.writerow({field: row.get(field, "") for field in fieldnames})
        logger.warning("Repaired TP-hit rows in trade history: %s", path)
    except Exception:
        logger.exception("Failed to repair TP-hit history")


class FuturesPositionStatus(str, Enum):
    OPEN = "open"
    TP_HIT = "tp_hit"
    SL_HIT = "sl_hit"
    CRASH_SL_HIT = "crash_sl"
    EXPIRED = "expired"
    LIQUIDATED = "liquidated"
    EXCLUDED = "excluded"


@dataclass
class FuturesPosition:
    """A simulated leveraged long position."""

    symbol: str
    entry_price: float
    quantity: float          # Coin units (notional / entry_price)
    margin_used: float       # USD margin locked
    notional: float          # USD notional value (margin × leverage)
    leverage: int
    entry_time: datetime
    tp_price: float
    sl_price: float
    liquidation_price: float
    status: FuturesPositionStatus = FuturesPositionStatus.OPEN
    exit_price: float | None = None
    exit_time: datetime | None = None
    pnl_pct: float = 0.0     # NET PNL on margin, after fees + funding
    pnl_usd: float = 0.0
    funding_paid: float = 0.0
    breakeven_armed: bool = False
    crash_protected: bool = False         # True when emergency crash SL has been armed
    last_known_price: float | None = None  # cached by trading thread, read by UI
    amount_usd: float = 0.0               # margin committed at entry
    entry_change_24h: float = 0.0         # 24h % change that triggered the buy
    margin_mode: str = "cross"


@dataclass
class PreTradeAnalysis:
    """Pre-entry wave/volatility analysis for a futures candidate."""

    symbol: str
    decision: str
    score: float
    reason: str
    current_price: float
    change_24h_pct: float
    change_4h_pct: float
    change_1h_pct: float
    rebound_from_low_pct: float
    range_24h_pct: float
    above_sma20_pct: float

    @property
    def allowed(self) -> bool:
        return self.decision == "RUN"

    def as_dict(self) -> dict:
        return {
            "symbol": self.symbol,
            "decision": self.decision,
            "score": self.score,
            "reason": self.reason,
            "current_price": self.current_price,
            "change_24h_pct": self.change_24h_pct,
            "change_4h_pct": self.change_4h_pct,
            "change_1h_pct": self.change_1h_pct,
            "rebound_from_low_pct": self.rebound_from_low_pct,
            "range_24h_pct": self.range_24h_pct,
            "above_sma20_pct": self.above_sma20_pct,
        }


def _pct_change(current: float, previous: float) -> float:
    return ((current - previous) / previous * 100) if previous > 0 else 0.0


def _recent_lower_close_streak(closes: list[float]) -> int:
    streak = 0
    for idx in range(len(closes) - 1, 0, -1):
        if closes[idx] < closes[idx - 1]:
            streak += 1
        else:
            break
    return streak


def analyze_pre_trade_klines(symbol: str, klines: list) -> PreTradeAnalysis:
    """Score whether a dip candidate has a tradable rebound structure.

    This is a deterministic paper-trading guard. It is not a prediction model;
    it simply refuses entries that are still pressing new lows, lack volatility,
    or show short-term momentum against the planned long.
    """
    if not klines or len(klines) < 20:
        return PreTradeAnalysis(
            symbol=symbol,
            decision="WAIT",
            score=0,
            reason="not enough candle data for pre-trade analysis",
            current_price=0.0,
            change_24h_pct=0.0,
            change_4h_pct=0.0,
            change_1h_pct=0.0,
            rebound_from_low_pct=0.0,
            range_24h_pct=0.0,
            above_sma20_pct=0.0,
        )

    closes = [float(k[4]) for k in klines if float(k[4]) > 0]
    highs = [float(k[2]) for k in klines if float(k[2]) > 0]
    lows = [float(k[3]) for k in klines if float(k[3]) > 0]
    if len(closes) < 20 or not highs or not lows:
        return PreTradeAnalysis(
            symbol=symbol,
            decision="WAIT",
            score=0,
            reason="invalid candle data for pre-trade analysis",
            current_price=0.0,
            change_24h_pct=0.0,
            change_4h_pct=0.0,
            change_1h_pct=0.0,
            rebound_from_low_pct=0.0,
            range_24h_pct=0.0,
            above_sma20_pct=0.0,
        )

    current = closes[-1]
    low_24h = min(lows)
    high_24h = max(highs)
    change_24h = _pct_change(current, closes[0])
    change_4h = _pct_change(current, closes[-17] if len(closes) >= 17 else closes[0])
    change_1h = _pct_change(current, closes[-5] if len(closes) >= 5 else closes[0])
    rebound_from_low = _pct_change(current, low_24h)
    range_24h = _pct_change(high_24h, low_24h)
    sma20 = sum(closes[-20:]) / 20
    above_sma20 = _pct_change(current, sma20)
    last_three_up = len(closes) >= 4 and closes[-1] > closes[-2] > closes[-3]
    lower_close_streak = _recent_lower_close_streak(closes)
    previous_sma20 = sum(closes[-28:-8]) / 20 if len(closes) >= 28 else sma20
    sma20_slope = _pct_change(sma20, previous_sma20)
    drawdown_from_high = _pct_change(current, high_24h)

    score = 50.0
    notes: list[str] = []
    blockers: list[str] = []

    if rebound_from_low >= config.PRE_TRADE_MIN_REBOUND_PCT:
        score += 20
    else:
        score -= 30
        blockers.append(f"no rebound from 24h low ({rebound_from_low:+.2f}%)")

    if change_1h >= 0.15:
        score += 15
    elif change_1h >= -0.20:
        score += 5
    elif change_1h <= -config.PRE_TRADE_MAX_1H_DROP_PCT:
        score -= 25
        blockers.append(f"1h still falling ({change_1h:+.2f}%)")
    else:
        score -= 10
        notes.append(f"soft 1h momentum {change_1h:+.2f}%")

    if change_4h > 0:
        score += 10
    elif change_4h <= -config.PRE_TRADE_MAX_4H_DROP_PCT:
        score -= 15
        blockers.append(f"4h downtrend {change_4h:+.2f}%")

    if range_24h >= config.PRE_TRADE_MIN_24H_RANGE_PCT:
        score += 10
    else:
        score -= 20
        blockers.append(f"low 24h range ({range_24h:.2f}%)")

    if above_sma20 > 0:
        score += 10
    else:
        score -= 5
        notes.append(f"below 20-candle avg {above_sma20:+.2f}%")

    if last_three_up:
        score += 10
    else:
        score -= 5
        notes.append("last candles not rising")

    if change_24h <= -10:
        score -= 10
        notes.append(f"deep 24h drop {change_24h:+.2f}%")

    if config.PRE_TRADE_BREAKDOWN_GUARD_ENABLED:
        if (
            change_24h <= -config.PRE_TRADE_MAX_24H_DROP_PCT
            and change_4h <= 0
            and change_1h <= 0
            and rebound_from_low < config.PRE_TRADE_MIN_BREAKDOWN_REBOUND_PCT
        ):
            score -= 35
            blockers.append(
                "breakdown guard: 24h/4h/1h still down with weak rebound "
                f"({rebound_from_low:+.2f}%)"
            )

        if lower_close_streak >= config.PRE_TRADE_MAX_LOWER_CLOSE_STREAK:
            score -= 25
            blockers.append(f"breakdown guard: {lower_close_streak} lower closes")

        if sma20_slope < 0 and above_sma20 <= -config.PRE_TRADE_MAX_BELOW_SMA20_PCT:
            score -= 20
            blockers.append(
                "breakdown guard: below falling 20-candle average "
                f"({above_sma20:+.2f}%, slope {sma20_slope:+.2f}%)"
            )

        if drawdown_from_high <= -config.PRE_TRADE_MAX_24H_DROP_PCT and rebound_from_low <= 0.10:
            score -= 20
            blockers.append(
                "breakdown guard: pinned near 24h low "
                f"(drawdown {drawdown_from_high:+.2f}%)"
            )

    score = max(0.0, min(100.0, score))
    decision = "RUN" if score >= config.PRE_TRADE_MIN_SCORE and not blockers else "WAIT"
    summary = (
        f"24h {change_24h:+.2f}% | 4h {change_4h:+.2f}% | 1h {change_1h:+.2f}% | "
        f"bounce {rebound_from_low:.2f}% | range {range_24h:.2f}%"
    )
    reason_parts = blockers or notes or ["wave structure acceptable"]
    reason = f"{summary}; {'; '.join(reason_parts[:5])}"

    return PreTradeAnalysis(
        symbol=symbol,
        decision=decision,
        score=score,
        reason=reason,
        current_price=current,
        change_24h_pct=change_24h,
        change_4h_pct=change_4h,
        change_1h_pct=change_1h,
        rebound_from_low_pct=rebound_from_low,
        range_24h_pct=range_24h,
        above_sma20_pct=above_sma20,
    )


class FuturesTrader:
    """Paper-mode futures trader using current Binance prices."""

    def __init__(self, api_key: str | None = None, api_secret: str | None = None):
        self.api_key = api_key or config.BINANCE_API_KEY
        self.api_secret = api_secret or config.BINANCE_API_SECRET
        # Force paper mode — there is no live futures execution path here.
        self.paper_mode = True
        self.leverage = max(1, min(config.LEVERAGE, config.MAX_LEVERAGE))
        if config.LEVERAGE > config.MAX_LEVERAGE:
            logger.warning(
                "LEVERAGE %dx exceeds MAX_LEVERAGE %dx — clamped to %dx for safety",
                config.LEVERAGE, config.MAX_LEVERAGE, config.MAX_LEVERAGE,
            )
        self.positions: list[FuturesPosition] = []
        self.account_capital_usd: float | None = None
        self.cash_balance = config.CAPITAL_USD  # Free margin
        self._client: BinanceClient | None = None
        self._load_trade_history()
        self._load_open_positions()
        self._reconcile_cash_balance_with_ledger()

        logger.info(
            "FuturesTrader initialized | Cross margin | Leverage: %dx | "
            "Fee: %.3f%% per side | Funding: %.3f%%/day | Net TP: %.3f%% | Net SL: %.3f%%",
            self.leverage, config.FUTURES_FEE_PCT * 100,
            config.FUNDING_RATE_DAILY * 100,
            config.FUTURES_NET_TP_PCT * 100, config.FUTURES_NET_SL_PCT * 100,
        )

    @property
    def client(self) -> BinanceClient:
        if self._client is None:
            self._client = BinanceClient(
                self.api_key, self.api_secret, testnet=config.BINANCE_TESTNET,
            )
        return self._client

    @staticmethod
    def _trading_pair(symbol: str) -> str:
        return f"{symbol}USDT"

    def get_current_price(self, symbol: str) -> float | None:
        """Fetch current price from Binance USDT-M futures."""
        pair = self._trading_pair(symbol)
        try:
            ticker = self.client.futures_symbol_ticker(symbol=pair)
            return float(ticker["price"])
        except BinanceAPIException:
            try:
                # Price-data fallback only. The app still never opens exchange orders.
                ticker = self.client.get_symbol_ticker(symbol=pair)
                return float(ticker["price"])
            except BinanceAPIException as e:
                logger.error("Failed to get price for %s: %s", pair, e)
                return None

    def get_5m_change(self, symbol: str) -> float | None:
        """Compute true 5-minute % price change from Binance kline data.

        Unlike `ticker['open']` (which is 24h open), this fetches the actual
        last two 5-minute candles and returns the close-to-close change.
        """
        pair = self._trading_pair(symbol)
        try:
            klines = self.client.futures_klines(
                symbol=pair, interval="5m", limit=2,
            )
        except BinanceAPIException:
            try:
                klines = self.client.get_klines(
                    symbol=pair, interval="5m", limit=2,
                )
            except BinanceAPIException as e:
                logger.debug("No kline data for %s: %s", pair, e)
                return None

        if len(klines) < 2:
            return None

        prev_close = float(klines[0][4])
        curr_close = float(klines[1][4])
        if prev_close <= 0:
            return None
        return ((curr_close - prev_close) / prev_close) * 100

    def get_kline_window_change(
        self,
        symbol: str,
        *,
        interval: str = "15m",
        limit: int = 5,
    ) -> float | None:
        """Compute close-to-close % change across a small kline window."""
        pair = self._trading_pair(symbol)
        try:
            klines = self.client.futures_klines(
                symbol=pair, interval=interval, limit=limit,
            )
        except BinanceAPIException:
            try:
                klines = self.client.get_klines(
                    symbol=pair, interval=interval, limit=limit,
                )
            except BinanceAPIException as e:
                logger.debug("No kline window data for %s: %s", pair, e)
                return None

        closes = [float(k[4]) for k in klines if float(k[4]) > 0]
        if len(closes) < 2 or closes[0] <= 0:
            return None
        return ((closes[-1] - closes[0]) / closes[0]) * 100

    def pre_trade_analysis(self, symbol: str) -> PreTradeAnalysis:
        """Analyze 24h wave structure before a new futures paper entry."""
        pair = self._trading_pair(symbol)
        try:
            klines = self.client.futures_klines(
                symbol=pair,
                interval=config.PRE_TRADE_KLINE_INTERVAL,
                limit=config.PRE_TRADE_KLINE_LIMIT,
            )
        except BinanceAPIException:
            try:
                klines = self.client.get_klines(
                    symbol=pair,
                    interval=config.PRE_TRADE_KLINE_INTERVAL,
                    limit=config.PRE_TRADE_KLINE_LIMIT,
                )
            except BinanceAPIException as e:
                logger.debug("No pre-trade kline data for %s: %s", pair, e)
                return PreTradeAnalysis(
                    symbol=symbol,
                    decision="WAIT",
                    score=0,
                    reason=f"no Binance kline data for {pair}",
                    current_price=0.0,
                    change_24h_pct=0.0,
                    change_4h_pct=0.0,
                    change_1h_pct=0.0,
                    rebound_from_low_pct=0.0,
                    range_24h_pct=0.0,
                    above_sma20_pct=0.0,
                )
        return analyze_pre_trade_klines(symbol, klines)

    def get_portfolio_value(self) -> float:
        """Cash balance + unrealized margin value using cached prices (no UI-thread API calls)."""
        equity, _maintenance = self._account_equity_and_maintenance()
        return equity

    @staticmethod
    def _mark_price(pos: FuturesPosition) -> float:
        return pos.last_known_price or pos.entry_price

    def _account_equity_and_maintenance(
        self,
        positions: list[FuturesPosition] | None = None,
    ) -> tuple[float, float]:
        """Return cross-account equity and maintenance requirement at cached marks."""
        open_positions = [
            p for p in (positions or self.get_open_positions())
            if p.status == FuturesPositionStatus.OPEN
        ]
        equity = self.cash_balance
        maintenance = 0.0
        for pos in open_positions:
            mark = self._mark_price(pos)
            equity += pos.margin_used + ((mark - pos.entry_price) * pos.quantity)
            maintenance += mark * pos.quantity * MAINTENANCE_MARGIN_RATE
        return equity, maintenance

    def _cross_liquidation_price(
        self,
        target: FuturesPosition,
        positions: list[FuturesPosition] | None = None,
        cash_balance: float | None = None,
    ) -> float:
        """Approximate cross-margin liquidation price for a long position.

        The model solves for the target mark price where account equity equals
        maintenance margin, assuming other open positions stay at their latest
        known mark. This is intentionally conservative paper math, not an
        exchange-exact Binance liquidation formula.
        """
        if target.quantity <= 0:
            return 0.0
        open_positions = [
            p for p in (positions or self.get_open_positions())
            if p.status == FuturesPositionStatus.OPEN
        ]
        cash = self.cash_balance if cash_balance is None else cash_balance

        other_equity = 0.0
        other_maintenance = 0.0
        for pos in open_positions:
            if pos is target:
                continue
            mark = self._mark_price(pos)
            other_equity += pos.margin_used + ((mark - pos.entry_price) * pos.quantity)
            other_maintenance += mark * pos.quantity * MAINTENANCE_MARGIN_RATE

        numerator = (
            other_maintenance
            - cash
            - target.margin_used
            - other_equity
            + (target.quantity * target.entry_price)
        )
        denominator = target.quantity * (1 - MAINTENANCE_MARGIN_RATE)
        if denominator <= 0:
            return 0.0
        price = numerator / denominator
        return max(0.0, price)

    def refresh_cross_liquidation_prices(self) -> None:
        open_positions = self.get_open_positions()
        for pos in open_positions:
            pos.margin_mode = "cross"
            pos.liquidation_price = self._cross_liquidation_price(pos, open_positions)

    def open_position(
        self, symbol: str, margin_usd: float | None = None,
        entry_price: float | None = None, entry_change_24h: float = 0.0,
    ) -> FuturesPosition | None:
        """Open a paper futures long with TP, SL, and liquidation tracking."""
        symbol = symbol.upper()
        if config.is_futures_excluded_symbol(symbol):
            logger.warning("Skipping %s — excluded from futures paper universe", symbol)
            return None

        self._normalize_cash_balance()
        for pos in self.positions:
            if pos.symbol == symbol and pos.status == FuturesPositionStatus.OPEN:
                logger.warning("Already have open futures position for %s, skipping", symbol)
                return None

        now = datetime.now(timezone.utc)

        # Loss cooldown: skip if recent SL or LIQUIDATION on this symbol
        if config.LOSS_COOLDOWN_HOURS > 0:
            cooldown = timedelta(hours=config.LOSS_COOLDOWN_HOURS)
            loss_states = (
                FuturesPositionStatus.SL_HIT,
                FuturesPositionStatus.CRASH_SL_HIT,
                FuturesPositionStatus.LIQUIDATED,
            )
            for pos in self.positions:
                if (
                    pos.symbol == symbol
                    and pos.status in loss_states
                    and pos.exit_time is not None
                    and (now - pos.exit_time) < cooldown
                ):
                    remaining = cooldown - (now - pos.exit_time)
                    logger.info(
                        "[COOLDOWN-FUT] %s — recent loss, skipping (%.1fh left)",
                        symbol, remaining.total_seconds() / 3600,
                    )
                    return None

        # TP cooldown: skip if this coin was just closed on a TP hit recently
        if config.TP_COOLDOWN_HOURS > 0:
            tp_cooldown = timedelta(hours=config.TP_COOLDOWN_HOURS)
            for pos in self.positions:
                if (
                    pos.symbol == symbol
                    and pos.status == FuturesPositionStatus.TP_HIT
                    and pos.exit_time is not None
                    and (now - pos.exit_time) < tp_cooldown
                ):
                    remaining = tp_cooldown - (now - pos.exit_time)
                    logger.info(
                        "[TP-COOLDOWN] %s — just hit TP, waiting %.0fm before re-entry",
                        symbol, remaining.total_seconds() / 60,
                    )
                    return None

        portfolio_value = self.get_portfolio_value()
        margin_usd = margin_usd or (portfolio_value * config.PER_TRADE_PCT)

        max_margin_with_entry_fee = self.cash_balance / (1 + self.leverage * config.FUTURES_FEE_PCT)
        if margin_usd > max_margin_with_entry_fee:
            margin_usd = max_margin_with_entry_fee
        if margin_usd < 10:
            logger.warning("Cash too low ($%.2f) — skipping %s", margin_usd, symbol)
            return None

        market_price = self.get_current_price(symbol)
        if market_price is None:
            logger.warning("Skipping %s — no Binance USDT price data available", symbol)
            return None
        price = market_price

        notional = margin_usd * self.leverage
        quantity = notional / price
        entry_fee = notional * config.FUTURES_FEE_PCT

        # Compute gross price moves to deliver NET targets on margin.
        # PNL_on_margin = price_change% × leverage − fee_drag − funding_drag
        # fee_drag (round trip) = 2 × FUTURES_FEE_PCT × leverage (charged on notional)
        fee_drag = 2 * config.FUTURES_FEE_PCT * self.leverage
        funding_drag = config.FUNDING_RATE_DAILY * self.leverage * 1.5  # conservative SL buffer
        gross_tp_move = _gross_tp_move_for_leverage(self.leverage, 0.0)
        gross_sl_move = (config.FUTURES_NET_SL_PCT - fee_drag - funding_drag) / self.leverage
        gross_sl_move = max(gross_sl_move, 0.001)

        tp_price = price * (1 + gross_tp_move)
        sl_price = price * (1 - gross_sl_move)
        projected_cash = self.cash_balance - margin_usd - entry_fee

        position = FuturesPosition(
            symbol=symbol,
            entry_price=price,
            quantity=quantity,
            margin_used=margin_usd,
            notional=notional,
            leverage=self.leverage,
            entry_time=datetime.now(timezone.utc),
            tp_price=tp_price,
            sl_price=sl_price,
            liquidation_price=0.0,
            amount_usd=margin_usd,
            entry_change_24h=entry_change_24h,
            margin_mode="cross",
        )
        projected_positions = self.get_open_positions() + [position]
        liq_price = self._cross_liquidation_price(
            position, projected_positions, projected_cash,
        )

        # Safety: SL must be ABOVE cross liquidation price.
        if sl_price <= liq_price:
            logger.warning(
                "%s: SL price $%.4f would breach cross liquidation $%.4f at %dx leverage. "
                "Reduce leverage, reduce position size, or tighten FUTURES_NET_SL_PCT.",
                symbol, sl_price, liq_price, self.leverage,
            )
            return None

        self.cash_balance = projected_cash
        self._normalize_cash_balance()
        position.liquidation_price = liq_price
        self.positions.append(position)
        self.refresh_cross_liquidation_prices()
        self._save_open_positions()

        logger.info(
            "[PAPER-FUT] LONG %s %.6f @ $%.4f | CROSS Margin $%.2f | Notional $%.2f | "
            "TP $%.4f | SL $%.4f | Cross LIQ $%.4f | Fee $%.2f",
            symbol, quantity, price, margin_usd, notional,
            tp_price, sl_price, position.liquidation_price, entry_fee,
        )
        self._append_event(
            event_type="OPEN",
            status="OPEN",
            amount=margin_usd,
            pnl=0.0,
            balance=self.get_portfolio_value(),
            symbol_or_route=symbol,
            details=f"{self.leverage}x notional ${notional:.2f} entry ${price:.4f}",
        )
        tg.alert_opened(symbol, price, tp_price, position.liquidation_price,
                        margin_usd, self.leverage, entry_change_24h)
        return position

    def check_positions(self) -> list[FuturesPosition]:
        """Check open positions for liquidation/TP/SL/expiry."""
        closed = []
        now = datetime.now(timezone.utc)

        open_positions = self.get_open_positions()
        for pos in open_positions:
            if config.is_futures_excluded_symbol(pos.symbol):
                pos.last_known_price = pos.entry_price
                self._close(pos, pos.entry_price, FuturesPositionStatus.EXCLUDED, now)
                closed.append(pos)
                continue
            try:
                price = self.get_current_price(pos.symbol)
            except Exception:
                price = None
            if price is None:
                continue
            pos.last_known_price = price

        self.refresh_cross_liquidation_prices()

        account_equity, maintenance_margin = self._account_equity_and_maintenance(open_positions)
        if open_positions and account_equity <= maintenance_margin:
            logger.warning(
                "[PAPER-FUT] CROSS ACCOUNT LIQUIDATION | equity $%.2f <= maintenance $%.2f",
                account_equity,
                maintenance_margin,
            )
            for pos in open_positions:
                if pos.status != FuturesPositionStatus.OPEN:
                    continue
                price = pos.last_known_price or pos.entry_price
                self._close(pos, price, FuturesPositionStatus.LIQUIDATED, now)
                closed.append(pos)
            self.refresh_cross_liquidation_prices()
            self._save_open_positions()
            return closed

        for pos in open_positions:
            if pos.status != FuturesPositionStatus.OPEN:
                continue
            price = pos.last_known_price
            if price is None:
                continue

            pos.tp_price = self._net_tp_price(pos, now)

            # Cross-margin liquidation safety check.
            if self._is_downside_liquidation_trigger(pos, price):
                self._close(pos, price, FuturesPositionStatus.LIQUIDATED, now)
                closed.append(pos)
                continue

            crash_stop_active = pos.crash_protected and config.CRASH_EMERGENCY_SL_ENABLED
            normal_stop_active = config.FUTURES_USE_SL and not pos.crash_protected
            if normal_stop_active or crash_stop_active:
                # Optional break-even trailing SL (only in normal mode, not crash)
                if (
                    normal_stop_active
                    and not pos.breakeven_armed
                    and config.BREAK_EVEN_TRIGGER_PCT > 0
                    and price >= pos.entry_price * (1 + config.BREAK_EVEN_TRIGGER_PCT)
                ):
                    fee_drag_price = (2 * config.FUTURES_FEE_PCT * pos.leverage) / pos.leverage
                    new_sl = pos.entry_price * (1 + fee_drag_price)
                    if new_sl > pos.sl_price and new_sl > pos.liquidation_price:
                        logger.info("[BREAK-EVEN-FUT] %s armed — SL $%.4f -> $%.4f",
                                    pos.symbol, pos.sl_price, new_sl)
                        pos.sl_price = new_sl
                        pos.breakeven_armed = True

                if price <= pos.sl_price:
                    reason = (
                        FuturesPositionStatus.CRASH_SL_HIT
                        if crash_stop_active
                        else FuturesPositionStatus.SL_HIT
                    )
                    self._close(pos, pos.sl_price, reason, now)
                    closed.append(pos)
                    continue

            # TP hit
            if price >= pos.tp_price:
                self._close(pos, pos.tp_price, FuturesPositionStatus.TP_HIT, now)
                closed.append(pos)
                continue

            # Max hold — exit at market, top-50 coins rebound given time
            if (now - pos.entry_time) > timedelta(days=config.MAX_HOLD_DAYS):
                self._close(pos, price, FuturesPositionStatus.EXPIRED, now)
                closed.append(pos)
                continue

        if closed:
            self.refresh_cross_liquidation_prices()
            self._save_open_positions()
        return closed

    @staticmethod
    def _is_downside_liquidation_trigger(pos: FuturesPosition, price: float) -> bool:
        """For a long, liquidation is only a downside threshold below entry."""
        liq = pos.liquidation_price
        return liq > 0 and liq < pos.entry_price and price <= liq

    @staticmethod
    def _net_tp_price(pos: FuturesPosition, now: datetime) -> float:
        hold_days = max((now - pos.entry_time).total_seconds() / 86400, 0.0)
        return pos.entry_price * (1 + _gross_tp_move_for_leverage(pos.leverage, hold_days))

    def _close(
        self,
        pos: FuturesPosition,
        exit_price: float,
        reason: FuturesPositionStatus,
        now: datetime,
    ):
        """Close a futures position with full fee + funding accounting."""
        pos.status = reason
        pos.exit_price = exit_price
        pos.exit_time = now

        hold_days = max((now - pos.entry_time).total_seconds() / 86400, 0)
        funding_cost = pos.notional * config.FUNDING_RATE_DAILY * hold_days
        pos.funding_paid = funding_cost

        exit_notional = pos.quantity * exit_price
        entry_fee = self._entry_fee_for(pos)
        exit_fee = exit_notional * config.FUTURES_FEE_PCT

        gross_pnl_usd = (exit_price - pos.entry_price) * pos.quantity
        cash_pnl_usd = gross_pnl_usd - exit_fee - funding_cost
        net_pnl_usd = cash_pnl_usd - entry_fee
        pos.pnl_usd = net_pnl_usd
        pos.pnl_pct = (net_pnl_usd / pos.margin_used) * 100

        if reason == FuturesPositionStatus.LIQUIDATED:
            self.cash_balance += pos.margin_used + cash_pnl_usd
            if self.cash_balance < 0:
                self.cash_balance = 0.0
            self._normalize_cash_balance()
            logger.warning(
                "[PAPER-FUT] LIQUIDATED %s @ $%.4f | Cross PNL: %+.2f%% (%+.2f USD) | Cash: $%.2f",
                pos.symbol, exit_price, pos.pnl_pct, pos.pnl_usd, self.cash_balance,
            )
            self._save_trade(pos)
            self._append_event(
                event_type="CLOSE",
                status=reason.value,
                amount=pos.amount_usd,
                pnl=pos.pnl_usd,
                balance=self.cash_balance,
                symbol_or_route=pos.symbol,
                details=f"exit ${exit_price:.4f} pnl {pos.pnl_pct:+.2f}%",
            )
            tg.alert_closed(pos.symbol, pos.entry_price, exit_price,
                            pos.pnl_pct, pos.pnl_usd, reason.value, self.cash_balance)
            return

        self.cash_balance += pos.margin_used + cash_pnl_usd
        self._normalize_cash_balance()
        logger.info(
            "[PAPER-FUT] CLOSE %s @ $%.4f | %s | NET PNL: %+.2f%% (%+.2f USD) | "
            "Entry fee $%.2f | Exit fee $%.2f | Funding $%.2f | Cash: $%.2f",
            pos.symbol, exit_price, reason.value, pos.pnl_pct, net_pnl_usd,
            entry_fee, exit_fee, funding_cost, self.cash_balance,
        )
        self._save_trade(pos)
        self._append_event(
            event_type="CLOSE",
            status=reason.value,
            amount=pos.amount_usd,
            pnl=pos.pnl_usd,
            balance=self.cash_balance,
            symbol_or_route=pos.symbol,
            details=f"exit ${exit_price:.4f} pnl {pos.pnl_pct:+.2f}%",
        )
        tg.alert_closed(pos.symbol, pos.entry_price, exit_price,
                        pos.pnl_pct, pos.pnl_usd, reason.value, self.cash_balance)

    def _save_trade(self, pos: FuturesPosition) -> None:
        try:
            _append_trade_csv({
                "open_time":       pos.entry_time.isoformat(),
                "close_time":      pos.exit_time.isoformat() if pos.exit_time else "",
                "symbol":          pos.symbol,
                "engine":          "futures",
                "entry_price":     round(pos.entry_price, 8),
                "exit_price":      round(pos.exit_price, 8) if pos.exit_price else "",
                "amount_usd":      round(pos.amount_usd, 4),
                "notional":        round(pos.notional, 4),
                "leverage":        pos.leverage,
                "pnl_pct":         round(pos.pnl_pct, 4),
                "pnl_usd":         round(pos.pnl_usd, 4),
                "entry_fee":       round(self._entry_fee_for(pos), 8),
                "exit_fee":        round(
                    (pos.quantity * pos.exit_price * config.FUTURES_FEE_PCT)
                    if pos.exit_price else 0.0,
                    8,
                ),
                "funding_paid":    round(pos.funding_paid, 4),
                "pnl_model":       "net_includes_entry_fee",
                "reason":          pos.status.value,
                "entry_change_24h": round(pos.entry_change_24h, 4),
            })
        except Exception:
            logger.exception("Failed to save trade to CSV")

    def _load_trade_history(self) -> None:
        path = _history_csv()
        if not path.exists():
            self.cash_balance = accounting.total_contributed_capital()
            return
        _migrate_trade_history_fee_model(path)
        _repair_positive_liquidation_history(path)
        _repair_tp_hit_history(path)
        loaded = 0
        with open(path, "r", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                if row.get("engine") != "futures":
                    continue
                try:
                    status = FuturesPositionStatus(row["reason"])
                    pos = FuturesPosition(
                        symbol=row["symbol"],
                        entry_price=float(row["entry_price"]),
                        quantity=0.0,
                        margin_used=float(row["amount_usd"]),
                        notional=float(row["notional"]),
                        leverage=int(row["leverage"]),
                        entry_time=datetime.fromisoformat(row["open_time"]),
                        tp_price=0.0,
                        sl_price=0.0,
                        liquidation_price=0.0,
                        status=status,
                        exit_price=float(row["exit_price"]) if row.get("exit_price") else None,
                        exit_time=datetime.fromisoformat(row["close_time"]) if row.get("close_time") else None,
                        pnl_pct=float(row["pnl_pct"]),
                        pnl_usd=float(row["pnl_usd"]),
                        funding_paid=float(row["funding_paid"]),
                        amount_usd=float(row["amount_usd"]),
                        entry_change_24h=float(row["entry_change_24h"]),
                    )
                    self.positions.append(pos)
                    loaded += 1
                except Exception:
                    logger.debug("Skipped unreadable history row", exc_info=True)
        past_pnl = sum(p.pnl_usd for p in self.positions)
        self.cash_balance = accounting.total_contributed_capital() + past_pnl
        self._normalize_cash_balance()
        if loaded:
            logger.info(
                "Loaded %d closed trades | Past P&L: $%+.2f | Restored balance: $%.2f",
                loaded, past_pnl, self.cash_balance,
            )

    def apply_monthly_contribution(self, now: datetime | None = None) -> float:
        """Apply the configured monthly paper contribution once per month."""
        self.cash_balance, amount = accounting.apply_monthly_contribution(self.cash_balance, now)
        self._normalize_cash_balance()
        if amount:
            self.refresh_cross_liquidation_prices()
            self._save_open_positions()
            self._append_event(
                event_type="CONTRIBUTION",
                status="APPLIED",
                amount=amount,
                pnl=0.0,
                balance=self.cash_balance,
                symbol_or_route="USD",
                details="monthly paper contribution",
            )
        return amount

    def get_contributed_capital(self) -> float:
        return accounting.total_contributed_capital()

    @staticmethod
    def _entry_fee_for(pos: FuturesPosition) -> float:
        return pos.notional * config.FUTURES_FEE_PCT

    def infer_starting_capital(self) -> float:
        """Infer pre-metadata starting capital from the paper account ledger."""
        state = accounting.load_account_state()
        monthly_contributions = float(state.get("total_contributed_usd", 0.0))
        closed_pnl = sum(p.pnl_usd for p in self.get_trade_history())
        entry_fees = sum(self._entry_fee_for(p) for p in self.positions)
        open_margin = sum(p.margin_used for p in self.get_open_positions())
        return self.cash_balance - monthly_contributions - closed_pnl + entry_fees + open_margin

    def starting_capital_delta(self, new_capital: float) -> float:
        new_capital = float(new_capital)
        if new_capital <= 0:
            raise ValueError("capital must be positive")
        current_capital = (
            self.account_capital_usd
            if self.account_capital_usd is not None
            else self.infer_starting_capital()
        )
        return new_capital - current_capital

    def sync_starting_capital(self, new_capital: float) -> float:
        """Apply a Settings capital change to the live paper cash balance.

        Open positions keep their original margin/leverage. Increasing capital
        behaves like a paper deposit into free cash; decreasing capital behaves
        like a withdrawal and is rejected if free cash is insufficient.
        """
        new_capital = float(new_capital)
        delta = self.starting_capital_delta(new_capital)
        if abs(delta) < 0.01:
            self.account_capital_usd = new_capital
            self._save_open_positions()
            return 0.0

        if self.cash_balance + delta < -0.005:
            raise ValueError(
                f"capital decrease needs ${abs(delta):,.2f} free cash, "
                f"but only ${self.cash_balance:,.2f} is available"
            )

        self.cash_balance += delta
        self._normalize_cash_balance()
        self.account_capital_usd = new_capital
        self.refresh_cross_liquidation_prices()
        self._save_open_positions()
        return delta

    def reset_paper_account(self, reason: str = "manual_reset") -> dict:
        """Archive the current futures paper session and start a fresh one.

        Current futures history/open positions are moved out of the active
        session by archiving the files, then clearing in-memory positions. P2P
        state and the monthly contribution ledger are intentionally left alone.
        """
        now = datetime.now(timezone.utc)
        session_id = f"reset_{now.strftime('%Y%m%d_%H%M%S')}"
        archive_dir = config.DATA_DIR / "futures_sessions" / session_id
        suffix = 1
        while archive_dir.exists():
            archive_dir = config.DATA_DIR / "futures_sessions" / f"{session_id}_{suffix}"
            suffix += 1
        archive_dir.mkdir(parents=True, exist_ok=False)

        closed_positions = self.get_trade_history()
        open_positions = self.get_open_positions()
        archived_realized_pnl = sum(p.pnl_usd for p in closed_positions)
        archived_open_margin = sum(p.margin_used for p in open_positions)

        for path in (
            _history_csv(),
            _open_positions_json(),
            config.DATA_DIR / "account_state.json",
        ):
            if path.exists():
                shutil.copy2(path, archive_dir / path.name)

        history_path = _history_csv()
        if history_path.exists():
            history_path.unlink()

        self.positions = []
        self.account_capital_usd = config.CAPITAL_USD
        self.cash_balance = accounting.total_contributed_capital()
        self._normalize_cash_balance()
        self._save_open_positions()

        summary = {
            "reset_time": now.isoformat(),
            "session_id": archive_dir.name,
            "archive_dir": str(archive_dir),
            "starting_capital_usd": config.CAPITAL_USD,
            "cash_balance": self.cash_balance,
            "archived_closed_trades": len(closed_positions),
            "archived_open_positions": len(open_positions),
            "archived_realized_pnl_usd": archived_realized_pnl,
            "archived_open_margin_usd": archived_open_margin,
            "reason": reason,
        }
        _append_reset_session(summary)

        self._append_event(
            event_type="RESET",
            status="APPLIED",
            amount=self.cash_balance,
            pnl=0.0,
            balance=self.cash_balance,
            symbol_or_route="FUTURES",
            details=(
                f"archived {len(closed_positions)} closed and "
                f"{len(open_positions)} open to {archive_dir.name}"
            ),
        )
        logger.warning(
            "Futures paper reset | Archived %d closed / %d open to %s | New cash: $%.2f",
            len(closed_positions),
            len(open_positions),
            archive_dir,
            self.cash_balance,
        )
        return summary

    def arm_crash_sl(self) -> int:
        """Set emergency SL on all open positions at current_price × (1 - CRASH_SL_PCT).

        Called when crash mode activates. Returns number of positions protected.
        """
        protected = 0
        self.refresh_cross_liquidation_prices()
        for pos in self.positions:
            if pos.status != FuturesPositionStatus.OPEN:
                continue
            price = self.get_current_price(pos.symbol)
            if price is None:
                price = pos.last_known_price or pos.entry_price
            emergency_sl = price * (1 - config.CRASH_SL_PCT)
            # Only set if above liquidation price (safety)
            if emergency_sl > pos.liquidation_price:
                pos.sl_price = emergency_sl
                pos.crash_protected = True
                protected += 1
                logger.warning(
                    "[CRASH-SL] %s — emergency SL set at $%.4f (%.1f%% below current $%.4f)",
                    pos.symbol, emergency_sl, config.CRASH_SL_PCT * 100, price,
                )
            else:
                logger.warning(
                    "[CRASH-SL] %s — emergency SL $%.4f would breach liquidation $%.4f, skipping",
                    pos.symbol, emergency_sl, pos.liquidation_price,
                )
        if protected:
            self._save_open_positions()
        return protected

    def _save_open_positions(self) -> None:
        """Persist open positions and current cash balance to disk."""
        self._normalize_cash_balance()
        open_pos = [p for p in self.positions if p.status == FuturesPositionStatus.OPEN]
        data = {
            "cash_balance": self.cash_balance,
            "starting_capital_usd": (
                self.account_capital_usd
                if self.account_capital_usd is not None
                else config.CAPITAL_USD
            ),
            "total_contributed_capital": accounting.total_contributed_capital(),
            "positions": [
                {
                    "symbol":            p.symbol,
                    "entry_price":       p.entry_price,
                    "quantity":          p.quantity,
                    "margin_used":       p.margin_used,
                    "notional":          p.notional,
                    "leverage":          p.leverage,
                    "entry_time":        p.entry_time.isoformat(),
                    "tp_price":          p.tp_price,
                    "sl_price":          p.sl_price,
                    "liquidation_price": p.liquidation_price,
                    "amount_usd":        p.amount_usd,
                    "entry_change_24h":  p.entry_change_24h,
                    "breakeven_armed":   p.breakeven_armed,
                    "crash_protected":   p.crash_protected,
                    "margin_mode":       "cross",
                }
                for p in open_pos
            ],
        }
        try:
            with open(_open_positions_json(), "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)
        except Exception:
            logger.exception("Failed to save open positions to disk")

    def _load_open_positions(self) -> None:
        """Restore open positions and exact cash balance from disk on startup."""
        path = _open_positions_json()
        if not path.exists():
            return
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if data.get("starting_capital_usd") is not None:
                self.account_capital_usd = float(data["starting_capital_usd"])
            restored = 0
            for p in data.get("positions", []):
                pos = FuturesPosition(
                    symbol=p["symbol"],
                    entry_price=p["entry_price"],
                    quantity=p["quantity"],
                    margin_used=p["margin_used"],
                    notional=p["notional"],
                    leverage=p["leverage"],
                    entry_time=datetime.fromisoformat(p["entry_time"]),
                    tp_price=p["tp_price"],
                    sl_price=p["sl_price"],
                    liquidation_price=p["liquidation_price"],
                    amount_usd=p["amount_usd"],
                    entry_change_24h=p["entry_change_24h"],
                    breakeven_armed=p.get("breakeven_armed", False),
                    crash_protected=p.get("crash_protected", False),
                    margin_mode=p.get("margin_mode", "cross"),
                    status=FuturesPositionStatus.OPEN,
                )
                self.positions.append(pos)
                restored += 1
            self.cash_balance = data.get("cash_balance", self.cash_balance)
            self._normalize_cash_balance()
            if self.account_capital_usd is None:
                inferred_capital = self.infer_starting_capital()
                self.account_capital_usd = inferred_capital
                try:
                    synced_delta = self.sync_starting_capital(config.CAPITAL_USD)
                    if abs(synced_delta) >= 0.01:
                        logger.info(
                            "Synced legacy paper capital %.2f -> %.2f | Cash delta: %+.2f",
                            inferred_capital, config.CAPITAL_USD, synced_delta,
                        )
                except ValueError as e:
                    logger.warning("Could not auto-sync legacy paper capital: %s", e)
            logger.info(
                "Restored %d open positions from disk | Cash: $%.2f",
                restored, self.cash_balance,
            )
            if restored:
                self.refresh_cross_liquidation_prices()
                self._save_open_positions()
        except Exception:
            logger.exception("Failed to load open positions from disk")

    def _cash_balance_from_ledger(self) -> float:
        closed_pnl = sum(p.pnl_usd for p in self.get_trade_history())
        open_positions = self.get_open_positions()
        open_margin = sum(p.margin_used for p in open_positions)
        open_entry_fees = sum(self._entry_fee_for(p) for p in open_positions)
        return accounting.total_contributed_capital() + closed_pnl - open_margin - open_entry_fees

    def _reconcile_cash_balance_with_ledger(self) -> None:
        expected_cash = self._cash_balance_from_ledger()
        if abs(expected_cash - self.cash_balance) < 0.01:
            return
        logger.warning(
            "Reconciled futures free cash from ledger | Saved: $%.2f | Ledger: $%.2f",
            self.cash_balance,
            expected_cash,
        )
        self.cash_balance = expected_cash
        self._normalize_cash_balance()
        self.refresh_cross_liquidation_prices()
        self._save_open_positions()

    def _normalize_cash_balance(self) -> None:
        """Avoid tiny floating-point cash dust showing as negative zero."""
        if abs(self.cash_balance) < 0.01:
            self.cash_balance = 0.0

    @staticmethod
    def _append_event(
        *,
        event_type: str,
        status: str,
        amount: float,
        pnl: float,
        balance: float,
        symbol_or_route: str,
        details: str,
    ) -> None:
        try:
            append_event(
                domain="futures",
                event_type=event_type,
                status=status,
                amount=amount,
                currency="USD",
                pnl=pnl,
                balance=balance,
                symbol_or_route=symbol_or_route,
                details=details,
            )
        except Exception:
            logger.debug("Failed to append futures event ledger row", exc_info=True)

    def get_open_positions(self) -> list[FuturesPosition]:
        return [p for p in self.positions if p.status == FuturesPositionStatus.OPEN]

    def get_trade_history(self) -> list[FuturesPosition]:
        return [p for p in self.positions if p.status != FuturesPositionStatus.OPEN]

    def get_stats(self) -> dict:
        closed = [
            p for p in self.get_trade_history()
            if p.status != FuturesPositionStatus.EXCLUDED
        ]
        if not closed:
            return {"total_trades": 0, "win_rate": 0, "avg_pnl": 0, "total_pnl": 0,
                    "liquidations": 0}

        wins = [p for p in closed if p.pnl_pct > 0]
        liqs = [p for p in closed if p.status == FuturesPositionStatus.LIQUIDATED]
        total_pnl = sum(p.pnl_pct for p in closed)

        return {
            "total_trades": len(closed),
            "wins": len(wins),
            "losses": len(closed) - len(wins),
            "liquidations": len(liqs),
            "win_rate": len(wins) / len(closed) * 100,
            "avg_pnl": total_pnl / len(closed),
            "total_pnl": total_pnl,
            "best_trade": max(closed, key=lambda p: p.pnl_pct).pnl_pct,
            "worst_trade": min(closed, key=lambda p: p.pnl_pct).pnl_pct,
            "total_funding_paid": sum(p.funding_paid for p in closed),
        }
