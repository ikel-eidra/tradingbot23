"""Strategy engine for the Top 10 Losers mean-reversion bot.

Orchestrates the monthly snapshot, daily dip detection, and trade
signal generation.
"""

import logging
from datetime import datetime, timezone

from bot import config
from bot.modules.data_fetcher import DataFetcher
from bot.modules.futures_trader import FuturesTrader
from bot.modules.trader import Trader

logger = logging.getLogger(__name__)


class Strategy:
    """Top 10 Losers mean-reversion strategy engine."""

    def __init__(
        self,
        fetcher: DataFetcher | None = None,
        trader: Trader | FuturesTrader | None = None,
    ):
        self.fetcher = fetcher or DataFetcher()
        self.trader = trader or Trader()
        self.is_futures = isinstance(self.trader, FuturesTrader)
        self.basket: list[dict] = []  # Current month's fixed coin basket
        self.basket_month: int | None = None  # Month the basket was set
        self.basket_year: int | None = None

    def should_refresh_basket(self, now: datetime | None = None) -> bool:
        """Check if we need a new monthly snapshot."""
        now = now or datetime.now(timezone.utc)
        if not self.basket:
            return True
        if self.basket_year != now.year or self.basket_month != now.month:
            return True
        return False

    def refresh_basket(self, now: datetime | None = None) -> list[dict]:
        """Take a new monthly snapshot of top losers.

        Fetches top 50 coins by market cap, identifies the top 10 losers,
        and locks the basket for the month.
        """
        now = now or datetime.now(timezone.utc)

        logger.info("Taking monthly snapshot for %s %d", now.strftime("%B"), now.year)
        coins = self.fetcher.get_top_coins()
        losers = self.fetcher.get_top_losers(coins)

        if not losers:
            logger.warning("No losers found — market may be fully green. Keeping previous basket.")
            return self.basket

        self.basket = losers
        self.basket_month = now.month
        self.basket_year = now.year

        # Save snapshot to disk
        self.fetcher.save_snapshot(losers, now)

        logger.info(
            "Monthly basket set: %s",
            [f"{c['symbol']} ({c['percent_change_24h']:+.2f}%)" for c in self.basket],
        )
        return self.basket

    def load_basket(self, year: int, month: int) -> list[dict] | None:
        """Load a previously saved basket from disk."""
        coins = self.fetcher.load_snapshot(year, month)
        if coins:
            self.basket = coins
            self.basket_month = month
            self.basket_year = year
            logger.info("Loaded basket for %d-%02d: %s", year, month, [c["symbol"] for c in coins])
        return coins

    def detect_dips(self) -> list[dict]:
        """Check current prices of basket coins and detect dips.

        A dip is when the 24h change is <= -DIP_THRESHOLD_PCT.
        CMC data is fetched once per cycle (not per coin) to avoid
        excessive API calls and rate limiting.

        Returns:
            List of coin dicts that are currently dipping.
        """
        if not self.basket:
            logger.warning("No basket set — cannot detect dips")
            return []

        if self.is_futures:
            return self._detect_dips_futures()

        # Fetch fresh market data once for all coins (not inside the loop)
        try:
            fresh_coins = self.fetcher.get_top_coins()
        except Exception:
            logger.exception("Failed to fetch fresh market data for dip detection")
            return []
        fresh_data = {c["symbol"]: c for c in fresh_coins}

        # DIP_THRESHOLD_PCT is stored as decimal (0.02 = 2%).
        # CMC returns percent_change_24h as percentage (-3.5 means -3.5%).
        threshold = -(config.DIP_THRESHOLD_PCT * 100)

        basket_symbols = {c["symbol"] for c in self.basket}
        dipping = []
        for coin in self.basket:
            symbol = coin["symbol"]
            if symbol not in fresh_data:
                logger.debug("No fresh data for %s, skipping", symbol)
                continue

            change_24h = fresh_data[symbol]["percent_change_24h"]
            current_price = fresh_data[symbol]["price"]

            if change_24h <= threshold:
                coin_with_price = {**coin, "current_price": current_price, "change_24h": change_24h}
                dipping.append(coin_with_price)
                logger.info(
                    "DIP detected: %s at $%.4f (24h: %+.2f%%)",
                    symbol, current_price, change_24h,
                )

        return dipping

    def _detect_dips_futures(self) -> list[dict]:
        """Detect dips for futures using true 5-minute kline change."""
        threshold = -(config.FUTURES_DIP_THRESHOLD_PCT * 100)
        dipping = []
        for coin in self.basket:
            symbol = coin["symbol"]
            change_5m = self.trader.get_5m_change(symbol)
            if change_5m is None:
                continue
            if change_5m <= threshold:
                price = self.trader.get_current_price(symbol)
                if price is None:
                    continue
                dipping.append({**coin, "current_price": price, "change_5m": change_5m})
                logger.info(
                    "5m DIP detected: %s at $%.4f (5m: %+.3f%%)",
                    symbol, price, change_5m,
                )
        return dipping

    def detect_dips_from_prices(self, price_data: dict[str, dict]) -> list[dict]:
        """Detect dips using pre-fetched price data (for backtesting).

        Args:
            price_data: Dict mapping symbol -> {"price": float, "change_pct": float}

        Returns:
            List of basket coins that are dipping.
        """
        if not self.basket:
            return []

        dipping = []
        for coin in self.basket:
            symbol = coin["symbol"]
            if symbol not in price_data:
                continue

            data = price_data[symbol]
            change_pct = data.get("change_pct", 0)

            if change_pct <= -(config.DIP_THRESHOLD_PCT * 100):
                coin_with_price = {
                    **coin,
                    "current_price": data["price"],
                    "change_24h": change_pct,
                }
                dipping.append(coin_with_price)

        return dipping

    def execute_signals(self, dipping_coins: list[dict]) -> list:
        """Execute buy orders for dipping coins.

        Args:
            dipping_coins: Coins that passed the dip threshold.

        Returns:
            List of opened positions.
        """
        opened = []
        for coin in dipping_coins:
            symbol = coin["symbol"]

            # Skip if already have an open position
            open_positions = self.trader.get_open_positions()
            if any(p.symbol == symbol for p in open_positions):
                logger.debug("Skipping %s — already have open position", symbol)
                continue

            position = self.trader.open_position(symbol)
            if position:
                opened.append(position)

        return opened

    def run_cycle(self) -> dict:
        """Run one full strategy cycle (snapshot check + dip scan + trade).

        Returns:
            Summary dict of actions taken.
        """
        now = datetime.now(timezone.utc)
        summary = {
            "timestamp": now.isoformat(),
            "basket_refreshed": False,
            "dips_found": 0,
            "positions_opened": 0,
            "positions_closed": 0,
        }

        # Step 1: Check if we need a new basket
        if self.should_refresh_basket(now):
            self.refresh_basket(now)
            summary["basket_refreshed"] = True

        # Step 2: Check existing positions (TP/SL/expiry)
        closed = self.trader.check_positions()
        summary["positions_closed"] = len(closed)

        # Step 3: Detect dips
        dipping = self.detect_dips()
        summary["dips_found"] = len(dipping)

        # Step 4: Execute signals
        if dipping:
            opened = self.execute_signals(dipping)
            summary["positions_opened"] = len(opened)

        # Log summary
        stats = self.trader.get_stats()
        portfolio = self.trader.get_portfolio_value()
        logger.info(
            "Cycle complete | Portfolio: $%.2f | Open: %d | Closed today: %d | "
            "Dips: %d | New trades: %d | Total trades: %d | Win rate: %.1f%%",
            portfolio,
            len(self.trader.get_open_positions()),
            summary["positions_closed"],
            summary["dips_found"],
            summary["positions_opened"],
            stats.get("total_trades", 0),
            stats.get("win_rate", 0),
        )

        return summary

    def get_status(self) -> dict:
        """Get current strategy status."""
        stats = self.trader.get_stats()
        return {
            "mode": config.TRADING_MODE,
            "basket": [c["symbol"] for c in self.basket] if self.basket else [],
            "basket_month": f"{self.basket_year}-{self.basket_month:02d}" if self.basket_month else None,
            "portfolio_value": self.trader.get_portfolio_value(),
            "cash_balance": self.trader.cash_balance,
            "open_positions": len(self.trader.get_open_positions()),
            "stats": stats,
        }
