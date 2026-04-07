"""Binance USDT-M Perpetual Futures trader (PAPER MODE ONLY).

This module simulates leveraged perpetual futures trading using LIVE prices
from Binance, but executes no real orders. It is intentionally paper-only —
to enable live futures trading you must explicitly extend this class with
proper risk controls (margin checks, isolated/cross mode, OCO, etc.).

Key differences vs spot:
- Leverage amplifies both gains and losses
- Funding rate is paid every 8h (modeled as a daily drag on PNL)
- Liquidation can wipe a position if price moves against you by ~1/leverage
- Fees are charged on NOTIONAL value, not margin (so leverage multiplies fee drag)
"""

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum

from binance.client import Client as BinanceClient
from binance.exceptions import BinanceAPIException

from bot import config

logger = logging.getLogger(__name__)


class FuturesPositionStatus(str, Enum):
    OPEN = "open"
    TP_HIT = "tp_hit"
    SL_HIT = "sl_hit"
    EXPIRED = "expired"
    LIQUIDATED = "liquidated"


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


class FuturesTrader:
    """Paper-mode futures trader using live Binance prices."""

    def __init__(self, api_key: str | None = None, api_secret: str | None = None):
        self.api_key = api_key or config.BINANCE_API_KEY
        self.api_secret = api_secret or config.BINANCE_API_SECRET
        # Force paper mode — there is no live futures execution path here.
        self.paper_mode = True
        self.leverage = min(config.LEVERAGE, config.MAX_LEVERAGE)
        if config.LEVERAGE > config.MAX_LEVERAGE:
            logger.warning(
                "LEVERAGE %dx exceeds MAX_LEVERAGE %dx — clamped to %dx for safety",
                config.LEVERAGE, config.MAX_LEVERAGE, config.MAX_LEVERAGE,
            )
        self.positions: list[FuturesPosition] = []
        self.cash_balance = config.CAPITAL_USD  # Free margin
        self._client: BinanceClient | None = None

        logger.info(
            "FuturesTrader initialized | Leverage: %dx | Fee: %.3f%% per side | "
            "Funding: %.3f%%/day | Net TP: %.3f%% | Net SL: %.3f%%",
            self.leverage, config.FUTURES_FEE_PCT * 100,
            config.FUNDING_RATE_DAILY * 100,
            config.FUTURES_NET_TP_PCT * 100, config.FUTURES_NET_SL_PCT * 100,
        )
        # Liquidation distance warning (rough: 1/leverage minus maintenance margin ~0.5%)
        liq_distance = (1 / self.leverage) - 0.005
        logger.info(
            "⚠️  Liquidation distance: ~%.2f%% adverse price move = total margin loss",
            liq_distance * 100,
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
        """Fetch current price from Binance USDT-M futures (or spot fallback)."""
        pair = self._trading_pair(symbol)
        try:
            ticker = self.client.futures_symbol_ticker(symbol=pair)
            return float(ticker["price"])
        except BinanceAPIException:
            try:
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
        """Cash balance + unrealized margin value of open positions."""
        unrealized = 0.0
        for pos in self.positions:
            if pos.status == FuturesPositionStatus.OPEN:
                price = self.get_current_price(pos.symbol)
                if price is None:
                    unrealized += pos.margin_used
                    continue
                price_change = (price - pos.entry_price) / pos.entry_price
                unrealized += pos.margin_used * (1 + price_change * pos.leverage)
        return self.cash_balance + unrealized

    def _liquidation_price(self, entry: float, leverage: int) -> float:
        """Approximate liquidation price for a long position.

        Real Binance formula uses maintenance margin rate (~0.5% for most pairs).
        Simplified: liq ≈ entry × (1 - 1/leverage + maint_margin_rate)
        """
        return entry * (1 - (1 / leverage) + 0.005)

    def open_position(
        self, symbol: str, margin_usd: float | None = None
    ) -> FuturesPosition | None:
        """Open a paper futures long with TP, SL, and liquidation tracking."""
        for pos in self.positions:
            if pos.symbol == symbol and pos.status == FuturesPositionStatus.OPEN:
                logger.warning("Already have open futures position for %s, skipping", symbol)
                return None

        portfolio_value = self.get_portfolio_value()
        margin_usd = margin_usd or (portfolio_value * config.PER_TRADE_PCT)

        if margin_usd > self.cash_balance:
            margin_usd = self.cash_balance
        if margin_usd < 10:
            logger.warning("Cash too low ($%.2f) — skipping %s", margin_usd, symbol)
            return None

        price = self.get_current_price(symbol)
        if price is None:
            return None

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
        liq_price = self._liquidation_price(price, self.leverage)

        # Safety: SL must be ABOVE liquidation price
        if sl_price <= liq_price:
            logger.warning(
                "%s: SL price $%.4f would breach liquidation $%.4f at %dx leverage. "
                "Reduce leverage or tighten NET_SL_PCT.",
                symbol, sl_price, liq_price, self.leverage,
            )
            return None

        self.cash_balance -= margin_usd
        self.cash_balance -= entry_fee

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
            liquidation_price=liq_price,
        )
        self.positions.append(position)

        logger.info(
            "[PAPER-FUT] LONG %s %.6f @ $%.4f | Margin $%.2f | Notional $%.2f | "
            "TP $%.4f | SL $%.4f | LIQ $%.4f | Fee $%.2f",
            symbol, quantity, price, margin_usd, notional,
            tp_price, sl_price, liq_price, entry_fee,
        )
        return position

    def check_positions(self) -> list[FuturesPosition]:
        """Check open positions for liquidation/TP/SL/expiry."""
        closed = []
        now = datetime.now(timezone.utc)

        for pos in self.positions:
            if pos.status != FuturesPositionStatus.OPEN:
                continue

            price = self.get_current_price(pos.symbol)
            if price is None:
                continue

            # Liquidation check FIRST — worst case wins
            if price <= pos.liquidation_price:
                self._close(pos, pos.liquidation_price, FuturesPositionStatus.LIQUIDATED, now)
                closed.append(pos)
                continue

            if price >= pos.tp_price:
                self._close(pos, pos.tp_price, FuturesPositionStatus.TP_HIT, now)
                closed.append(pos)
                continue

            if price <= pos.sl_price:
                self._close(pos, pos.sl_price, FuturesPositionStatus.SL_HIT, now)
                closed.append(pos)
                continue

            if (now - pos.entry_time) > timedelta(days=config.MAX_HOLD_DAYS):
                self._close(pos, price, FuturesPositionStatus.EXPIRED, now)
                closed.append(pos)
                continue

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
        exit_fee = exit_notional * config.FUTURES_FEE_PCT

        gross_pnl_usd = (exit_price - pos.entry_price) * pos.quantity
        net_pnl_usd = gross_pnl_usd - exit_fee - funding_cost
        pos.pnl_usd = net_pnl_usd
        pos.pnl_pct = (net_pnl_usd / pos.margin_used) * 100

        if reason == FuturesPositionStatus.LIQUIDATED:
            pos.pnl_pct = -100.0
            pos.pnl_usd = -pos.margin_used
            logger.warning(
                "[PAPER-FUT] 💀 LIQUIDATED %s @ $%.4f | Lost margin $%.2f | Cash: $%.2f",
                pos.symbol, exit_price, pos.margin_used, self.cash_balance,
            )
            return

        self.cash_balance += pos.margin_used + net_pnl_usd
        logger.info(
            "[PAPER-FUT] CLOSE %s @ $%.4f | %s | NET PNL: %+.2f%% (%+$%.2f) | "
            "Fee $%.2f | Funding $%.2f | Cash: $%.2f",
            pos.symbol, exit_price, reason.value, pos.pnl_pct, net_pnl_usd,
            exit_fee, funding_cost, self.cash_balance,
        )

    def get_open_positions(self) -> list[FuturesPosition]:
        return [p for p in self.positions if p.status == FuturesPositionStatus.OPEN]

    def get_trade_history(self) -> list[FuturesPosition]:
        return [p for p in self.positions if p.status != FuturesPositionStatus.OPEN]

    def get_stats(self) -> dict:
        closed = self.get_trade_history()
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
