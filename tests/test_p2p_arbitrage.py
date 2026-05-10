"""Tests for P2P arbitrage route scoring."""

import tempfile
import unittest
from pathlib import Path

from bot.modules.p2p_arbitrage import (
    P2PJournal,
    P2PPaperArb,
    P2PRouteSettings,
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

        self.assertIsNotNone(cycle)
        self.assertGreater(state["balance_php"], 500_000)
        self.assertEqual(len(reloaded["cycles"]), 1)

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


if __name__ == "__main__":
    unittest.main()
