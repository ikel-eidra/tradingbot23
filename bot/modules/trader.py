"""Binance spot trading module.

Handles order placement (market buy, limit sell TP, stop-limit SL),
position tracking, and order management.
"""

import csv
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum

from binance.client import Client as BinanceClient
from binance.exceptions import BinanceAPIException

from bot import config
from bot.modules import accounting
from bot.modules.futures_trader import _append_trade_csv

logger = logging.getLogger(__name__)


class OrderSide(str, Enum):
    BUY = "BUY"
    SELL = "SELL"


class PositionStatus(str, Enum):
    OPEN = "open"
    TP_HIT = "tp_hit"
    SL_HIT = "sl_hit"
    EXPIRED = "expired"  # max hold exceeded
    CLOSED = "closed"


@dataclass
class Position:
    """Represents an open trading position."""

    symbol: str
    entry_price: float
    quantity: float
    entry_time: datetime
    tp_price: float
    sl_price: float
    status: PositionStatus = PositionStatus.OPEN
    exit_price: float | None = None
    exit_time: datetime | None = None
    pnl_pct: float = 0.0
    pnl_usd: float = 0.0
    buy_order_id: str | None = None
    tp_order_id: str | None = None
    sl_order_id: str | None = None
    breakeven_armed: bool = False
    last_known_price: float | None = None  # updated by trading thread, read by UI
    amount_usd: float = 0.0               # USD invested at entry
    entry_change_24h: float = 0.0         # 24h % change that triggered the buy


