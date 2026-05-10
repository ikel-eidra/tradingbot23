"""Strategy engine for the top losers mean-reversion bot.

Orchestrates the monthly snapshot, daily dip detection, and trade
signal generation.
"""

import logging
from datetime import datetime, timezone

from bot import config
from bot.modules.data_fetcher import DataFetcher
from bot.modules.futures_trader import FuturesTrader
from bot.modules import telegram_notifier as tg

logger = logging.getLogger(__name__)


class Strategy:
    """Top losers mean-reversion strategy engine."""

    def __init__(
        self,
        fetcher: DataFetcher | None = None,
        trader: FuturesTrader | None = None,
    ):
        self.fetcher = fetcher or DataFetcher()
        self.trader = trader or FuturesTrader()
        self.is_futures = True
        self.basket: list[dict] = []  # Current month's fixed coin basket
        self.basket_month: int | None = None  # Month the basket was set
        self.basket_year: int | None = None
        self._crash_mode: bool = False

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

        Fetches top coins by market cap, identifies the configured top losers,
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
        """Check current 24h change for basket coins using CoinGecko data.

        Futures entries use the 24h CoinGecko move for basket signals.
        """
        if not self.basket:
            logger.warning("No basket set — cannot detect dips")
            return []

        try:
            fresh_coins = self.fetcher.get_top_coins()
        except Exception:
            logger.exception("Failed to fetch fresh market data for dip detection")
            return []

        fresh_data = {c["symbol"]: c for c in fresh_coins}
        threshold  = -(config.DIP_THRESHOLD_PCT * 100)

        dipping = []
        for coin in self.basket:
            symbol = coin["symbol"]
            if symbol not in fresh_data:
                logger.debug("No fresh data for %s, skipping", symbol)
                continue

            change_24h    = fresh_data[symbol]["percent_change_24h"]
            current_price = fresh_data[symbol]["price"]

            if change_24h <= threshold:
                dipping.append({**coin, "current_price": current_price, "change_24h": change_24h})
                logger.info("DIP detected: %s at $%.4f (24h: %+.2f%%)",
                            symbol, current_price, change_24h)

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

            cg_price   = coin.get("current_price")
            change_24h = coin.get("change_24h", 0.0)
            position = self.trader.open_position(
                symbol, entry_price=cg_price, entry_change_24h=change_24h,
            )
            if position:
                opened.append(position)

        return opened

    def fill_empty_slots(self) -> list:
        """Fill any open position slots with basket coins (no dip threshold required).

        Ensures capital is always fully deployed. Picks the worst 24h performers
        from the basket that aren't already held, sorted worst-first.
        """
        if not self.basket:
            return []

        open_positions = self.trader.get_open_positions()
        open_symbols   = {p.symbol for p in open_positions}
        slots          = config.TOP_N_LOSERS - len(open_positions)
        if slots <= 0 or self.trader.cash_balance < 10:
            return []

        try:
            fresh_coins = self.fetcher.get_top_coins()
        except Exception:
            logger.exception("Failed to fetch market data for fill_empty_slots")
            return []

        fresh_data = {c["symbol"]: c for c in fresh_coins}

        candidates = []
        for coin in self.basket:
            sym = coin["symbol"]
            if sym in open_symbols or sym not in fresh_data:
                continue
            candidates.append({
                **coin,
                "current_price": fresh_data[sym]["price"],
                "change_24h":    fresh_data[sym]["percent_change_24h"],
            })

        candidates.sort(key=lambda c: c["change_24h"])

        opened = []
        for coin in candidates[:slots]:
            position = self.trader.open_position(
                coin["symbol"],
                entry_price=coin["current_price"],
                entry_change_24h=coin["change_24h"],
            )
            if position:
                logger.info("[FILL] Opened %s (24h: %+.2f%%) to fill empty slot",
                            coin["symbol"], coin["change_24h"])
                opened.append(position)
        return opened

    def _update_crash_mode(self) -> None:
        """Check BTC 24h change and toggle crash mode accordingly."""
        try:
            fresh_coins = self.fetcher.get_top_coins()
        except Exception:
            logger.debug("Could not fetch coins for crash check")
            return

        btc_data = next((c for c in fresh_coins if c["symbol"] == "BTC"), None)
        if btc_data is None:
            return

        btc_change = btc_data["percent_change_24h"] / 100  # convert % to decimal

        was_crash = self._crash_mode

        if not self._crash_mode and btc_change < config.CRASH_BTC_TRIGGER_PCT:
            self._crash_mode = True
            logger.warning(
                "[CRASH-MODE] ACTIVATED — BTC 24h: %.2f%% (threshold: %.2f%%)",
                btc_change * 100, config.CRASH_BTC_TRIGGER_PCT * 100,
            )
            if self.is_futures:
                protected = self.trader.arm_crash_sl()
                tg.alert_crash(btc_change * 100, protected)

        elif self._crash_mode and btc_change > config.CRASH_BTC_RECOVERY_PCT:
            self._crash_mode = False
            logger.info(
                "[CRASH-MODE] LIFTED — BTC 24h recovered to %.2f%% (threshold: %.2f%%)",
                btc_change * 100, config.CRASH_BTC_RECOVERY_PCT * 100,
            )
            tg.alert_crash_recovery(btc_change * 100)

    def run_cycle(self) -> dict:
        """Run one full strategy cycle (snapshot check + dip scan + fill + trade).

        Returns:
            Summary dict of actions taken.
        """
        now = datetime.now(timezone.utc)
        summary = {
            "timestamp": now.isoformat(),
            "basket_refreshed": False,
            "cash_contributed": 0.0,
            "dips_found": 0,
            "positions_opened": 0,
            "positions_closed": 0,
            "slots_filled": 0,
        }

        if hasattr(self.trader, "apply_monthly_contribution"):
            contributed = self.trader.apply_monthly_contribution(now)
            summary["cash_contributed"] = contributed
            if contributed:
                logger.info(
                    "Monthly paper contribution applied: $%.2f | Cash: $%.2f",
                    contributed, self.trader.cash_balance,
                )
                tg.alert_contribution(contributed, self.trader.cash_balance, now.strftime("%Y-%m"))

        # Step 1: Check if we need a new basket
        if self.should_refresh_basket(now):
            self.refresh_basket(now)
            summary["basket_refreshed"] = True

        # Step 2: Check existing positions (TP/SL/expiry)
        closed = self.trader.check_positions()
        summary["positions_closed"] = len(closed)

        # Step 2b: Crash detection — check BTC 24h change
        self._update_crash_mode()

        # Step 3 & 4: Skip entries entirely if crash mode is active
        if self._crash_mode:
            logger.warning("[CRASH-MODE] Entries blocked — BTC crash in progress")
        else:
            # Step 3: Detect dips and open on dip signals
            dipping = self.detect_dips()
            summary["dips_found"] = len(dipping)
            if dipping:
                opened = self.execute_signals(dipping)
                summary["positions_opened"] = len(opened)

            # Step 4: Fill any remaining empty slots (always invested)
            filled = self.fill_empty_slots()
            summary["slots_filled"] = len(filled)

        # Log summary
        stats = self.trader.get_stats()
        portfolio = self.trader.get_portfolio_value()
        logger.info(
            "Cycle complete | Portfolio: $%.2f | Open: %d | Closed: %d | "
            "Dips: %d | Opened: %d | Filled: %d | Total: %d | Win rate: %.1f%%",
            portfolio,
            len(self.trader.get_open_positions()),
            summary["positions_closed"],
            summary["dips_found"],
            summary["positions_opened"],
            summary["slots_filled"],
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
