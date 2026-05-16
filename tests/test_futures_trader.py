"""Tests for the paper-only Binance USDT-M Futures trader."""

import json
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from bot import config
from bot.modules import accounting
from bot.modules.futures_trader import (
    _history_csv,
    _open_positions_json,
    FuturesPositionStatus,
    FuturesTrader,
    recent_reset_sessions,
)
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
        self.assertEqual(t.leverage, 20)

    def test_open_paper_long(self):
        with patch.object(self.trader, "get_current_price", return_value=100.0):
            pos = self.trader.open_position("BTC", margin_usd=1000)
        self.assertIsNotNone(pos)
        self.assertEqual(pos.leverage, 2)
        self.assertEqual(pos.margin_mode, "cross")
        self.assertAlmostEqual(pos.notional, 2000, places=2)
        self.assertGreater(pos.tp_price, 100)
        self.assertLess(pos.sl_price, 100)
        self.assertLess(pos.liquidation_price, pos.sl_price)

    def test_open_rejects_excluded_stablecoin_even_with_entry_price(self):
        with patch.object(self.trader, "get_current_price", return_value=1.0) as price_mock:
            pos = self.trader.open_position("USDG", margin_usd=100, entry_price=1.0)

        self.assertIsNone(pos)
        price_mock.assert_not_called()

    def test_open_validates_binance_price_when_entry_price_is_supplied(self):
        with patch.object(self.trader, "get_current_price", return_value=None):
            pos = self.trader.open_position("NOTBINANCE", margin_usd=100, entry_price=1.0)

        self.assertIsNone(pos)
        self.assertEqual(self.trader.get_open_positions(), [])

    def test_check_positions_closes_newly_excluded_open_position(self):
        with patch.object(self.trader, "get_current_price", return_value=1.0):
            pos = self.trader.open_position("BTC", margin_usd=100)
        self.assertIsNotNone(pos)
        pos.symbol = "USDG"

        closed = self.trader.check_positions()

        self.assertEqual(len(closed), 1)
        self.assertEqual(closed[0].status, FuturesPositionStatus.EXCLUDED)
        self.assertEqual(self.trader.get_open_positions(), [])
        saved = json.loads(_open_positions_json().read_text(encoding="utf-8"))
        self.assertEqual(saved["positions"], [])

    def test_cross_margin_keeps_20x_small_trade_liquidation_far(self):
        config.LEVERAGE = 20
        config.CAPITAL_USD = 5000
        trader = FuturesTrader()

        with patch.object(trader, "get_current_price", return_value=100.0):
            pos = trader.open_position("BTC", margin_usd=100)

        self.assertIsNotNone(pos)
        self.assertEqual(pos.leverage, 20)
        self.assertLess(pos.liquidation_price, 50)

    def test_open_caps_margin_to_include_entry_fee(self):
        self.trader.cash_balance = 100
        with patch.object(self.trader, "get_current_price", return_value=100.0):
            pos = self.trader.open_position("BTC", margin_usd=100)
        self.assertIsNotNone(pos)
        self.assertLess(pos.margin_used, 100)
        self.assertGreaterEqual(self.trader.cash_balance, 0)

    def test_sync_starting_capital_adds_free_cash(self):
        with patch.object(self.trader, "get_current_price", return_value=100.0):
            self.trader.open_position("BTC", margin_usd=1000)
        cash_before = self.trader.cash_balance

        applied = self.trader.sync_starting_capital(15000)

        self.assertAlmostEqual(applied, 5000, places=2)
        self.assertAlmostEqual(self.trader.cash_balance, cash_before + 5000, places=2)
        self.assertAlmostEqual(self.trader.account_capital_usd, 15000)

    def test_sync_starting_capital_infers_legacy_smaller_account(self):
        config.CAPITAL_USD = 500
        trader = FuturesTrader()
        with patch.object(trader, "get_current_price", return_value=100.0):
            trader.open_position("BTC", margin_usd=100)
        config.CAPITAL_USD = 5000

        applied = trader.sync_starting_capital(5000)

        self.assertAlmostEqual(applied, 4500, places=2)
        self.assertGreater(trader.cash_balance, 4800)
        self.assertAlmostEqual(trader.account_capital_usd, 5000)

    def test_load_open_positions_auto_syncs_legacy_capital(self):
        config.CAPITAL_USD = 500
        trader = FuturesTrader()
        with patch.object(trader, "get_current_price", return_value=100.0):
            trader.open_position("BTC", margin_usd=100)
        path = config.DATA_DIR / "open_positions.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        data.pop("starting_capital_usd", None)
        data.pop("total_contributed_capital", None)
        path.write_text(json.dumps(data), encoding="utf-8")

        config.CAPITAL_USD = 5000
        reloaded = FuturesTrader()

        self.assertGreater(reloaded.cash_balance, 4800)
        self.assertAlmostEqual(reloaded.account_capital_usd, 5000)
        saved = json.loads(path.read_text(encoding="utf-8"))
        self.assertAlmostEqual(saved["starting_capital_usd"], 5000)

    def test_sync_starting_capital_rejects_unavailable_withdrawal(self):
        with patch.object(self.trader, "get_current_price", return_value=100.0):
            self.trader.open_position("BTC", margin_usd=9500)

        with self.assertRaises(ValueError):
            self.trader.sync_starting_capital(100)

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

    def test_reset_paper_account_archives_and_starts_new_history(self):
        config.MONTHLY_CONTRIBUTION_USD = 100
        config.MONTHLY_CONTRIBUTION_DAY = 1
        self.trader.apply_monthly_contribution(datetime(2026, 5, 1, tzinfo=timezone.utc))

        with patch.object(self.trader, "get_current_price", return_value=100.0):
            closed_seed = self.trader.open_position("ETH", margin_usd=1000)
        with patch.object(self.trader, "get_current_price", return_value=closed_seed.tp_price):
            self.trader.check_positions()
        with patch.object(self.trader, "get_current_price", return_value=100.0):
            self.trader.open_position("BTC", margin_usd=500)

        self.assertTrue(_history_csv().exists())
        self.assertEqual(len(self.trader.get_trade_history()), 1)
        self.assertEqual(len(self.trader.get_open_positions()), 1)

        summary = self.trader.reset_paper_account(reason="test_reset")

        self.assertFalse(_history_csv().exists())
        self.assertEqual(self.trader.get_trade_history(), [])
        self.assertEqual(self.trader.get_open_positions(), [])
        self.assertAlmostEqual(self.trader.cash_balance, 10100)

        archive_dir = config.DATA_DIR / "futures_sessions" / summary["session_id"]
        self.assertTrue((archive_dir / "trade_history.csv").exists())
        self.assertTrue((archive_dir / "open_positions.json").exists())

        saved_open = json.loads(_open_positions_json().read_text(encoding="utf-8"))
        self.assertEqual(saved_open["positions"], [])
        self.assertAlmostEqual(saved_open["cash_balance"], 10100)

        sessions = recent_reset_sessions(limit=1)
        self.assertEqual(sessions[-1]["session_id"], summary["session_id"])
        self.assertEqual(int(sessions[-1]["archived_closed_trades"]), 1)
        self.assertEqual(int(sessions[-1]["archived_open_positions"]), 1)

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

    def test_closed_pnl_includes_entry_and_exit_fees(self):
        config.FUNDING_RATE_DAILY = 0
        with patch.object(self.trader, "get_current_price", return_value=100.0):
            pos = self.trader.open_position("ETH", margin_usd=1000)
        close_price = pos.tp_price
        gross = (close_price - pos.entry_price) * pos.quantity
        entry_fee = pos.notional * config.FUTURES_FEE_PCT
        exit_fee = (pos.quantity * close_price) * config.FUTURES_FEE_PCT
        expected_pnl = gross - entry_fee - exit_fee

        with patch.object(self.trader, "get_current_price", return_value=close_price):
            closed = self.trader.check_positions()

        self.assertAlmostEqual(closed[0].pnl_usd, expected_pnl, places=4)
        self.assertAlmostEqual(self.trader.cash_balance, config.CAPITAL_USD + expected_pnl, places=4)

    def test_sl_hit_yields_negative_pnl(self):
        with patch.object(self.trader, "get_current_price", return_value=100.0):
            pos = self.trader.open_position("ETH", margin_usd=1000)
        with patch.object(self.trader, "get_current_price", return_value=pos.sl_price):
            closed = self.trader.check_positions()
        self.assertEqual(closed[0].status, FuturesPositionStatus.SL_HIT)
        self.assertLess(closed[0].pnl_pct, 0)

    def test_liquidation(self):
        with patch.object(self.trader, "get_current_price", return_value=100.0):
            pos = self.trader.open_position("ETH", margin_usd=9500)
        with patch.object(self.trader, "get_current_price", return_value=pos.liquidation_price):
            closed = self.trader.check_positions()
        self.assertEqual(closed[0].status, FuturesPositionStatus.LIQUIDATED)
        self.assertLess(closed[0].pnl_pct, -100.0)
        self.assertGreaterEqual(self.trader.cash_balance, 0)

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
