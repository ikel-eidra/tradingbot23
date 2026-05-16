"""Tests for P2P arbitrage route scoring."""

import json
import tempfile
import unittest
from pathlib import Path

from bot.modules.p2p_arbitrage import (
    P2PJournal,
    P2PPaperArb,
    P2PRealismSettings,
    P2PRouteSettings,
    apply_realism_to_hold_evaluation,
    apply_realism_to_sweep,
    build_depth_sweep,
    build_p2p_routes,
    build_p2p_hold_entry,
)
from bot.modules.p2p_monitor import P2PAd, P2PSnapshot


def p2p_ad(side, marketplace, price, min_limit, max_limit, available, advertiser, orders=100, rate=0.99):
    return P2PAd(
        side=side,
        asset="USDT",
        fiat="PHP",
        marketplace=marketplace,
        price=price,
        min_limit=min_limit,
        max_limit=max_limit,
        available=available,
        methods=("GCash", "Bank Transfer"),
        advertiser=advertiser,
        orders=orders,
        completion_rate=rate,
    )


class TestP2PArbitrage(unittest.TestCase):
    def test_build_routes_uses_common_capacity_and_profit(self):
        snapshot = P2PSnapshot(
            marketplace="Binance",
            asset="USDT",
            fiat="PHP",
            buy_ads=[
                p2p_ad("BUY", "Binance", 60.00, 1_000, 200_000, 5_000, "seller"),
            ],
            sell_ads=[
                p2p_ad("SELL", "Binance", 60.30, 1_000, 150_000, 5_000, "buyer"),
            ],
            as_of=None,
        )

        routes = build_p2p_routes(
            [snapshot],
            P2PRouteSettings(capital_php=500_000, min_profit_php=1, min_profit_pct=0.1),
        )

        self.assertEqual(len(routes), 1)
        route = routes[0]
        self.assertEqual(route.route_label, "Binance -> Binance")
        self.assertEqual(route.size_php, 150_000)
        self.assertAlmostEqual(route.profit_php, 750.0)
        self.assertAlmostEqual(route.profit_pct, 0.5)
        self.assertEqual(route.grade, "A")

    def test_cross_exchange_route_applies_transfer_fee(self):
        buy_snapshot = P2PSnapshot(
            marketplace="OKX",
            asset="USDT",
            fiat="PHP",
            buy_ads=[p2p_ad("BUY", "OKX", 60.00, 1_000, 100_000, 5_000, "okx seller")],
            sell_ads=[],
            as_of=None,
        )
        sell_snapshot = P2PSnapshot(
            marketplace="Bybit",
            asset="USDT",
            fiat="PHP",
            buy_ads=[],
            sell_ads=[p2p_ad("SELL", "Bybit", 60.60, 1_000, 100_000, 5_000, "bybit buyer")],
            as_of=None,
        )

        routes = build_p2p_routes(
            [buy_snapshot, sell_snapshot],
            P2PRouteSettings(
                capital_php=60_000,
                min_profit_php=1,
                min_profit_pct=0.1,
                cross_exchange_transfer_fee_usdt=1,
            ),
        )

        self.assertEqual(len(routes), 1)
        route = routes[0]
        self.assertEqual(route.route_label, "OKX -> Bybit")
        self.assertAlmostEqual(route.buy_usdt, 1_000)
        self.assertAlmostEqual(route.sell_usdt, 999)
        self.assertAlmostEqual(route.profit_php, 539.4)

    def test_depth_sweep_applies_cross_exchange_transfer_fee(self):
        buy_snapshot = P2PSnapshot(
            marketplace="Binance",
            asset="USDT",
            fiat="PHP",
            buy_ads=[p2p_ad("BUY", "Binance", 60.00, 1_000, 100_000, 5_000, "seller")],
            sell_ads=[],
            as_of=None,
        )
        sell_snapshot = P2PSnapshot(
            marketplace="OKX",
            asset="USDT",
            fiat="PHP",
            buy_ads=[],
            sell_ads=[p2p_ad("SELL", "OKX", 60.60, 1_000, 100_000, 5_000, "buyer")],
            as_of=None,
        )

        sweep = build_depth_sweep(
            [buy_snapshot, sell_snapshot],
            P2PRouteSettings(
                capital_php=60_000,
                min_profit_pct=0.1,
                cross_exchange_transfer_fee_usdt=1,
            ),
        )

        self.assertIsNotNone(sweep)
        self.assertEqual(sweep.transfer_fee_usdt, 1)
        self.assertAlmostEqual(sweep.buy_usdt, 1_000)
        self.assertAlmostEqual(sweep.sell_usdt, 999)
        self.assertAlmostEqual(sweep.profit_php, 539.4)

    def test_journal_appends_and_reads_recent_routes(self):
        snapshot = P2PSnapshot(
            marketplace="Binance",
            asset="USDT",
            fiat="PHP",
            buy_ads=[p2p_ad("BUY", "Binance", 60.00, 1_000, 100_000, 5_000, "seller")],
            sell_ads=[p2p_ad("SELL", "Binance", 60.30, 1_000, 100_000, 5_000, "buyer")],
            as_of=None,
        )
        route = build_p2p_routes(
            [snapshot],
            P2PRouteSettings(capital_php=50_000, min_profit_php=1, min_profit_pct=0.1),
        )[0]

        with tempfile.TemporaryDirectory() as tmpdir:
            journal = P2PJournal(path=Path(tmpdir) / "journal.csv")
            journal.append(route, status="PLANNED", notes="test")
            rows = journal.recent()

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["status"], "PLANNED")
        self.assertEqual(rows[0]["route"], "Binance -> Binance")
        self.assertEqual(rows[0]["notes"], "test")

    def test_depth_sweep_uses_multiple_ads_and_weighted_average(self):
        snapshot = P2PSnapshot(
            marketplace="Binance",
            asset="USDT",
            fiat="PHP",
            buy_ads=[
                p2p_ad("BUY", "Binance", 60.00, 1_000, 100_000, 5_000, "seller 1"),
                p2p_ad("BUY", "Binance", 60.20, 1_000, 100_000, 5_000, "seller 2"),
            ],
            sell_ads=[
                p2p_ad("SELL", "Binance", 60.60, 1_000, 80_000, 5_000, "buyer 1"),
                p2p_ad("SELL", "Binance", 60.50, 1_000, 200_000, 5_000, "buyer 2"),
            ],
            as_of=None,
        )

        sweep = build_depth_sweep(
            [snapshot],
            P2PRouteSettings(capital_php=200_000, min_profit_pct=0.1),
        )

        self.assertIsNotNone(sweep)
        self.assertEqual(len(sweep.buy_lots), 2)
        self.assertGreaterEqual(len(sweep.sell_lots), 1)
        self.assertAlmostEqual(sweep.size_php, 200_000)
        self.assertGreater(sweep.avg_sell_price, sweep.avg_buy_price)
        self.assertGreater(sweep.profit_php, 0)

    def test_realism_buffer_reduces_depth_sweep_profit(self):
        snapshot = P2PSnapshot(
            marketplace="Binance",
            asset="USDT",
            fiat="PHP",
            buy_ads=[p2p_ad("BUY", "Binance", 60.00, 1_000, 100_000, 5_000, "seller")],
            sell_ads=[p2p_ad("SELL", "Binance", 60.30, 1_000, 100_000, 5_000, "buyer")],
            as_of=None,
        )
        sweep = build_depth_sweep(
            [snapshot],
            P2PRouteSettings(capital_php=50_000, min_profit_pct=0.1),
        )

        adjusted = apply_realism_to_sweep(
            sweep,
            P2PRealismSettings(
                settlement_delay_mins=30,
                cancel_rate_pct=3,
                spread_decay_pct=0.05,
            ),
        )

        self.assertLess(adjusted.profit_php, sweep.profit_php)
        self.assertIn("realism drag", "; ".join(adjusted.warnings))

    def test_paper_arb_executes_profitable_sweep_and_persists_balance(self):
        snapshot = P2PSnapshot(
            marketplace="Binance",
            asset="USDT",
            fiat="PHP",
            buy_ads=[p2p_ad("BUY", "Binance", 60.00, 1_000, 100_000, 5_000, "seller")],
            sell_ads=[p2p_ad("SELL", "Binance", 60.30, 1_000, 100_000, 5_000, "buyer")],
            as_of=None,
        )
        sweep = build_depth_sweep(
            [snapshot],
            P2PRouteSettings(capital_php=50_000, min_profit_pct=0.1),
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            paper = P2PPaperArb(path=Path(tmpdir) / "paper.json")
            state, cycle = paper.execute_if_profitable(sweep, min_profit_pct=0.1, starting_php=500_000)
            reloaded = paper.state()
            csv_text = (Path(tmpdir) / "p2p_transaction_history.csv").read_text(encoding="utf-8")

        self.assertIsNotNone(cycle)
        self.assertGreater(state["balance_php"], 500_000)
        self.assertEqual(len(reloaded["cycles"]), 1)
        self.assertEqual(reloaded["transactions"][-1]["type"], "PAPER_CYCLE")
        self.assertGreater(float(reloaded["transactions"][-1]["profit_php"]), 0)
        self.assertIn("PAPER_CYCLE", csv_text)

    def test_paper_arb_reset_archives_previous_transaction_csv(self):
        snapshot = P2PSnapshot(
            marketplace="Binance",
            asset="USDT",
            fiat="PHP",
            buy_ads=[p2p_ad("BUY", "Binance", 60.00, 1_000, 100_000, 5_000, "seller")],
            sell_ads=[p2p_ad("SELL", "Binance", 60.30, 1_000, 100_000, 5_000, "buyer")],
            as_of=None,
        )
        sweep = build_depth_sweep(
            [snapshot],
            P2PRouteSettings(capital_php=50_000, min_profit_pct=0.1),
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            paper = P2PPaperArb(path=Path(tmpdir) / "paper.json")
            paper.execute_if_profitable(sweep, min_profit_pct=0.1, starting_php=500_000)
            paper.reset(500_000)
            current_csv = (Path(tmpdir) / "p2p_transaction_history.csv").read_text(encoding="utf-8")
            archives = list((Path(tmpdir) / "p2p_sessions").glob("reset_*"))
            self.assertEqual(len(archives), 1)
            self.assertTrue((archives[0] / "paper.json").exists())
            self.assertTrue((archives[0] / "p2p_transaction_history.csv").exists())
            self.assertIn("RESET", current_csv)
            self.assertNotIn("PAPER_CYCLE", current_csv)

    def test_paper_cycle_is_blocked_while_hold_is_open(self):
        buy_snapshot = P2PSnapshot(
            marketplace="Binance",
            asset="USDT",
            fiat="PHP",
            buy_ads=[p2p_ad("BUY", "Binance", 60.00, 1_000, 100_000, 5_000, "seller")],
            sell_ads=[p2p_ad("SELL", "Binance", 60.30, 1_000, 100_000, 5_000, "buyer")],
            as_of=None,
        )
        entry = build_p2p_hold_entry(
            [buy_snapshot],
            P2PRouteSettings(capital_php=50_000, min_profit_pct=0.2),
        )
        sweep = build_depth_sweep(
            [buy_snapshot],
            P2PRouteSettings(capital_php=10_000, min_profit_pct=0.1),
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            paper = P2PPaperArb(path=Path(tmpdir) / "paper.json")
            paper.open_hold(entry, target_profit_pct=0.2, starting_php=100_000)
            state, cycle = paper.execute_if_profitable(sweep, min_profit_pct=0.1, starting_php=100_000)

        self.assertIsNone(cycle)
        self.assertEqual(state["transactions"][-1]["type"], "HOLD_BUY")
        self.assertEqual(len(state["cycles"]), 0)

    def test_paper_arb_syncs_starting_capital_down_to_cash(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            paper = P2PPaperArb(path=Path(tmpdir) / "paper.json")
            paper.reset(500_000)
            state, delta = paper.sync_starting_capital(100_000)
            reloaded = paper.state(100_000)

        self.assertAlmostEqual(delta, -400_000)
        self.assertAlmostEqual(state["starting_php"], 100_000)
        self.assertAlmostEqual(state["cash_php"], 100_000)
        self.assertAlmostEqual(state["balance_php"], 100_000)
        self.assertAlmostEqual(reloaded["balance_php"], 100_000)
        self.assertEqual(reloaded["transactions"][-1]["type"], "CAPITAL")
        self.assertAlmostEqual(reloaded["transactions"][-1]["cash_delta_php"], -400_000)

    def test_paper_arb_syncs_starting_capital_down_scales_open_hold(self):
        buy_snapshot = P2PSnapshot(
            marketplace="Binance",
            asset="USDT",
            fiat="PHP",
            buy_ads=[p2p_ad("BUY", "Binance", 60.00, 1_000, 600_000, 20_000, "seller")],
            sell_ads=[],
            as_of=None,
        )
        entry = build_p2p_hold_entry(
            [buy_snapshot],
            P2PRouteSettings(capital_php=500_000, min_profit_pct=0.2),
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            paper = P2PPaperArb(path=Path(tmpdir) / "paper.json")
            state, position = paper.open_hold(entry, target_profit_pct=0.2, starting_php=500_000)
            self.assertIsNotNone(position)
            self.assertAlmostEqual(state["cash_php"], 0)

            state, delta = paper.sync_starting_capital(100_000)
            hold = state["hold_position"]

        self.assertAlmostEqual(delta, -400_000)
        self.assertAlmostEqual(state["cash_php"], 0)
        self.assertAlmostEqual(state["balance_php"], 100_000)
        self.assertAlmostEqual(hold["cost_php"], 100_000)
        self.assertAlmostEqual(hold["usdt"], entry.buy_usdt * 0.2)
        self.assertEqual(state["transactions"][-1]["type"], "CAPITAL")

    def test_hold_buy_waits_then_sells_at_profit_threshold(self):
        buy_snapshot = P2PSnapshot(
            marketplace="Binance",
            asset="USDT",
            fiat="PHP",
            buy_ads=[p2p_ad("BUY", "Binance", 60.00, 1_000, 100_000, 5_000, "seller")],
            sell_ads=[p2p_ad("SELL", "Binance", 60.02, 1_000, 100_000, 5_000, "buyer")],
            as_of=None,
        )
        entry = build_p2p_hold_entry(
            [buy_snapshot],
            P2PRouteSettings(capital_php=50_000, min_profit_pct=0.2),
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            paper = P2PPaperArb(path=Path(tmpdir) / "paper.json")
            state, position = paper.open_hold(entry, target_profit_pct=0.2, starting_php=100_000)
            self.assertIsNotNone(position)
            self.assertAlmostEqual(state["cash_php"], 50_000)

            state, evaluation = paper.evaluate_hold_exit(
                [buy_snapshot],
                P2PRouteSettings(capital_php=50_000, min_profit_pct=0.2),
                starting_php=100_000,
            )
            self.assertIsNotNone(evaluation)
            self.assertFalse(evaluation.exit_ready)
            self.assertIsNotNone(state["hold_position"])

            sell_snapshot = P2PSnapshot(
                marketplace="Binance",
                asset="USDT",
                fiat="PHP",
                buy_ads=[],
                sell_ads=[p2p_ad("SELL", "Binance", 60.30, 1_000, 100_000, 5_000, "buyer")],
                as_of=None,
            )
            state, evaluation = paper.evaluate_hold_exit(
                [sell_snapshot],
                P2PRouteSettings(capital_php=50_000, min_profit_pct=0.2),
                starting_php=100_000,
            )
            reloaded = paper.state(100_000)

        self.assertTrue(evaluation.exit_ready)
        self.assertIsNone(state["hold_position"])
        self.assertGreater(state["cash_php"], 100_000)
        self.assertEqual(len(reloaded["hold_trades"]), 1)
        self.assertIn("HOLD_BUY", [row["type"] for row in reloaded["transactions"]])
        self.assertEqual(reloaded["transactions"][-1]["type"], "HOLD_SELL")

    def test_realism_buffer_can_hold_back_buy_hold_exit(self):
        buy_snapshot = P2PSnapshot(
            marketplace="Binance",
            asset="USDT",
            fiat="PHP",
            buy_ads=[p2p_ad("BUY", "Binance", 60.00, 1_000, 100_000, 5_000, "seller")],
            sell_ads=[],
            as_of=None,
        )
        sell_snapshot = P2PSnapshot(
            marketplace="Binance",
            asset="USDT",
            fiat="PHP",
            buy_ads=[],
            sell_ads=[p2p_ad("SELL", "Binance", 60.20, 1_000, 100_000, 5_000, "buyer")],
            as_of=None,
        )
        entry = build_p2p_hold_entry(
            [buy_snapshot],
            P2PRouteSettings(capital_php=50_000, min_profit_pct=0.2),
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            paper = P2PPaperArb(path=Path(tmpdir) / "paper.json")
            paper.open_hold(entry, target_profit_pct=0.2, starting_php=100_000)
            _, raw_evaluation = paper.evaluate_hold_exit(
                [sell_snapshot],
                P2PRouteSettings(capital_php=50_000, min_profit_pct=0.2),
                starting_php=100_000,
            )
            adjusted = apply_realism_to_hold_evaluation(
                raw_evaluation,
                P2PRealismSettings(spread_decay_pct=0.20, cancel_rate_pct=0, settlement_delay_mins=0),
            )

        self.assertTrue(raw_evaluation.exit_ready)
        self.assertFalse(adjusted.exit_ready)
        self.assertLess(adjusted.profit_php, raw_evaluation.profit_php)

    def test_legacy_paper_state_gets_transaction_history(self):
        legacy = {
            "started_at": "2026-05-10T00:00:00+00:00",
            "starting_php": 100_000.0,
            "balance_php": 101_000.0,
            "cash_php": 101_000.0,
            "realized_profit_php": 1_000.0,
            "cycles": [
                {
                    "timestamp": "2026-05-10T01:00:00+00:00",
                    "size_php": 100_000.0,
                    "profit_php": 1_000.0,
                    "profit_pct": 1.0,
                    "balance_after_php": 101_000.0,
                    "avg_buy_price": 60.0,
                    "avg_sell_price": 60.6,
                    "warnings": ["ok"],
                }
            ],
            "hold_position": None,
            "hold_trades": [],
        }

        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "paper.json"
            path.write_text(json.dumps(legacy), encoding="utf-8")
            paper = P2PPaperArb(path=path)
            state = paper.state(100_000)

        self.assertEqual([row["type"] for row in state["transactions"]], ["RESET", "PAPER_CYCLE"])


if __name__ == "__main__":
    unittest.main()
