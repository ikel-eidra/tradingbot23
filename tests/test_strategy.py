"""Tests for the strategy engine."""

import unittest
from datetime import datetime, timezone
from types import SimpleNamespace

from bot import config
from bot.modules.strategy import Strategy
from tests.support import isolate_data_dir


class TestStrategy(unittest.TestCase):
    """Tests for Strategy."""

    def setUp(self):
        isolate_data_dir(self)
        config.CAPITAL_USD = 10000
        config.PER_TRADE_PCT = 0.20
        config.LEVERAGE = 1
        config.FUTURES_FEE_PCT = 0.0006
        config.MONTHLY_CONTRIBUTION_USD = 0
        config.TOP_N_COINS = 50
        config.TOP_N_LOSERS = 5
        config.MAX_OPEN_TRADES = 5

    def test_execute_signals_respects_max_open_slots(self):
        """Should not open more positions than the configured basket slots."""

        class FakeTrader:
            def __init__(self):
                self.positions = [SimpleNamespace(symbol=f"HELD{i}") for i in range(4)]

            def get_open_positions(self):
                return list(self.positions)

            def open_position(self, symbol, entry_price=None, entry_change_24h=0.0):
                pos = SimpleNamespace(symbol=symbol)
                self.positions.append(pos)
                return pos

        trader = FakeTrader()
        strategy = Strategy(trader=trader)
        opened = strategy.execute_signals([
            {"symbol": "AAA", "cmc_rank": 10, "current_price": 1.0, "change_24h": -3.0},
            {"symbol": "BBB", "cmc_rank": 11, "current_price": 1.0, "change_24h": -4.0},
        ])

        self.assertEqual(len(opened), 1)
        self.assertEqual(len(trader.get_open_positions()), config.MAX_OPEN_TRADES)

    def test_execute_signals_respects_max_open_trades_separate_from_basket_size(self):
        """Open slots are controlled by MAX_OPEN_TRADES, not monthly basket size."""

        config.TOP_N_LOSERS = 10
        config.MAX_OPEN_TRADES = 1

        class FakeTrader:
            def __init__(self):
                self.positions = []

            def get_open_positions(self):
                return list(self.positions)

            def open_position(self, symbol, entry_price=None, entry_change_24h=0.0):
                pos = SimpleNamespace(symbol=symbol)
                self.positions.append(pos)
                return pos

        trader = FakeTrader()
        strategy = Strategy(trader=trader)
        opened = strategy.execute_signals([
            {"symbol": "AAA", "cmc_rank": 10, "current_price": 1.0, "change_24h": -3.0},
            {"symbol": "BBB", "cmc_rank": 11, "current_price": 1.0, "change_24h": -4.0},
        ])

        self.assertEqual([p.symbol for p in opened], ["AAA"])
        self.assertEqual(len(trader.get_open_positions()), 1)

    def test_execute_signals_skips_outside_top_50(self):
        """Signals without a valid top-50 rank should never open positions."""

        class FakeTrader:
            def __init__(self):
                self.positions = []

            def get_open_positions(self):
                return list(self.positions)

            def open_position(self, symbol, entry_price=None, entry_change_24h=0.0):
                pos = SimpleNamespace(symbol=symbol)
                self.positions.append(pos)
                return pos

        trader = FakeTrader()
        strategy = Strategy(trader=trader)
        opened = strategy.execute_signals([
            {"symbol": "RANK51", "cmc_rank": 51, "current_price": 1.0, "change_24h": -5.0},
            {"symbol": "NORANK", "current_price": 1.0, "change_24h": -5.0},
        ])

        self.assertEqual(opened, [])
        self.assertEqual(trader.get_open_positions(), [])

    def test_execute_signals_skips_excluded_stablecoin_symbols(self):
        """Stablecoins from stale or external data should never open futures trades."""

        class FakeTrader:
            def __init__(self):
                self.positions = []

            def get_open_positions(self):
                return list(self.positions)

            def open_position(self, symbol, entry_price=None, entry_change_24h=0.0):
                pos = SimpleNamespace(symbol=symbol)
                self.positions.append(pos)
                return pos

        trader = FakeTrader()
        strategy = Strategy(trader=trader)
        opened = strategy.execute_signals([
            {"symbol": "USDG", "cmc_rank": 20, "current_price": 1.0, "change_24h": -0.1},
            {"symbol": "GOOD", "cmc_rank": 21, "current_price": 2.0, "change_24h": -5.0},
        ])

        self.assertEqual([p.symbol for p in opened], ["GOOD"])
        self.assertEqual([p.symbol for p in trader.get_open_positions()], ["GOOD"])

    def test_fill_empty_slots_skips_stale_outside_top_50_basket_entries(self):
        """Stale basket rows outside top 50 should not refill empty slots."""

        class FakeFetcher:
            def get_top_coins(self):
                return [
                    {"symbol": "GOOD", "cmc_rank": 12, "price": 2.0, "percent_change_24h": -4.0},
                    {"symbol": "STALE", "cmc_rank": 51, "price": 1.0, "percent_change_24h": -9.0},
                ]

        class FakeTrader:
            cash_balance = 1000

            def __init__(self):
                self.positions = []

            def get_open_positions(self):
                return list(self.positions)

            def open_position(self, symbol, entry_price=None, entry_change_24h=0.0):
                pos = SimpleNamespace(symbol=symbol)
                self.positions.append(pos)
                return pos

        trader = FakeTrader()
        strategy = Strategy(fetcher=FakeFetcher(), trader=trader)
        strategy.basket = [
            {"symbol": "GOOD", "cmc_rank": 12},
            {"symbol": "STALE", "cmc_rank": 51},
        ]

        opened = strategy.fill_empty_slots()

        self.assertEqual([p.symbol for p in opened], ["GOOD"])
        self.assertEqual([p.symbol for p in trader.get_open_positions()], ["GOOD"])

    def test_fill_empty_slots_uses_max_open_trades_limit(self):
        """Slot filling stops at MAX_OPEN_TRADES even when the basket is larger."""

        config.TOP_N_LOSERS = 10
        config.MAX_OPEN_TRADES = 2

        class FakeFetcher:
            def get_top_coins(self):
                return [
                    {"symbol": "AAA", "cmc_rank": 10, "price": 1.0, "percent_change_24h": -6.0},
                    {"symbol": "BBB", "cmc_rank": 11, "price": 2.0, "percent_change_24h": -5.0},
                    {"symbol": "CCC", "cmc_rank": 12, "price": 3.0, "percent_change_24h": -4.0},
                ]

        class FakeTrader:
            cash_balance = 1000

            def __init__(self):
                self.positions = []

            def get_open_positions(self):
                return list(self.positions)

            def open_position(self, symbol, entry_price=None, entry_change_24h=0.0):
                pos = SimpleNamespace(symbol=symbol)
                self.positions.append(pos)
                return pos

        trader = FakeTrader()
        strategy = Strategy(fetcher=FakeFetcher(), trader=trader)
        strategy.basket = [
            {"symbol": "AAA", "cmc_rank": 10},
            {"symbol": "BBB", "cmc_rank": 11},
            {"symbol": "CCC", "cmc_rank": 12},
        ]

        opened = strategy.fill_empty_slots()

        self.assertEqual([p.symbol for p in opened], ["AAA", "BBB"])
        self.assertEqual(len(trader.get_open_positions()), 2)

    def test_fill_empty_slots_skips_excluded_symbols_from_stale_basket(self):
        """A stale basket containing a newly excluded stablecoin should not refill it."""

        class FakeFetcher:
            def get_top_coins(self):
                return [
                    {"symbol": "USDG", "cmc_rank": 20, "price": 1.0, "percent_change_24h": -8.0},
                    {"symbol": "GOOD", "cmc_rank": 21, "price": 2.0, "percent_change_24h": -4.0},
                ]

        class FakeTrader:
            cash_balance = 1000

            def __init__(self):
                self.positions = []

            def get_open_positions(self):
                return list(self.positions)

            def open_position(self, symbol, entry_price=None, entry_change_24h=0.0):
                pos = SimpleNamespace(symbol=symbol)
                self.positions.append(pos)
                return pos

        trader = FakeTrader()
        strategy = Strategy(fetcher=FakeFetcher(), trader=trader)
        strategy.basket = [
            {"symbol": "USDG", "cmc_rank": 20},
            {"symbol": "GOOD", "cmc_rank": 21},
        ]

        opened = strategy.fill_empty_slots()

        self.assertEqual([p.symbol for p in opened], ["GOOD"])
        self.assertEqual([p.symbol for p in trader.get_open_positions()], ["GOOD"])

    def test_should_refresh_basket_when_empty(self):
        """Should refresh when basket is empty."""
        strategy = Strategy()
        self.assertTrue(strategy.should_refresh_basket())

    def test_should_refresh_basket_on_new_month(self):
        """Should refresh when month changes."""
        strategy = Strategy()
        strategy.basket = [{"symbol": "BTC"}]
        strategy.basket_month = 3
        strategy.basket_year = 2026

        april = datetime(2026, 4, 1, tzinfo=timezone.utc)
        self.assertTrue(strategy.should_refresh_basket(april))

    def test_should_not_refresh_same_month(self):
        """Should not refresh within the same month."""
        strategy = Strategy()
        strategy.basket = [{"symbol": "BTC"}]
        strategy.basket_month = 3
        strategy.basket_year = 2026

        mid_march = datetime(2026, 3, 15, tzinfo=timezone.utc)
        self.assertFalse(strategy.should_refresh_basket(mid_march))

    def test_detect_dips_from_prices(self):
        """Should detect dips at -2% threshold."""
        strategy = Strategy()
        strategy.basket = [
            {"symbol": "BTC", "price": 65000},
            {"symbol": "ETH", "price": 3000},
            {"symbol": "SOL", "price": 100},
        ]

        price_data = {
            "BTC": {"price": 63000, "change_pct": -3.1},   # Dipping
            "ETH": {"price": 2970, "change_pct": -1.0},    # Not enough
            "SOL": {"price": 95, "change_pct": -5.0},      # Dipping
        }

        dips = strategy.detect_dips_from_prices(price_data)

        symbols = [d["symbol"] for d in dips]
        self.assertIn("BTC", symbols)
        self.assertIn("SOL", symbols)
        self.assertNotIn("ETH", symbols)

    def test_detect_dips_empty_basket(self):
        """Should return empty list when basket is empty."""
        strategy = Strategy()
        dips = strategy.detect_dips_from_prices({"BTC": {"price": 60000, "change_pct": -5.0}})
        self.assertEqual(dips, [])


if __name__ == "__main__":
    unittest.main()
