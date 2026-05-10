"""Tests for P2P arbitrage route scoring."""

import tempfile
import unittest
from pathlib import Path

from bot.modules.p2p_arbitrage import P2PJournal, P2PRouteSettings, build_p2p_routes
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


if __name__ == "__main__":
    unittest.main()
