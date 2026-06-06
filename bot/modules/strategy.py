"""Strategy engine for the top losers mean-reversion bot.

Orchestrates the monthly snapshot, daily dip detection, and trade
signal generation.
"""

import logging
from datetime import datetime, timedelta, timezone

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
        self.last_pre_trade_decisions: list[dict] = []
        self._paper_guard_reason_cache: dict[str, str | None] = {}

    def _rank(self, coin: dict) -> int | None:
        """Return the market-cap rank we use for the top-N guard."""
        rank = coin.get("cmc_rank") or coin.get("market_cap_rank")
        try:
            return int(rank)
        except (TypeError, ValueError):
            return None

    def _is_top_ranked_coin(self, coin: dict) -> bool:
        rank = self._rank(coin)
        return rank is not None and 1 <= rank <= config.TOP_N_COINS

    def _is_tradeable_symbol(self, symbol: str) -> bool:
        return not config.is_futures_excluded_symbol(symbol)

    def _filter_top_ranked(self, coins: list[dict], context: str) -> list[dict]:
        eligible = [
            c for c in coins
            if self._is_top_ranked_coin(c) and self._is_tradeable_symbol(c.get("symbol", ""))
        ]
        skipped = [
            c.get("symbol", "?") for c in coins
            if not self._is_top_ranked_coin(c)
            or not self._is_tradeable_symbol(c.get("symbol", ""))
        ]
        if skipped:
            logger.warning(
                "%s skipped outside top %d or excluded futures symbols: %s",
                context,
                config.TOP_N_COINS,
                skipped,
            )
        return eligible

    @staticmethod
    def _position_status_value(pos) -> str:
        status = getattr(pos, "status", "")
        return getattr(status, "value", status) or ""

    def _paper_guard_reason(self, symbol: str, now: datetime | None = None) -> str | None:
        symbol = symbol.upper()
        if not config.PAPER_SYMBOL_GUARD_ENABLED:
            return None
        if symbol in self._paper_guard_reason_cache:
            return self._paper_guard_reason_cache[symbol]

        history_getter = getattr(self.trader, "get_trade_history", None)
        if history_getter is None:
            self._paper_guard_reason_cache[symbol] = None
            return None

        now = now or datetime.now(timezone.utc)
        cutoff = now - timedelta(days=config.PAPER_SYMBOL_GUARD_LOOKBACK_DAYS)
        recent = []
        try:
            for pos in history_getter():
                if getattr(pos, "symbol", "").upper() != symbol:
                    continue
                exit_time = getattr(pos, "exit_time", None)
                if exit_time is not None and exit_time < cutoff:
                    continue
                recent.append(pos)
        except Exception:
            logger.debug("Paper symbol guard could not read trade history", exc_info=True)
            self._paper_guard_reason_cache[symbol] = None
            return None

        if not recent:
            self._paper_guard_reason_cache[symbol] = None
            return None

        total_pnl = sum(float(getattr(pos, "pnl_usd", 0.0) or 0.0) for pos in recent)
        if total_pnl <= -config.PAPER_SYMBOL_GUARD_MAX_REALIZED_LOSS_USD:
            reason = (
                f"paper guard: {symbol} realized {total_pnl:+.2f} USD over "
                f"{config.PAPER_SYMBOL_GUARD_LOOKBACK_DAYS:.0f}d"
            )
            self._paper_guard_reason_cache[symbol] = reason
            return reason

        if config.PAPER_SYMBOL_GUARD_BLOCK_LIQUIDATED:
            liquidated = [
                pos for pos in recent
                if self._position_status_value(pos) == "liquidated"
            ]
            if liquidated:
                reason = f"paper guard: {symbol} had recent liquidation"
                self._paper_guard_reason_cache[symbol] = reason
                return reason

        expired_losses = [
            pos for pos in recent
            if self._position_status_value(pos) == "expired"
            and float(getattr(pos, "pnl_usd", 0.0) or 0.0) <= -config.PAPER_SYMBOL_GUARD_EXPIRED_LOSS_USD
        ]
        if expired_losses:
            worst = min(float(getattr(pos, "pnl_usd", 0.0) or 0.0) for pos in expired_losses)
            reason = f"paper guard: {symbol} expired loss {worst:+.2f} USD"
            self._paper_guard_reason_cache[symbol] = reason
            return reason

        self._paper_guard_reason_cache[symbol] = None
        return None

    def _paper_guard_allows_candidate(
        self,
        symbol: str,
        source: str,
        *,
        record: bool = True,
    ) -> bool:
        reason = self._paper_guard_reason(symbol)
        if reason is None:
            return True

        logger.info("[PAPER-GUARD] WAIT %s | %s", symbol, reason)
        if record:
            self.last_pre_trade_decisions.append({
                "symbol": symbol,
                "source": source,
                "decision": "WAIT",
                "score": 0,
                "reason": reason,
            })
        return False

    def _filter_paper_guarded(self, coins: list[dict], context: str) -> list[dict]:
        kept = []
        blocked = []
        for coin in coins:
            symbol = coin.get("symbol", "")
            if self._paper_guard_allows_candidate(symbol, context, record=False):
                kept.append(coin)
            else:
                blocked.append(symbol)
        if blocked:
            logger.warning("%s paper guard skipped: %s", context, blocked)
        return kept

    @staticmethod
    def _analysis_allowed(analysis) -> bool:
        if analysis is None:
            return True
        if isinstance(analysis, dict):
            return bool(analysis.get("allowed", analysis.get("decision") == "RUN"))
        return bool(getattr(analysis, "allowed", False))

    @staticmethod
    def _analysis_dict(symbol: str, source: str, analysis) -> dict:
        if isinstance(analysis, dict):
            row = dict(analysis)
        elif hasattr(analysis, "as_dict"):
            row = analysis.as_dict()
        else:
            row = {
                "symbol": symbol,
                "decision": getattr(analysis, "decision", "RUN"),
                "score": getattr(analysis, "score", 100),
                "reason": getattr(analysis, "reason", "pre-trade analysis unavailable"),
            }
        row.setdefault("symbol", symbol)
        row.setdefault("decision", "RUN" if row.get("allowed", True) else "WAIT")
        row.setdefault("score", 100)
        row.setdefault("reason", "pre-trade analysis unavailable")
        row["source"] = source
        return row

    def _pre_trade_allows_entry(self, coin: dict, source: str) -> bool:
        """Run optional wave/volatility analysis before opening a futures trade."""
        if not config.PRE_TRADE_ANALYSIS_ENABLED:
            return True
        analyzer = getattr(self.trader, "pre_trade_analysis", None)
        if analyzer is None:
            return True

        symbol = coin["symbol"]
        try:
            analysis = analyzer(symbol)
        except Exception:
            logger.exception("Pre-trade analysis failed for %s", symbol)
            self.last_pre_trade_decisions.append({
                "symbol": symbol,
                "source": source,
                "decision": "WAIT",
                "score": 0,
                "reason": "pre-trade analysis error",
            })
            return False

        row = self._analysis_dict(symbol, source, analysis)
        self.last_pre_trade_decisions.append(row)
        if not self._analysis_allowed(analysis):
            logger.info(
                "[PRE-TRADE] WAIT %s | score %.0f | %s",
                symbol,
                float(row.get("score", 0) or 0),
                row.get("reason", ""),
            )
            return False

        logger.info(
            "[PRE-TRADE] RUN %s | score %.0f | %s",
            symbol,
            float(row.get("score", 0) or 0),
            row.get("reason", ""),
        )
        return True

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
        coins = self._filter_top_ranked(self.fetcher.get_top_coins(), "Monthly universe")
        expanded_losers = self.fetcher.get_top_losers(
            coins,
            n_losers=min(len(coins), max(config.TOP_N_LOSERS * 3, config.TOP_N_LOSERS + 5)),
        )
        losers = self._filter_paper_guarded(expanded_losers, "Monthly basket")[:config.TOP_N_LOSERS]

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
            coins = self._filter_top_ranked(coins, "Loaded basket")
            if not coins:
                logger.warning("Loaded basket had no coins inside configured top %d", config.TOP_N_COINS)
                return None
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
            if not self._is_tradeable_symbol(symbol):
                logger.debug("Skipping %s — excluded from futures universe", symbol)
                continue
            fresh_coin = fresh_data.get(symbol)
            if not self._is_top_ranked_coin(coin):
                logger.debug("Skipping %s — basket rank is outside top %d", symbol, config.TOP_N_COINS)
                continue
            if not fresh_coin:
                logger.debug("No fresh data for %s, skipping", symbol)
                continue
            if not self._is_top_ranked_coin(fresh_coin):
                logger.debug("Skipping %s — fresh rank is outside top %d", symbol, config.TOP_N_COINS)
                continue
            if not self._paper_guard_allows_candidate(symbol, "dip_pool", record=False):
                continue

            change_24h    = fresh_coin["percent_change_24h"]
            current_price = fresh_coin["price"]

            if change_24h <= threshold:
                dipping.append({
                    **coin,
                    "cmc_rank": fresh_coin.get("cmc_rank", coin.get("cmc_rank")),
                    "current_price": current_price,
                    "change_24h": change_24h,
                })
                logger.info("DIP detected: %s at $%.4f (24h: %+.2f%%)",
                            symbol, current_price, change_24h)

        return dipping

    def _detect_dips_futures(self) -> list[dict]:
        """Detect dips for futures using true 5-minute kline change."""
        threshold = -(config.FUTURES_DIP_THRESHOLD_PCT * 100)
        dipping = []
        for coin in self.basket:
            symbol = coin["symbol"]
            if not self._is_tradeable_symbol(symbol):
                logger.debug("Skipping %s futures dip — excluded from futures universe", symbol)
                continue
            if not self._paper_guard_allows_candidate(symbol, "futures_dip_pool", record=False):
                continue
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
            if not self._is_tradeable_symbol(symbol):
                continue
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
            if not self._is_tradeable_symbol(symbol):
                logger.warning("Skipping %s — excluded from futures universe", symbol)
                continue
            if not self._is_top_ranked_coin(coin):
                logger.warning(
                    "Skipping %s — rank %s is outside configured top %d",
                    symbol,
                    self._rank(coin),
                    config.TOP_N_COINS,
                )
                continue

            open_positions = self.trader.get_open_positions()
            if len(open_positions) >= config.MAX_OPEN_TRADES:
                logger.debug("Skipping %s — max open position slots reached", symbol)
                break

            # Skip if already have an open position
            if any(p.symbol == symbol for p in open_positions):
                logger.debug("Skipping %s — already have open position", symbol)
                continue

            if not self._paper_guard_allows_candidate(symbol, "dip"):
                continue

            if not self._pre_trade_allows_entry(coin, "dip"):
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
        slots          = config.MAX_OPEN_TRADES - len(open_positions)
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
            fresh_coin = fresh_data.get(sym)
            if sym in open_symbols:
                continue
            if not self._is_tradeable_symbol(sym):
                logger.debug("Skipping %s fill — excluded from futures universe", sym)
                continue
            if not self._is_top_ranked_coin(coin):
                logger.debug("Skipping %s fill — basket rank is outside top %d", sym, config.TOP_N_COINS)
                continue
            if not fresh_coin:
                continue
            if not self._is_top_ranked_coin(fresh_coin):
                logger.debug("Skipping %s fill — fresh rank is outside top %d", sym, config.TOP_N_COINS)
                continue
            if not self._paper_guard_allows_candidate(sym, "fill_pool", record=False):
                continue
            candidates.append({
                **coin,
                "cmc_rank": fresh_coin.get("cmc_rank", coin.get("cmc_rank")),
                "current_price": fresh_coin["price"],
                "change_24h":    fresh_coin["percent_change_24h"],
            })

        candidates.sort(key=lambda c: c["change_24h"])

        opened = []
        for coin in candidates:
            if len(opened) >= slots:
                break
            if not self._paper_guard_allows_candidate(coin["symbol"], "fill"):
                continue
            if not self._pre_trade_allows_entry(coin, "fill"):
                continue
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
        if not config.CRASH_ENTRY_GUARD_ENABLED:
            if self._crash_mode:
                logger.info("[CRASH-MODE] LIFTED — entry guard disabled in settings")
            self._crash_mode = False
            return

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
                protected = (
                    self.trader.arm_crash_sl()
                    if config.CRASH_EMERGENCY_SL_ENABLED
                    else 0
                )
                tg.alert_crash(
                    btc_change * 100,
                    protected,
                    emergency_sl_enabled=config.CRASH_EMERGENCY_SL_ENABLED,
                )

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
            "pre_trade_checked": 0,
            "pre_trade_wait": 0,
            "pre_trade_decisions": [],
        }
        self.last_pre_trade_decisions = []
        self._paper_guard_reason_cache = {}

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

        summary["pre_trade_decisions"] = list(self.last_pre_trade_decisions)
        summary["pre_trade_checked"] = len(self.last_pre_trade_decisions)
        summary["pre_trade_wait"] = len([
            d for d in self.last_pre_trade_decisions
            if d.get("decision") != "RUN"
        ])

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
