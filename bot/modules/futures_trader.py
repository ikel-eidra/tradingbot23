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


class FuturesPositionStatus(str, Enum):
    OPEN = "open"
    TP_HIT = "tp_hit"
    SL_HIT = "sl_hit"
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

    def get_portfolio_value(self) -> float:
        """Cash balance + unrealized margin value using cached prices (no UI-thread API calls)."""
        unrealized = 0.0
        for pos in self.positions:
            if pos.status == FuturesPositionStatus.OPEN:
                price = pos.last_known_price or pos.entry_price
                price_change = (price - pos.entry_price) / pos.entry_price
                unrealized += pos.margin_used * (1 + price_change * pos.leverage)
        return self.cash_balance + unrealized

    @staticmethod
    def _mark_price(pos: FuturesPosition) -> float:
        return pos.last_known_price or pos.entry_price

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
            loss_states = (FuturesPositionStatus.SL_HIT, FuturesPositionStatus.LIQUIDATED)
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
        funding_drag = config.FUNDING_RATE_DAILY * self.leverage * 1.5  # ~1.5d avg hold
        gross_tp_move = (config.FUTURES_NET_TP_PCT + fee_drag + funding_drag) / self.leverage
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

        for pos in open_positions:
            if pos.status != FuturesPositionStatus.OPEN:
                continue
            price = pos.last_known_price
            if price is None:
                continue

            # Cross-margin liquidation safety check.
            if pos.liquidation_price > 0 and price <= pos.liquidation_price:
                self._close(pos, pos.liquidation_price, FuturesPositionStatus.LIQUIDATED, now)
                closed.append(pos)
                continue

            if config.FUTURES_USE_SL or pos.crash_protected:
                # Optional break-even trailing SL (only in normal mode, not crash)
                if (
                    config.FUTURES_USE_SL
                    and not pos.crash_protected
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
                    self._close(pos, pos.sl_price, FuturesPositionStatus.SL_HIT, now)
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
