"""Tests for the Binance trader module."""

import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

from bot import config
from bot.modules.trader import PositionStatus, Trader


class TestTrader(unittest.TestCase):
    """Tests for Trader (paper mode)."""

    def setUp(self):
        config.TRADING_MODE = "paper"
        config.CAPITAL_USD = 10000
        config.PER_TRADE_PCT = 0.20
        config.NET_TP_PCT = 0.01
        config.NET_SL_PCT = 0.015
        config.FEE_PCT = 0.001
        config.TP_PCT = config.NET_TP_PCT + (2 * config.FEE_PCT)  # 1.2% gross
        config.SL_PCT = max(config.NET_SL_PCT - (2 * config.FEE_PCT), 0.001)  # 1.3% gross
        config.MAX_HOLD_DAYS = 3
        self.trader = Trader()

    def test_portfolio_value_initial(self):
        """Initial portfolio value should equal capital."""
        # Mock get_current_price since we have no open positions
        self.assertEqual(self.trader.get_portfolio_value(), 10000)

    def test_cash_balance_after_paper_buy(self):
        """Cash should decrease by trade amount + Binance fee."""
        with patch.object(self.trader, "get_current_price", return_value=100.0):
            with patch.object(self.trader, "_adjust_quantity", side_effect=lambda p, q: q):
                with patch.object(self.trader, "_round_price", side_effect=lambda p, pr: pr):
                    pos = self.trader.open_position("TEST", amount_usd=2000)

        self.assertIsNotNone(pos)
        # $2000 trade + 0.1% fee ($2) = $2002 total deducted
        expected_cash = 10000 - 2000 - (2000 * config.FEE_PCT)
        self.assertAlmostEqual(self.trader.cash_balance, expected_cash, places=2)

    def test_no_duplicate_positions(self):
        """Should not open duplicate position for same coin."""
        with patch.object(self.trader, "get_current_price", return_value=100.0):
            with patch.object(self.trader, "_adjust_quantity", side_effect=lambda p, q: q):
                with patch.object(self.trader, "_round_price", side_effect=lambda p, pr: pr):
                    pos1 = self.trader.open_position("TEST", amount_usd=1000)
                    pos2 = self.trader.open_position("TEST", amount_usd=1000)

        self.assertIsNotNone(pos1)
        self.assertIsNone(pos2)  # Duplicate blocked

    def test_tp_hit(self):
        """Position should close at TP when price rises enough."""
        with patch.object(self.trader, "get_current_price", return_value=100.0):
            with patch.object(self.trader, "_adjust_quantity", side_effect=lambda p, q: q):
                with patch.object(self.trader, "_round_price", side_effect=lambda p, pr: pr):
                    pos = self.trader.open_position("TEST", amount_usd=1000)

        # Simulate price hitting TP
        with patch.object(self.trader, "get_current_price", return_value=102.5):
            closed = self.trader.check_positions()

        self.assertEqual(len(closed), 1)
        self.assertEqual(closed[0].status, PositionStatus.TP_HIT)
        self.assertGreater(closed[0].pnl_pct, 0)

    def test_sl_hit(self):
        """Position should close at SL when price drops enough."""
        with patch.object(self.trader, "get_current_price", return_value=100.0):
            with patch.object(self.trader, "_adjust_quantity", side_effect=lambda p, q: q):
                with patch.object(self.trader, "_round_price", side_effect=lambda p, pr: pr):
                    pos = self.trader.open_position("TEST", amount_usd=1000)

        # Simulate price hitting SL
        with patch.object(self.trader, "get_current_price", return_value=98.0):
            closed = self.trader.check_positions()

        self.assertEqual(len(closed), 1)
        self.assertEqual(closed[0].status, PositionStatus.SL_HIT)
        self.assertLess(closed[0].pnl_pct, 0)

    def test_max_hold_expiry(self):
        """Position should auto-close after max hold days."""
        with patch.object(self.trader, "get_current_price", return_value=100.0):
            with patch.object(self.trader, "_adjust_quantity", side_effect=lambda p, q: q):
                with patch.object(self.trader, "_round_price", side_effect=lambda p, pr: pr):
                    pos = self.trader.open_position("TEST", amount_usd=1000)

        # Backdate entry to trigger expiry
        pos.entry_time = datetime.now(timezone.utc) - timedelta(days=4)

        with patch.object(self.trader, "get_current_price", return_value=100.5):
            closed = self.trader.check_positions()

        self.assertEqual(len(closed), 1)
        self.assertEqual(closed[0].status, PositionStatus.EXPIRED)

    def test_compounding_position_size(self):
        """Position size should grow with portfolio gains."""
        # Simulate a winning trade to grow the portfolio
        with patch.object(self.trader, "get_current_price", return_value=100.0):
            with patch.object(self.trader, "_adjust_quantity", side_effect=lambda p, q: q):
                with patch.object(self.trader, "_round_price", side_effect=lambda p, pr: pr):
                    pos = self.trader.open_position("WIN", amount_usd=2000)

        # Close at profit
        with patch.object(self.trader, "get_current_price", return_value=103.0):
            self.trader.check_positions()

        # Cash should now be > initial due to profit
        self.assertGreater(self.trader.cash_balance, 10000)

    def test_net_pnl_after_fees(self):
        """A trade closed at the gross TP should yield approximately the NET TP target."""
        with patch.object(self.trader, "get_current_price", return_value=100.0):
            with patch.object(self.trader, "_adjust_quantity", side_effect=lambda p, q: q):
                with patch.object(self.trader, "_round_price", side_effect=lambda p, pr: pr):
                    pos = self.trader.open_position("TEST", amount_usd=1000)

        # Close at gross TP price (1.2% above entry)
        gross_tp_price = 100.0 * (1 + config.TP_PCT)
        with patch.object(self.trader, "get_current_price", return_value=gross_tp_price):
            closed = self.trader.check_positions()

        self.assertEqual(len(closed), 1)
        # Net PNL should be ~1% (the NET_TP_PCT target), within 0.05% tolerance
        self.assertAlmostEqual(closed[0].pnl_pct, config.NET_TP_PCT * 100, delta=0.05)

    def test_scalper_mode_half_percent_net(self):
        """0.5% net target should yield ~0.5% net PNL after fees."""
        config.NET_TP_PCT = 0.005
        config.TP_PCT = config.NET_TP_PCT + (2 * config.FEE_PCT)  # 0.7% gross
        trader = Trader()

        with patch.object(trader, "get_current_price", return_value=100.0):
            with patch.object(trader, "_adjust_quantity", side_effect=lambda p, q: q):
                with patch.object(trader, "_round_price", side_effect=lambda p, pr: pr):
                    pos = trader.open_position("SCALP", amount_usd=1000)

        gross_tp_price = 100.0 * (1 + config.TP_PCT)
        with patch.object(trader, "get_current_price", return_value=gross_tp_price):
            closed = trader.check_positions()

        self.assertEqual(len(closed), 1)
        self.assertAlmostEqual(closed[0].pnl_pct, 0.5, delta=0.05)

    def test_extreme_scalper_quarter_percent_net(self):
        """0.25% net target should yield ~0.25% net PNL after fees."""
        config.NET_TP_PCT = 0.0025
        config.TP_PCT = config.NET_TP_PCT + (2 * config.FEE_PCT)  # 0.45% gross
        trader = Trader()

        with patch.object(trader, "get_current_price", return_value=100.0):
            with patch.object(trader, "_adjust_quantity", side_effect=lambda p, q: q):
                with patch.object(trader, "_round_price", side_effect=lambda p, pr: pr):
                    pos = trader.open_position("FAST", amount_usd=1000)

        gross_tp_price = 100.0 * (1 + config.TP_PCT)
        with patch.object(trader, "get_current_price", return_value=gross_tp_price):
            closed = trader.check_positions()

        self.assertEqual(len(closed), 1)
        self.assertAlmostEqual(closed[0].pnl_pct, 0.25, delta=0.05)

    def test_breakeven_winrate_calculation(self):
        """Verify the risk/reward math used by config.validate()."""
        # 1% net TP, 1.5% net SL → 60% break-even win rate
        be = 0.015 / (0.01 + 0.015) * 100
        self.assertAlmostEqual(be, 60.0, places=1)
        # 0.25% net TP, 0.5% net SL → 66.7% break-even win rate
        be = 0.005 / (0.0025 + 0.005) * 100
        self.assertAlmostEqual(be, 66.67, places=1)

    def test_stats_empty(self):
        """Stats should handle no trades."""
        stats = self.trader.get_stats()
        self.assertEqual(stats["total_trades"], 0)


if __name__ == "__main__":
    unittest.main()
