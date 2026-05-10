"""Tests for the Binance P2P monitor parser."""

import unittest

from bot.modules.p2p_monitor import P2PMonitor, P2PMonitorError


class FakeResponse:
    def __init__(self, payload, status_code=200):
        self.payload = payload
        self.status_code = status_code

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self):
        return self.payload


class FakeSession:
    def __init__(self, payloads):
        self.payloads = payloads
        self.calls = []
        self.headers = {}

    def post(self, url, json, timeout):
        self.calls.append({"url": url, "json": json, "timeout": timeout})
        return FakeResponse(self.payloads[json["tradeType"]])


def ad(price, min_amount, max_amount, available, name, rate, methods=None, orders=25):
    return {
        "adv": {
            "asset": "USDT",
            "fiatUnit": "PHP",
            "price": str(price),
            "minSingleTransAmount": str(min_amount),
            "maxSingleTransAmount": str(max_amount),
            "tradableQuantity": str(available),
            "tradeMethods": [
                {"tradeMethodName": method}
                for method in (methods or ["GCash", "Bank Transfer"])
            ],
        },
        "advertiser": {
            "nickName": name,
            "monthFinishRate": str(rate),
            "monthOrderCount": str(orders),
        },
    }


class TestP2PMonitor(unittest.TestCase):
    def test_fetch_snapshot_sorts_ads_and_computes_spread(self):
        session = FakeSession({
            "BUY": {
                "code": "000000",
                "data": [
                    ad(60.80, 1000, 5000, 1200, "Seller B", 0.97),
                    ad(60.20, 500, 10000, 5000, "Seller A", 0.99, ["Maya"]),
                ],
            },
            "SELL": {
                "code": "000000",
                "data": [
                    ad(60.10, 1000, 8000, 2000, "Buyer B", 0.95),
                    ad(60.55, 500, 15000, 3500, "Buyer A", 0.98),
                ],
            },
        })

        snapshot = P2PMonitor(session=session).fetch_snapshot(rows=2)

        self.assertEqual(snapshot.best_buy.price, 60.20)
        self.assertEqual(snapshot.best_buy.advertiser, "Seller A")
        self.assertEqual(snapshot.best_buy.methods, ("Maya",))
        self.assertEqual(snapshot.best_sell.price, 60.55)
        self.assertAlmostEqual(snapshot.spread, 0.35)
        self.assertAlmostEqual(snapshot.spread_pct, 0.581395, places=5)
        self.assertEqual(session.calls[0]["json"]["tradeType"], "BUY")
        self.assertEqual(session.calls[1]["json"]["tradeType"], "SELL")
        self.assertEqual(session.calls[0]["json"]["asset"], "USDT")
        self.assertEqual(session.calls[0]["json"]["fiat"], "PHP")

    def test_non_success_binance_code_raises_monitor_error(self):
        session = FakeSession({
            "BUY": {"code": "100001", "message": "bad request", "data": []},
        })

        with self.assertRaises(P2PMonitorError):
            P2PMonitor(session=session).fetch_ads("BUY")

    def test_rejects_invalid_side(self):
        with self.assertRaises(ValueError):
            P2PMonitor(session=FakeSession({})).fetch_ads("HOLD")


if __name__ == "__main__":
    unittest.main()
