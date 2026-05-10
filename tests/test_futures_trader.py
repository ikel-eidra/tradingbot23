"""Tests for the paper-only Binance USDT-M Futures trader."""

import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from bot import config
from bot.modules import accounting
from bot.modules.futures_trader import FuturesPositionStatus, FuturesTrader
from tests.support import isolate_data_dir


class TestFuturesTrader(unittest.TestCase):
    def setUp(self):
        isolate_data_dir(self)
        config.CAPITAL_USD = 10000
        config.PER_TRADE_PCT = 0.20
        config.LEVERAGE = 2
        config.FUTURES_FEE_PCT = 0.0006
        config.FUNDING_RATE_DAILY = 0.0003
        config.FUTURES_NET_TP_PCT = 0.005
        config.FUTURES_NET_SL_PCT = 0.0075
        config.FUTURES_USE_SL = True
        config.MAX_HOLD_DAYS = 3
        config.BREAK_EVEN_TRIGGER_PCT = 0.005
        config.LOSS_COOLDOWN_HOURS = 24
        config.TP_COOLDOWN_HOURS = 1
        config.MONTHLY_CONTRIBUTION_USD = 0
        config.MONTHLY_CONTRIBUTION_DAY = 1
        self.trader = FuturesTrader()

    def test_leverage_clamp(self):
        config.LEVERAGE = 999
        t = FuturesTrader()
        self.assertEqual(t.leverage, config.MAX_LEVERAGE)

    def test_open_paper_long(self):
        with patch.object(self.trader, "get_current_price", return_value=100.0):
            pos = self.trader.open_position("BTC", margin_usd=1000)
        self.assertIsNotNone(pos)
        self.assertEqual(pos.leverage, 2)
        self.assertAlmostEqual(pos.notional, 2000, places=2)
        self.assertGreater(pos.tp_price, 100)
        self.assertLess(pos.sl_price, 100)
        self.assertLess(pos.liquidation_price, pos.sl_price)

    def test_open_caps_margin_to_include_entry_fee(self):
        self.trader.cash_balance = 100
        with patch.object(self.trader, "get_current_price", return_value=100.0):
            pos = self.trader.open_position("BTC", margin_usd=100)
        self.assertIsNotNone(pos)
        self.assertLess(pos.margin_used, 100)
        self.assertGreaterEqual(self.trader.cash_balance, 0)

    def test_monthly_contribution_applies_once_per_month(self):
        config.MONTHLY_CONTRIBUTION_USD = 100
        config.MONTHLY_CONTRIBUTION_DAY = 1
        may_first = datetime(2026, 5, 1, tzinfo=timezone.utc)

        self.assertEqual(self.trader.apply_monthly_contribution(may_first), 100)
        self.assertAlmostEqual(self.trader.cash_balance, 10100)
        self.assertEqual(self.trader.apply_monthly_contribution(may_first), 0)
        self.assertAlmostEqual(self.trader.cash_balance, 10100)
        self.assertAlmostEqual(self.trader.get_contributed_capital(), 10100)

    def test_monthly_contribution_applies_after_due_day(self):
        config.MONTHLY_CONTRIBUTION_USD = 100
        config.MONTHLY_CONTRIBUTION_DAY = 1
        may_tenth = datetime(2026, 5, 10, tzinfo=timezone.utc)

        self.assertEqual(self.trader.apply_monthly_contribution(may_tenth), 100)
        self.assertAlmostEqual(self.trader.cash_balance, 10100)

    def test_contribution_schedule_marks_one_year(self):
        config.MONTHLY_CONTRIBUTION_USD = 100
        config.MONTHLY_CONTRIBUTION_DAY = 31
        now = datetime(2026, 2, 28, tzinfo=timezone.utc)

        self.assertEqual(self.trader.apply_monthly_contribution(now), 100)
        rows = accounting.contribution_schedule(months=12, now=now)

        self.assertEqual(len(rows), 12)
        self.assertEqual(rows[0]["month"], "2026-02")
        self.assertEqual(rows[0]["date"], "2026-02-28")
        self.assertEqual(rows[0]["status"], "paid")
        self.assertEqual(rows[1]["month"], "2026-03")
        self.assertEqual(rows[1]["date"], "2026-03-31")
        self.assertEqual(rows[1]["status"], "scheduled")

    def test_no_duplicate(self):
        with patch.object(self.trader, "get_current_price", return_value=100.0):
            p1 = self.trader.open_position("BTC", margin_usd=500)
            p2 = self.trader.open_position("BTC", margin_usd=500)
        self.assertIsNotNone(p1)
        self.assertIsNone(p2)

    def test_tp_hit_yields_positive_pnl(self):
        with patch.object(self.trader, "get_current_price", return_value=100.0):
            pos = self.trader.open_position("ETH", margin_usd=1000)
        with patch.object(self.trader, "get_current_price", return_value=pos.tp_price):
            closed = self.trader.check_positions()
        self.assertEqual(len(closed), 1)
        self.assertEqual(closed[0].status, FuturesPositionStatus.TP_HIT)
        self.assertGreater(closed[0].pnl_pct, 0)

    def test_sl_hit_yields_negative_pnl(self):
        with patch.object(self.trader, "get_current_price", return_value=100.0):
            pos = self.trader.open_position("ETH", margin_usd=1000)
        with patch.object(self.trader, "get_current_price", return_value=pos.sl_price):
            closed = self.trader.check_positions()
        self.assertEqual(closed[0].status, FuturesPositionStatus.SL_HIT)
        self.assertLess(closed[0].pnl_pct, 0)

    def test_liquidation(self):
        with patch.object(self.trader, "get_current_price", return_value=100.0):
            pos = self.trader.open_position("ETH", margin_usd=1000)
        with patch.object(self.trader, "get_current_price", return_value=pos.liquidation_price - 1):
            closed = self.trader.check_positions()
        self.assertEqual(closed[0].status, FuturesPositionStatus.LIQUIDATED)
        self.assertEqual(closed[0].pnl_pct, -100.0)

    def test_expiry(self):
        with patch.object(self.trader, "get_current_price", return_value=100.0):
            pos = self.trader.open_position("ETH", margin_usd=1000)
        pos.entry_time = datetime.now(timezone.utc) - timedelta(days=4)
        with patch.object(self.trader, "get_current_price", return_value=100.05):
            closed = self.trader.check_positions()
        self.assertEqual(closed[0].status, FuturesPositionStatus.EXPIRED)

    def test_breakeven_arms_after_profit(self):
        with patch.object(self.trader, "get_current_price", return_value=100.0):
            pos = self.trader.open_position("ETH", margin_usd=1000)
        original_sl = pos.sl_price
        with patch.object(self.trader, "get_current_price", return_value=100.6):
            self.trader.check_positions()
        self.assertTrue(pos.breakeven_armed)
        self.assertGreater(pos.sl_price, original_sl)

    def test_loss_cooldown_blocks_reentry(self):
        with patch.object(self.trader, "get_current_price", return_value=100.0):
            self.trader.open_position("DUMP", margin_usd=500)
        with patch.object(self.trader, "get_current_price", return_value=99.0):
            self.trader.check_positions()
        with patch.object(self.trader, "get_current_price", return_value=99.0):
            new_pos = self.trader.open_position("DUMP", margin_usd=500)
        self.assertIsNone(new_pos)


if __name__ == "__main__":
    unittest.main()