class Trader:
    """Binance spot trader with TP/SL management."""

    def __init__(self, api_key: str | None = None, api_secret: str | None = None):
        self.api_key = api_key or config.BINANCE_API_KEY
        self.api_secret = api_secret or config.BINANCE_API_SECRET
        self.paper_mode = config.TRADING_MODE != "live"
        self.positions: list[Position] = []
        self.initial_capital = config.CAPITAL_USD
        self.cash_balance = config.CAPITAL_USD  # Tracks available cash (paper mode)
        self._client: BinanceClient | None = None
        self._symbol_info_cache: dict = {}
        self._load_trade_history()

    @property
    def client(self) -> BinanceClient:
        """Lazy-initialize Binance client."""
        if self._client is None:
            testnet = config.BINANCE_TESTNET
            if self.paper_mode:
                logger.info("Running in PAPER mode — no real orders will be placed")
                self._client = BinanceClient(
                    self.api_key, self.api_secret, testnet=testnet,
                )
            else:
                self._client = BinanceClient(
                    self.api_key, self.api_secret, testnet=testnet,
                )
                self._client.ping()
                logger.info(
                    "Connected to Binance API (LIVE mode%s)",
                    " — TESTNET" if testnet else "",
                )
        return self._client

    def _get_trading_pair(self, symbol: str) -> str:
        """Convert coin symbol to Binance trading pair (e.g., BTC -> BTCUSDT)."""
        return f"{symbol}USDT"

    def _get_symbol_info(self, pair: str) -> dict | None:
        """Fetch and cache symbol trading rules from Binance."""
        if pair not in self._symbol_info_cache:
            try:
                info = self.client.get_symbol_info(pair)
                self._symbol_info_cache[pair] = info
            except BinanceAPIException as e:
                logger.error("Cannot get symbol info for %s: %s", pair, e)
                return None
        return self._symbol_info_cache[pair]

    def _get_lot_size(self, pair: str) -> dict | None:
        """Get LOT_SIZE filter for a trading pair."""
        info = self._get_symbol_info(pair)
        if info is None:
            return None
        for f in info.get("filters", []):
            if f["filterType"] == "LOT_SIZE":
                return f
        return None

    def _adjust_quantity(self, pair: str, quantity: float) -> float:
        """Adjust quantity to meet Binance LOT_SIZE requirements."""
        lot = self._get_lot_size(pair)
        if lot is None:
            return quantity
        step_size = float(lot["stepSize"])
        min_qty = float(lot["minQty"])
        if step_size > 0:
            # Round down to nearest step
            quantity = (quantity // step_size) * step_size
        return max(quantity, min_qty)

    def _round_price(self, pair: str, price: float) -> float:
        """Round price to meet Binance PRICE_FILTER requirements."""
        info = self._get_symbol_info(pair)
        if info is None:
            return round(price, 8)
        for f in info.get("filters", []):
            if f["filterType"] == "PRICE_FILTER":
                tick_size = float(f["tickSize"])
                if tick_size > 0:
                    price = (price // tick_size) * tick_size
                return price
        return round(price, 8)

    def get_current_price(self, symbol: str) -> float | None:
        """Fetch current price from Binance."""
        pair = self._get_trading_pair(symbol)
        try:
            ticker = self.client.get_symbol_ticker(symbol=pair)
            return float(ticker["price"])
        except BinanceAPIException as e:
            logger.error("Failed to get price for %s: %s", pair, e)
            return None

    def open_position(self, symbol: str, amount_usd: float | None = None, entry_price: float | None = None, entry_change_24h: float = 0.0) -> Position | None:
        """Open a new spot position with TP and SL.

        Args:
            symbol: Coin symbol (e.g., "BTC").
            amount_usd: USD amount to spend. Defaults to config.

        Returns:
            Position object if successful, None otherwise.
        """
        # Check if we already have an open position for this coin
        for pos in self.positions:
            if pos.symbol == symbol and pos.status == PositionStatus.OPEN:
                logger.warning("Already have an open position for %s, skipping", symbol)
                return None

        # Cooldown: skip if a recent SL hit on this symbol
        if config.LOSS_COOLDOWN_HOURS > 0:
            now = datetime.now(timezone.utc)
            cooldown = timedelta(hours=config.LOSS_COOLDOWN_HOURS)
            for pos in self.positions:
                if (
                    pos.symbol == symbol
                    and pos.status == PositionStatus.SL_HIT
                    and pos.exit_time is not None
                    and (now - pos.exit_time) < cooldown
                ):
                    remaining = cooldown - (now - pos.exit_time)
                    logger.info(
                        "[COOLDOWN] %s — recent SL %.1fh ago, skipping (%.1fh left)",
                        symbol, (now - pos.exit_time).total_seconds() / 3600,
                        remaining.total_seconds() / 3600,
                    )
                    return None

        pair = self._get_trading_pair(symbol)
        # Dynamic compounding: use current portfolio value, not initial capital
        portfolio_value = self.get_portfolio_value()
        amount_usd = amount_usd or (portfolio_value * config.PER_TRADE_PCT)

        # Safety check: don't exceed available cash
        if amount_usd > self.cash_balance:
            logger.warning(
                "Insufficient cash ($%.2f) for $%.2f trade on %s, reducing to available",
                self.cash_balance, amount_usd, symbol,
            )
            amount_usd = self.cash_balance
            if amount_usd < 10:  # Minimum trade size
                logger.warning("Cash balance too low ($%.2f), skipping %s", amount_usd, symbol)
                return None

        current_price = entry_price or self.get_current_price(symbol)
        if current_price is None:
            return None

        quantity = amount_usd / current_price
        quantity = self._adjust_quantity(pair, quantity)

        tp_price = self._round_price(pair, current_price * (1 + config.TP_PCT))
        sl_price = self._round_price(pair, current_price * (1 - config.SL_PCT))

        if self.paper_mode:
            # Paper mode: simulate the buy, deduct cost + Binance fee from cash
            gross_cost = quantity * current_price
            fee = gross_cost * config.FEE_PCT
            total_cost = gross_cost + fee
            self.cash_balance -= total_cost
            logger.info(
                "[PAPER] BUY %s %.6f @ $%.4f (cost $%.2f + fee $%.2f) | TP $%.4f | SL $%.4f | Cash: $%.2f",
                symbol, quantity, current_price, gross_cost, fee, tp_price, sl_price, self.cash_balance,
            )
            position = Position(
                symbol=symbol,
                entry_price=current_price,
                quantity=quantity,
                entry_time=datetime.now(timezone.utc),
                tp_price=tp_price,
                sl_price=sl_price,
                buy_order_id="PAPER",
                amount_usd=gross_cost,
                entry_change_24h=entry_change_24h,
            )
            self.positions.append(position)
            return position

        # Live mode: place market buy, then OCO sell (TP + SL)
        try:
            buy_order = self.client.order_market_buy(
                symbol=pair,
                quantity=quantity,
            )
            filled_price = float(buy_order["fills"][0]["price"]) if buy_order["fills"] else current_price
            filled_qty = float(buy_order["executedQty"])

            # Recalculate TP/SL based on actual fill price
            tp_price = self._round_price(pair, filled_price * (1 + config.TP_PCT))
            sl_price = self._round_price(pair, filled_price * (1 - config.SL_PCT))
            # SL limit price slightly below stop price to ensure fill
            sl_limit_price = self._round_price(pair, sl_price * 0.998)

            logger.info(
                "[LIVE] BUY %s %.6f @ $%.4f (order %s) | TP $%.4f | SL $%.4f",
                symbol, filled_qty, filled_price, buy_order["orderId"], tp_price, sl_price,
            )

            # Place OCO order: TP (limit sell) + SL (stop-limit sell) in one call.
            # This ensures both TP and SL are on the exchange — if one fills,
            # Binance automatically cancels the other.
            oco_order = self.client.create_oco_order(
                symbol=pair,
                side="SELL",
                quantity=filled_qty,
                price=str(tp_price),           # Limit sell (TP)
                stopPrice=str(sl_price),       # Stop trigger (SL)
                stopLimitPrice=str(sl_limit_price),  # Limit price after stop triggers
                stopLimitTimeInForce="GTC",
            )

            # Extract order IDs from OCO response
            tp_order_id = None
            sl_order_id = None
            for order_report in oco_order.get("orderReports", []):
                if order_report.get("type") == "LIMIT_MAKER":
                    tp_order_id = str(order_report["orderId"])
                elif order_report.get("type") == "STOP_LOSS_LIMIT":
                    sl_order_id = str(order_report["orderId"])

            position = Position(
                symbol=symbol,
                entry_price=filled_price,
                quantity=filled_qty,
                entry_time=datetime.now(timezone.utc),
                tp_price=tp_price,
                sl_price=sl_price,
                buy_order_id=str(buy_order["orderId"]),
                tp_order_id=tp_order_id,
                sl_order_id=sl_order_id,
                amount_usd=filled_qty * filled_price,
                entry_change_24h=entry_change_24h,
            )
            self.positions.append(position)
            return position

        except BinanceAPIException as e:
            logger.error("Failed to open position for %s: %s", symbol, e)
            return None

    def check_positions(self) -> list[Position]:
        """Check all open positions for TP/SL/expiry and close as needed.

        Returns:
            List of positions that were closed in this check.
        """
        closed = []
        now = datetime.now(timezone.utc)

        for pos in self.positions:
            if pos.status != PositionStatus.OPEN:
                continue

            try:
                current_price = self.get_current_price(pos.symbol)
            except Exception:
                current_price = None
            if current_price is None:
                continue
            pos.last_known_price = current_price

            # Break-even SL: once price moves +BREAK_EVEN_TRIGGER_PCT in our
            # favor, slide SL up to entry+fees so we exit flat on a reversal.
            if (
                not pos.breakeven_armed
                and config.BREAK_EVEN_TRIGGER_PCT > 0
                and current_price >= pos.entry_price * (1 + config.BREAK_EVEN_TRIGGER_PCT)
            ):
                new_sl = pos.entry_price * (1 + 2 * config.FEE_PCT)
                if new_sl > pos.sl_price:
                    logger.info(
                        "[BREAK-EVEN] %s armed — SL moved $%.4f -> $%.4f",
                        pos.symbol, pos.sl_price, new_sl,
                    )
                    pos.sl_price = new_sl
                    pos.breakeven_armed = True

            # Check TP
            if current_price >= pos.tp_price:
                self._close_position(pos, current_price, PositionStatus.TP_HIT)
                closed.append(pos)
                continue

            # Check SL
            if current_price <= pos.sl_price:
                self._close_position(pos, current_price, PositionStatus.SL_HIT)
                closed.append(pos)
                continue

            # Check max hold
            hold_duration = now - pos.entry_time
            if hold_duration > timedelta(days=config.MAX_HOLD_DAYS):
                self._close_position(pos, current_price, PositionStatus.EXPIRED)
                closed.append(pos)
                continue

        return closed

    def _close_position(self, pos: Position, exit_price: float, reason: PositionStatus):
        """Close a position. PNL is computed NET of round-trip Binance fees."""
        pos.status = reason
        pos.exit_price = exit_price
        pos.exit_time = datetime.now(timezone.utc)

        # Net PNL accounts for both buy fee and sell fee.
        # entry effective cost per unit = entry_price * (1 + FEE_PCT)
        # exit effective proceeds per unit = exit_price * (1 - FEE_PCT)
        entry_cost = pos.entry_price * (1 + config.FEE_PCT)
        exit_proceeds = exit_price * (1 - config.FEE_PCT)
        pos.pnl_pct = ((exit_proceeds - entry_cost) / entry_cost) * 100
        pos.pnl_usd = (exit_proceeds - entry_cost) * pos.quantity

        self._save_trade(pos)

        if self.paper_mode:
            # Return proceeds (minus sell fee) to cash balance
            gross_proceeds = pos.quantity * exit_price
            sell_fee = gross_proceeds * config.FEE_PCT
            net_proceeds = gross_proceeds - sell_fee
            self.cash_balance += net_proceeds
            logger.info(
                "[PAPER] CLOSE %s @ $%.4f | Reason: %s | NET PNL: %+.2f%% | Fee: $%.2f | Cash: $%.2f",
                pos.symbol, exit_price, reason.value, pos.pnl_pct, sell_fee, self.cash_balance,
            )
        else:
            # Cancel any open TP/SL orders and market sell
            pair = self._get_trading_pair(pos.symbol)
            for order_id in (pos.tp_order_id, pos.sl_order_id):
                if order_id and order_id != "PAPER":
                    try:
                        self.client.cancel_order(symbol=pair, orderId=int(order_id))
                    except BinanceAPIException:
                        pass  # Order may already be filled/cancelled

            try:
                self.client.order_market_sell(symbol=pair, quantity=pos.quantity)
                logger.info(
                    "[LIVE] CLOSE %s @ $%.4f | Reason: %s | PNL: %+.2f%%",
                    pos.symbol, exit_price, reason.value, pos.pnl_pct,
                )
            except BinanceAPIException as e:
                logger.error("Failed to close position for %s: %s", pos.symbol, e)

    def get_portfolio_value(self) -> float:
        """Calculate current portfolio value (cash + open position values).

        Uses last_known_price cached by the trading thread to avoid blocking
        the UI thread with live API calls.
        """
        open_value = 0.0
        for pos in self.positions:
            if pos.status == PositionStatus.OPEN:
                price = pos.last_known_price or pos.entry_price
                open_value += pos.quantity * price
        total = self.cash_balance + open_value
        logger.debug("Portfolio value: $%.2f (cash: $%.2f, open: $%.2f)", total, self.cash_balance, open_value)
        return total

    def _save_trade(self, pos: Position) -> None:
        try:
            _append_trade_csv({
                "open_time":        pos.entry_time.isoformat(),
                "close_time":       pos.exit_time.isoformat() if pos.exit_time else "",
                "symbol":           pos.symbol,
                "engine":           "spot",
                "entry_price":      round(pos.entry_price, 8),
                "exit_price":       round(pos.exit_price, 8) if pos.exit_price else "",
                "amount_usd":       round(pos.amount_usd, 4),
                "notional":         round(pos.amount_usd, 4),
                "leverage":         1,
                "pnl_pct":          round(pos.pnl_pct, 4),
                "pnl_usd":          round(pos.pnl_usd, 4),
                "funding_paid":     0,
                "reason":           pos.status.value,
                "entry_change_24h": round(pos.entry_change_24h, 4),
            })
        except Exception:
            logger.exception("Failed to save trade to CSV")

    def _load_trade_history(self) -> None:
        from bot.modules.futures_trader import _history_csv  # noqa: PLC0415
        path = _history_csv()
        if not path.exists():
            self.cash_balance = accounting.total_contributed_capital()
            return
        loaded = 0
        with open(path, "r", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                if row.get("engine") != "spot":
                    continue
                try:
                    status = PositionStatus(row["reason"])
                    pos = Position(
                        symbol=row["symbol"],
                        entry_price=float(row["entry_price"]),
                        quantity=0.0,
                        entry_time=datetime.fromisoformat(row["open_time"]),
                        tp_price=0.0,
                        sl_price=0.0,
                        status=status,
                        exit_price=float(row["exit_price"]) if row.get("exit_price") else None,
                        exit_time=datetime.fromisoformat(row["close_time"]) if row.get("close_time") else None,
                        pnl_pct=float(row["pnl_pct"]),
                        pnl_usd=float(row["pnl_usd"]),
                        amount_usd=float(row["amount_usd"]),
                        entry_change_24h=float(row["entry_change_24h"]),
                    )
                    self.positions.append(pos)
                    loaded += 1
                except Exception:
                    logger.debug("Skipped unreadable history row", exc_info=True)
        past_pnl = sum(p.pnl_usd for p in self.positions)
        self.cash_balance = accounting.total_contributed_capital() + past_pnl
        if loaded:
            logger.info(
                "Loaded %d closed spot trades | Past P&L: $%+.2f | Restored balance: $%.2f",
                loaded, past_pnl, self.cash_balance,
            )

    def apply_monthly_contribution(self, now: datetime | None = None) -> float:
        """Apply the configured monthly paper contribution once per month."""
        if not self.paper_mode:
            return 0.0
        self.cash_balance, amount = accounting.apply_monthly_contribution(self.cash_balance, now)
        return amount

    def get_contributed_capital(self) -> float:
        return accounting.total_contributed_capital()

    def get_open_positions(self) -> list[Position]:
        """Return all currently open positions."""
        return [p for p in self.positions if p.status == PositionStatus.OPEN]

    def get_trade_history(self) -> list[Position]:
        """Return all closed positions."""
        return [p for p in self.positions if p.status != PositionStatus.OPEN]

    def get_stats(self) -> dict:
        """Calculate trading statistics."""
        closed = self.get_trade_history()
        if not closed:
            return {"total_trades": 0, "win_rate": 0, "avg_pnl": 0, "total_pnl": 0}

        wins = [p for p in closed if p.pnl_pct > 0]
        total_pnl = sum(p.pnl_pct for p in closed)

        return {
            "total_trades": len(closed),
            "wins": len(wins),
            "losses": len(closed) - len(wins),
            "win_rate": len(wins) / len(closed) * 100,
            "avg_pnl": total_pnl / len(closed),
            "total_pnl": total_pnl,
            "best_trade": max(closed, key=lambda p: p.pnl_pct).pnl_pct,
            "worst_trade": min(closed, key=lambda p: p.pnl_pct).pnl_pct,
        }
