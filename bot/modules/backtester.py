"""Backtesting engine for the top losers futures strategy.

Uses Binance USDT-M futures kline (candlestick) data to simulate the strategy
over a configurable date range.
"""

import datetime as dt
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

import requests

from bot import config

logger = logging.getLogger(__name__)


@dataclass
class BacktestTrade:
    """A single trade in a backtest."""

    symbol: str
    entry_date: datetime
    entry_price: float
    exit_date: datetime | None = None
    exit_price: float | None = None
    tp_price: float = 0.0
    sl_price: float = 0.0
    quantity: float = 0.0
    margin_usd: float = 0.0
    entry_fee_usd: float = 0.0
    leverage: int = 1
    pnl_pct: float = 0.0
    pnl_usd: float = 0.0
    exit_reason: str = ""  # "tp", "sl", "expired"


@dataclass
class BacktestResult:
    """Results from a backtest run."""

    start_date: datetime
    end_date: datetime
    initial_capital: float
    final_capital: float
    total_trades: int = 0
    winning_trades: int = 0
    losing_trades: int = 0
    win_rate: float = 0.0
    total_pnl_pct: float = 0.0
    max_drawdown_pct: float = 0.0
    trades: list[BacktestTrade] = field(default_factory=list)
    monthly_snapshots: dict = field(default_factory=dict)
    equity_curve: list[tuple[datetime, float]] = field(default_factory=list)

    def summary(self) -> str:
        """Generate a human-readable summary."""
        return (
            f"\n{'='*60}\n"
            f"BACKTEST RESULTS: {self.start_date.date()} to {self.end_date.date()}\n"
            f"{'='*60}\n"
            f"Initial Capital:  ${self.initial_capital:,.2f}\n"
            f"Final Capital:    ${self.final_capital:,.2f}\n"
            f"Total PNL:        {self.total_pnl_pct:+.2f}%\n"
            f"Max Drawdown:     {self.max_drawdown_pct:.2f}%\n"
            f"{'─'*60}\n"
            f"Total Trades:     {self.total_trades}\n"
            f"Winning Trades:   {self.winning_trades}\n"
            f"Losing Trades:    {self.losing_trades}\n"
            f"Win Rate:         {self.win_rate:.1f}%\n"
            f"{'─'*60}\n"
            f"Monthly Baskets:\n"
            + "\n".join(
                f"  {month}: {', '.join(coins)}"
                for month, coins in sorted(self.monthly_snapshots.items())
            )
            + f"\n{'='*60}\n"
        )


