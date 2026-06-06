"""Tests for the paper-only Binance USDT-M Futures trader."""

import csv
import json
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from bot import config
from bot.modules import accounting
from bot.modules.futures_trader import (
    _history_csv,
    _open_positions_json,
    analyze_pre_trade_klines,
    FuturesPositionStatus,
    FuturesTrader,
    recent_reset_sessions,
)
from tests.support import isolate_data_dir


def _klines_from_closes(closes):
    rows = []
    prev = closes[0]
    for close in closes:
        high = max(prev, close) * 1.001
        low = min(prev, close) * 0.999
        rows.append([0, str(prev), str(high), str(low), str(close), "1000"])
        prev = close
    return rows


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
        config.CRASH_EMERGENCY_SL_ENABLED = False
        config.MAX_HOLD_DAYS = 3
        config.BREAK_EVEN_TRIGGER_PCT = 0.005
        config.LOSS_COOLDOWN_HOURS = 24
        config.TP_COOLDOWN_HOURS = 1
        config.MONTHLY_CONTRIBUTION_USD = 0
        config.MONTHLY_CONTRIBUTION_DAY = 1
        config.PRE_TRADE_BREAKDOWN_GUARD_ENABLED = True
        config.PRE_TRADE_MAX_24H_DROP_PCT = 8.0
        config.PRE_TRADE_MAX_LOWER_CLOSE_STREAK = 5
        config.PRE_TRADE_MAX_BELOW_SMA20_PCT = 1.5
        config.PRE_TRADE_MIN_BREAKDOWN_REBOUND_PCT = 1.0
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

    def test_pre_trade_analysis_blocks_falling_knife(self):
        closes = [100 - (i * 0.12) for i in range(97)]

        analysis = analyze_pre_trade_klines("TEST", _klines_from_closes(closes))

        self.assertEqual(analysis.decision, "WAIT")
        self.assertFalse(analysis.allowed)
        self.assertIn("no rebound", analysis.reason)

    def test_pre_trade_breakdown_guard_blocks_lower_close_streak(self):
        closes = [100.0] * 84
        closes += [99.8, 99.6, 99.4, 99.2, 99.0, 98.8, 98.6, 98.4, 98.2, 98.0, 97.8, 97.6, 97.4]

        analysis = analyze_pre_trade_klines("TEST", _klines_from_closes(closes))

        self.assertEqual(analysis.decision, "WAIT")
        self.assertFalse(analysis.allowed)
        self.assertIn("breakdown guard", analysis.reason)
        self.assertIn("lower closes", analysis.reason)

    def test_pre_trade_analysis_allows_rebound_wave(self):
        closes = [100 - (i * 0.08) for i in range(70)]
        closes += [94.4, 94.2, 94.0, 94.3, 94.7, 95.1, 95.5, 95.9, 96.2]
        closes += [96.4] * (97 - len(closes))

        analysis = analyze_pre_trade_klines("TEST", _klines_from_closes(closes))

        self.assertEqual(analysis.decision, "RUN")
        self.assertTrue(analysis.allowed)
        self.assertGreaterEqual(analysis.score, config.PRE_TRADE_MIN_SCORE)

    def test_get_kline_window_change_returns_close_to_close_pct(self):
        class FakeClient:
            def futures_klines(self, symbol, interval, limit):
                self.args = (symbol, interval, limit)
                return _klines_from_closes([100, 99, 98, 97, 96])

        fake = FakeClient()
        self.trader._client = fake

        change = self.trader.get_kline_window_change("BTC", interval="15m", limit=5)

        self.assertAlmostEqual(change, -4.0)
        self.assertEqual(fake.args, ("BTCUSDT", "15m", 5))

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
        with patch.object(self.trader, "get_current_price", return_value=closed_seed.tp_price * 1.01):
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
        with patch.object(self.trader, "get_current_price", return_value=pos.tp_price * 1.01):
            closed = self.trader.check_positions()
        self.assertEqual(len(closed), 1)
        self.assertEqual(closed[0].status, FuturesPositionStatus.TP_HIT)
        self.assertGreater(closed[0].pnl_pct, 0)
        self.assertAlmostEqual(closed[0].pnl_pct, config.FUTURES_NET_TP_PCT * 100, delta=0.05)

    def test_closed_pnl_includes_entry_and_exit_fees(self):
        config.FUNDING_RATE_DAILY = 0
        with patch.object(self.trader, "get_current_price", return_value=100.0):
            pos = self.trader.open_position("ETH", margin_usd=1000)
        close_price = pos.tp_price * 1.01
        entry_fee = pos.notional * config.FUTURES_FEE_PCT

        with patch.object(self.trader, "get_current_price", return_value=close_price):
            closed = self.trader.check_positions()

        close_price = closed[0].exit_price
        gross = (close_price - pos.entry_price) * pos.quantity
        exit_fee = (pos.quantity * close_price) * config.FUTURES_FEE_PCT
        expected_pnl = gross - entry_fee - exit_fee
        self.assertAlmostEqual(closed[0].pnl_usd, expected_pnl, places=4)
        self.assertAlmostEqual(self.trader.cash_balance, config.CAPITAL_USD + expected_pnl, places=4)

    def test_sl_hit_yields_negative_pnl(self):
        with patch.object(self.trader, "get_current_price", return_value=100.0):
            pos = self.trader.open_position("ETH", margin_usd=1000)
        with patch.object(self.trader, "get_current_price", return_value=pos.sl_price):
            closed = self.trader.check_positions()
        self.assertEqual(closed[0].status, FuturesPositionStatus.SL_HIT)
        self.assertLess(closed[0].pnl_pct, 0)

    def test_crash_protected_stop_uses_crash_sl_reason(self):
        config.FUTURES_USE_SL = False
        config.CRASH_EMERGENCY_SL_ENABLED = True
        with patch.object(self.trader, "get_current_price", return_value=100.0):
            pos = self.trader.open_position("ETH", margin_usd=1000)
        pos.crash_protected = True
        pos.sl_price = 98.0

        with patch.object(self.trader, "get_current_price", return_value=97.0):
            closed = self.trader.check_positions()

        self.assertEqual(closed[0].status, FuturesPositionStatus.CRASH_SL_HIT)
        self.assertEqual(closed[0].exit_price, 98.0)
        self.assertLess(closed[0].pnl_pct, 0)

    def test_crash_protected_stop_is_ignored_when_emergency_sl_disabled(self):
        config.FUTURES_USE_SL = False
        config.CRASH_EMERGENCY_SL_ENABLED = False
        with patch.object(self.trader, "get_current_price", return_value=100.0):
            pos = self.trader.open_position("ETH", margin_usd=1000)
        pos.crash_protected = True
        pos.sl_price = 98.0

        with patch.object(self.trader, "get_current_price", return_value=97.0):
            closed = self.trader.check_positions()

        self.assertEqual(closed, [])
        self.assertEqual(pos.status, FuturesPositionStatus.OPEN)

    def test_liquidation(self):
        with patch.object(self.trader, "get_current_price", return_value=100.0):
            pos = self.trader.open_position("ETH", margin_usd=9500)
        with patch.object(self.trader, "get_current_price", return_value=pos.liquidation_price):
            closed = self.trader.check_positions()
        self.assertEqual(closed[0].status, FuturesPositionStatus.LIQUIDATED)
        self.assertLess(closed[0].pnl_pct, -100.0)
        self.assertGreaterEqual(self.trader.cash_balance, 0)

    def test_account_level_cross_liquidation_closes_underwater_book(self):
        config.LEVERAGE = 20
        config.FUTURES_USE_SL = False
        trader = FuturesTrader()

        with patch.object(trader, "get_current_price", return_value=100.0):
            for symbol in ("BTC", "ETH", "BNB"):
                self.assertIsNotNone(trader.open_position(symbol, margin_usd=3000))

        with patch.object(trader, "get_current_price", return_value=80.0):
            closed = trader.check_positions()

        self.assertEqual(len(closed), 3)
        self.assertEqual(trader.get_open_positions(), [])
        self.assertTrue(all(pos.status == FuturesPositionStatus.LIQUIDATED for pos in closed))
        self.assertGreaterEqual(trader.cash_balance, 0)

    def test_upside_cross_liq_does_not_override_tp(self):
        config.LEVERAGE = 20
        with patch.object(self.trader, "get_current_price", return_value=100.0):
            pos = self.trader.open_position("ETH", margin_usd=100)
        self.assertIsNotNone(pos)
        pos.liquidation_price = pos.tp_price * 2

        with (
            patch.object(self.trader, "refresh_cross_liquidation_prices", return_value=None),
            patch.object(self.trader, "get_current_price", return_value=pos.tp_price * 1.01),
        ):
            closed = self.trader.check_positions()

        self.assertEqual(len(closed), 1)
        self.assertEqual(closed[0].status, FuturesPositionStatus.TP_HIT)
        self.assertLess(closed[0].pnl_pct, 5.0)

    def test_positive_liquidation_history_is_repaired_to_tp(self):
        now = datetime.now(timezone.utc)
        earlier = now - timedelta(hours=1)
        row = {
            "open_time": earlier.isoformat(),
            "close_time": now.isoformat(),
            "symbol": "BTC",
            "engine": "futures",
            "entry_price": "100",
            "exit_price": "120",
            "amount_usd": "100",
            "notional": "2000",
            "leverage": "20",
            "pnl_pct": "395",
            "pnl_usd": "395",
            "entry_fee": "1.2",
            "exit_fee": "1.44",
            "funding_paid": "0.0",
            "pnl_model": "net_includes_entry_fee",
            "reason": "liquidated",
            "entry_change_24h": "-2.5",
        }
        path = _history_csv()
        with open(path, "w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=list(row.keys()))
            writer.writeheader()
            writer.writerow(row)

        repaired = FuturesTrader()
        loaded = repaired.get_trade_history()[0]
        with open(path, "r", newline="", encoding="utf-8") as handle:
            rows = list(csv.DictReader(handle))

        self.assertEqual(loaded.status, FuturesPositionStatus.TP_HIT)
        self.assertEqual(rows[0]["reason"], "tp_hit")
        self.assertLess(float(rows[0]["exit_price"]), 101.0)
        self.assertGreater(loaded.pnl_usd, 0)
        self.assertLess(loaded.pnl_pct, 5.0)

    def test_legacy_tp_history_is_repaired_to_configured_net_target(self):
        now = datetime.now(timezone.utc)
        earlier = now - timedelta(days=4)
        row = {
            "open_time": earlier.isoformat(),
            "close_time": now.isoformat(),
            "symbol": "XLM",
            "engine": "futures",
            "entry_price": "0.1498",
            "exit_price": "0.15012207",
            "amount_usd": "148.1111",
            "notional": "2962.2216",
            "leverage": "20",
            "pnl_pct": "-0.4912",
            "pnl_usd": "-0.7276",
            "entry_fee": "1.77733299",
            "exit_fee": "1.78115425",
            "funding_paid": "3.5379",
            "pnl_model": "net_includes_entry_fee",
            "reason": "tp_hit",
            "entry_change_24h": "-1.6422",
        }
        path = _history_csv()
        with open(path, "w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=list(row.keys()))
            writer.writeheader()
            writer.writerow(row)

        repaired = FuturesTrader()
        loaded = repaired.get_trade_history()[0]
        with open(path, "r", newline="", encoding="utf-8") as handle:
            rows = list(csv.DictReader(handle))

        self.assertEqual(loaded.status, FuturesPositionStatus.TP_HIT)
        self.assertGreater(loaded.pnl_usd, 0)
        self.assertAlmostEqual(loaded.pnl_pct, config.FUTURES_NET_TP_PCT * 100, delta=0.05)
        self.assertGreater(float(rows[0]["exit_price"]), float(row["exit_price"]))

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
