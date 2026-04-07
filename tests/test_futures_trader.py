"""Tests for the paper-only Binance USDT-M Futures trader."""

import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from bot import config
from bot.modules.futures_trader import FuturesPositionStatus, FuturesTrader


class TestFuturesTrader(unittest.TestCase):
    def setUp(self):
        config.CAPITAL_USD = 10000
        config.PER_TRADE_PCT = 0.20
        config.LEVERAGE = 2
        config.FUTURES_FEE_PCT = 0.0006
        config.FUNDING_RATE_DAILY = 0.0003
        config.FUTURES_NET_TP_PCT = 0.005
        config.FUTURES_NET_SL_PCT = 0.0075
        config.MAX_HOLD_DAYS = 3
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


if __name__ == "__main__":
    unittest.main()