class Backtester:
    """Historical backtesting engine using Binance kline data."""

    def __init__(self):
        pass

    def fetch_daily_klines(
        self, symbol: str, start: datetime, end: datetime
    ) -> list[dict]:
        """Fetch daily OHLCV data from Binance USDT-M futures.

        Args:
            symbol: Coin symbol (e.g., "BTC").
            start: Start date.
            end: End date.

        Returns:
            List of daily candle dicts with open, high, low, close, volume.
        """
        pair = f"{symbol}USDT"
        start_ms = int(start.timestamp() * 1000)
        end_ms = int(end.timestamp() * 1000)

        try:
            resp = requests.get(
                "https://fapi.binance.com/fapi/v1/klines",
                params={
                    "symbol": pair,
                    "interval": "1d",
                    "startTime": start_ms,
                    "endTime": end_ms,
                    "limit": 1500,
                },
                timeout=20,
            )
            resp.raise_for_status()
            klines = resp.json()
        except (requests.RequestException, ValueError) as e:
            logger.warning("Failed to fetch klines for %s: %s", pair, e)
            return []

        candles = []
        for k in klines:
            candles.append({
                "date": datetime.fromtimestamp(k[0] / 1000, tz=timezone.utc),
                "open": float(k[1]),
                "high": float(k[2]),
                "low": float(k[3]),
                "close": float(k[4]),
                "volume": float(k[5]),
            })

        return candles

    def _simulate_month(
        self,
        basket: list[dict],
        candle_data: dict[str, list[dict]],
        month_start: datetime,
        month_end: datetime,
        capital: float,
    ) -> tuple[list[BacktestTrade], float]:
        """Simulate one month of the strategy.

        Args:
            basket: Fixed list of coin dicts for this month.
            candle_data: Dict mapping symbol -> list of daily candles.
            month_start: First day of the month.
            month_end: Last day of the month.
            capital: Starting capital for this month.

        Returns:
            Tuple of (trades, ending capital).
        """
        trades: list[BacktestTrade] = []
        cash = capital
        open_positions: list[BacktestTrade] = []

        symbols = [c["symbol"] for c in basket]

        # Get the date range for this month
        all_dates = set()
        for sym in symbols:
            if sym in candle_data:
                for candle in candle_data[sym]:
                    if month_start <= candle["date"] <= month_end:
                        all_dates.add(candle["date"].date())
        all_dates = sorted(all_dates)

        for day in all_dates:
            # Check open positions for TP/SL/expiry
            still_open = []
            for pos in open_positions:
                sym = pos.symbol
                candle = self._get_candle_for_date(candle_data.get(sym, []), day)
                if candle is None:
                    still_open.append(pos)
                    continue

                days_held = (datetime.combine(day, datetime.min.time()).replace(tzinfo=timezone.utc) - pos.entry_date).days

                # Check if high hit TP
                if candle["high"] >= pos.tp_price:
                    cash += self._close_trade(pos, pos.tp_price, candle["date"], "tp")
                    trades.append(pos)
                    continue

                # Check if low hit SL
                if candle["low"] <= pos.sl_price:
                    cash += self._close_trade(pos, pos.sl_price, candle["date"], "sl")
                    trades.append(pos)
                    continue

                # Check expiry
                if days_held >= config.MAX_HOLD_DAYS:
                    cash += self._close_trade(pos, candle["close"], candle["date"], "expired")
                    trades.append(pos)
                    continue

                still_open.append(pos)

            open_positions = still_open

            # Check for new dip entries
            for sym in symbols:
                # Skip if already have open position for this coin
                if any(p.symbol == sym for p in open_positions):
                    continue

                candle = self._get_candle_for_date(candle_data.get(sym, []), day)
                if candle is None:
                    continue

                # Calculate daily change (open to close or prev close to close)
                prev_candle = self._get_prev_candle(candle_data.get(sym, []), day)
                if prev_candle is None:
                    continue

                change_pct = ((candle["close"] - prev_candle["close"]) / prev_candle["close"]) * 100

                if change_pct <= -(config.DIP_THRESHOLD_PCT * 100):
                    # Calculate portfolio value for dynamic sizing
                    open_value = sum(
                        self._position_equity(
                            p,
                            self._get_price_for_date(candle_data.get(p.symbol, []), day, p.entry_price),
                        )
                        for p in open_positions
                    )
                    portfolio_value = cash + open_value
                    margin = portfolio_value * config.PER_TRADE_PCT

                    max_margin = cash / (1 + config.LEVERAGE * config.FUTURES_FEE_PCT)
                    if margin > max_margin:
                        margin = max_margin
                    if margin < 10:
                        continue

                    entry_price = candle["close"]
                    leverage = min(config.LEVERAGE, config.MAX_LEVERAGE)
                    notional = margin * leverage
                    entry_fee = notional * config.FUTURES_FEE_PCT
                    quantity = notional / entry_price

                    fee_drag = 2 * config.FUTURES_FEE_PCT * leverage
                    funding_drag = config.FUNDING_RATE_DAILY * config.MAX_HOLD_DAYS * leverage
                    gross_tp_move = (config.FUTURES_NET_TP_PCT + fee_drag + funding_drag) / leverage
                    gross_sl_move = max(
                        (config.FUTURES_NET_SL_PCT - fee_drag - funding_drag) / leverage,
                        0.001,
                    )
                    tp_price = entry_price * (1 + gross_tp_move)
                    sl_price = entry_price * (1 - gross_sl_move) if config.FUTURES_USE_SL else 0.0

                    cash -= margin + entry_fee

                    pos = BacktestTrade(
                        symbol=sym,
                        entry_date=candle["date"],
                        entry_price=entry_price,
                        tp_price=tp_price,
                        sl_price=sl_price,
                        quantity=quantity,
                        margin_usd=margin,
                        entry_fee_usd=entry_fee,
                        leverage=leverage,
                    )
                    open_positions.append(pos)

        # Close any remaining open positions at month end
        for pos in open_positions:
            last_day = all_dates[-1] if all_dates else month_end.date()
            candle = self._get_candle_for_date(candle_data.get(pos.symbol, []), last_day)
            exit_price = candle["close"] if candle else pos.entry_price
            exit_date = datetime.combine(last_day, datetime.min.time()).replace(tzinfo=timezone.utc)
            cash += self._close_trade(pos, exit_price, exit_date, "month_end")
            trades.append(pos)

        return trades, cash

    def _close_trade(
        self,
        pos: "BacktestTrade",
        exit_price: float,
        exit_date: datetime,
        reason: str,
    ) -> float:
        """Close a backtest futures trade. Returns released margin plus net PNL."""
        pos.exit_date = exit_date
        pos.exit_price = exit_price
        pos.exit_reason = reason

        exit_notional = exit_price * pos.quantity
        exit_fee = exit_notional * config.FUTURES_FEE_PCT
        gross_pnl = (exit_price - pos.entry_price) * pos.quantity
        pos.pnl_usd = gross_pnl - pos.entry_fee_usd - exit_fee
        pos.pnl_pct = (pos.pnl_usd / pos.margin_usd) * 100 if pos.margin_usd else 0.0

        return pos.margin_usd + gross_pnl - exit_fee

    def _position_equity(self, pos: "BacktestTrade", current_price: float) -> float:
        """Estimate open futures position equity at a given mark price."""
        exit_notional = current_price * pos.quantity
        exit_fee = exit_notional * config.FUTURES_FEE_PCT
        gross_pnl = (current_price - pos.entry_price) * pos.quantity
        return pos.margin_usd + gross_pnl - exit_fee

    def _get_candle_for_date(self, candles: list[dict], day: dt.date) -> dict | None:
        """Find candle matching a specific date."""
        for c in candles:
            if c["date"].date() == day:
                return c
        return None

    def _get_prev_candle(self, candles: list[dict], day: dt.date) -> dict | None:
        """Find the candle before a specific date."""
        prev = None
        for c in candles:
            if c["date"].date() >= day:
                return prev
            prev = c
        return prev

    def _get_price_for_date(self, candles: list[dict], day: dt.date, fallback: float) -> float:
        """Get closing price for a date, with fallback."""
        candle = self._get_candle_for_date(candles, day)
        return candle["close"] if candle else fallback

    def run(
        self,
        start_date: datetime,
        end_date: datetime,
        initial_capital: float | None = None,
        basket_override: dict[str, list[dict]] | None = None,
    ) -> BacktestResult:
        """Run a full backtest over the given date range.

        Args:
            start_date: Backtest start (e.g., 2025-04-01).
            end_date: Backtest end (e.g., 2026-03-31).
            initial_capital: Starting capital. Defaults to config.
            basket_override: Optional dict mapping "YYYY-MM" -> list of coin dicts
                to use as the monthly basket (for reproducible backtests).

        Returns:
            BacktestResult with full trade history and statistics.
        """
        initial_capital = initial_capital or config.CAPITAL_USD
        capital = initial_capital
        all_trades: list[BacktestTrade] = []
        monthly_snapshots: dict[str, list[str]] = {}
        equity_curve: list[tuple[datetime, float]] = [(start_date, capital)]
        peak_capital = capital
        max_drawdown = 0.0

        # Iterate month by month
        current = start_date.replace(day=1)
        while current <= end_date:
            month_key = current.strftime("%Y-%m")
            month_end = (current.replace(day=28) + timedelta(days=4)).replace(day=1) - timedelta(days=1)
            month_end = min(month_end.replace(tzinfo=timezone.utc), end_date)
            month_start = current.replace(tzinfo=timezone.utc) if current.tzinfo is None else current

            logger.info("Backtesting %s | Capital: $%.2f", month_key, capital)

            # Get basket for this month
            if basket_override and month_key in basket_override:
                basket = basket_override[month_key]
            else:
                # For backtesting without historical snapshots, use a default top-coin list.
                logger.info(
                    "No basket override for %s — using default top coins. "
                    "For accurate backtests, provide historical snapshots.",
                    month_key,
                )
                basket = self._get_default_basket()

            symbols = [c["symbol"] for c in basket]
            monthly_snapshots[month_key] = symbols

            # Fetch historical data for all basket coins
            candle_data = {}
            # Fetch a few extra days before month start for prev-close calculation
            fetch_start = month_start - timedelta(days=5)
            for sym in symbols:
                candles = self.fetch_daily_klines(sym, fetch_start, month_end)
                if candles:
                    candle_data[sym] = candles
                    logger.debug("Fetched %d candles for %s", len(candles), sym)
                else:
                    logger.warning("No data for %s in %s", sym, month_key)

            # Simulate the month
            month_trades, capital = self._simulate_month(
                basket, candle_data, month_start, month_end, capital
            )
            all_trades.extend(month_trades)

            # Track equity and drawdown
            equity_curve.append((month_end, capital))
            if capital > peak_capital:
                peak_capital = capital
            drawdown = ((peak_capital - capital) / peak_capital) * 100
            if drawdown > max_drawdown:
                max_drawdown = drawdown

            # Move to next month
            current = (month_end + timedelta(days=1)).replace(day=1)

        # Compile results
        winning = [t for t in all_trades if t.pnl_pct > 0]
        result = BacktestResult(
            start_date=start_date,
            end_date=end_date,
            initial_capital=initial_capital,
            final_capital=capital,
            total_trades=len(all_trades),
            winning_trades=len(winning),
            losing_trades=len(all_trades) - len(winning),
            win_rate=(len(winning) / len(all_trades) * 100) if all_trades else 0,
            total_pnl_pct=((capital - initial_capital) / initial_capital) * 100,
            max_drawdown_pct=max_drawdown,
            trades=all_trades,
            monthly_snapshots=monthly_snapshots,
            equity_curve=equity_curve,
        )

        logger.info(result.summary())
        return result

    def _get_default_basket(self) -> list[dict]:
        """Default basket of well-known top-50 coins for backtesting.

        Used when no historical CMC snapshot is available.
        """
        default_symbols = [
            "BTC", "ETH", "BNB", "XRP", "ADA",
            "DOGE", "SOL", "DOT", "LINK", "LTC",
        ]
        return [{"symbol": s, "name": s} for s in default_symbols]
