"""Tests for the CoinGecko data fetcher."""

import unittest

from bot.modules.data_fetcher import DataFetcher


class TestDataFetcher(unittest.TestCase):
    """Tests for DataFetcher."""

    def setUp(self):
        self.fetcher = DataFetcher()

    def test_get_top_losers_sorts_by_change(self):
        """Top losers should be sorted most negative first."""
        coins = [
            {"symbol": "BTC", "name": "Bitcoin", "market_cap": 1e12, "price": 65000,
             "percent_change_24h": -1.5, "volume_24h": 1e9},
            {"symbol": "ETH", "name": "Ethereum", "market_cap": 3e11, "price": 3000,
             "percent_change_24h": -4.2, "volume_24h": 5e8},
            {"symbol": "SOL", "name": "Solana", "market_cap": 5e10, "price": 100,
             "percent_change_24h": -2.8, "volume_24h": 2e8},
            {"symbol": "ADA", "name": "Cardano", "market_cap": 1e10, "price": 0.25,
             "percent_change_24h": 1.5, "volume_24h": 1e8},  # Positive — excluded
        ]

        losers = self.fetcher.get_top_losers(coins, n_losers=3, min_volume=0)

        self.assertEqual(len(losers), 3)
        self.assertEqual(losers[0]["symbol"], "ETH")  # -4.2% (most negative)
        self.assertEqual(losers[1]["symbol"], "SOL")  # -2.8%
        self.assertEqual(losers[2]["symbol"], "BTC")  # -1.5%

    def test_get_top_losers_respects_volume_filter(self):
        """Low-volume coins should be excluded."""
        coins = [
            {"symbol": "BTC", "name": "Bitcoin", "market_cap": 1e12, "price": 65000,
             "percent_change_24h": -3.0, "volume_24h": 1e9},
            {"symbol": "SMALL", "name": "SmallCoin", "market_cap": 1e8, "price": 0.01,
             "percent_change_24h": -5.0, "volume_24h": 1e6},  # Low volume
        ]

        losers = self.fetcher.get_top_losers(coins, n_losers=10, min_volume=50_000_000)

        self.assertEqual(len(losers), 1)
        self.assertEqual(losers[0]["symbol"], "BTC")

    def test_get_top_losers_limits_count(self):
        """Should only return n_losers coins."""
        coins = [
            {"symbol": f"COIN{i}", "name": f"Coin{i}", "market_cap": 1e10,
             "price": 100, "percent_change_24h": -float(i), "volume_24h": 1e9}
            for i in range(1, 21)
        ]

        losers = self.fetcher.get_top_losers(coins, n_losers=5, min_volume=0)
        self.assertEqual(len(losers), 5)

    def test_get_top_losers_excludes_positive(self):
        """Coins with positive 24h change should be excluded."""
        coins = [
            {"symbol": "UP", "name": "UpCoin", "market_cap": 1e10, "price": 100,
             "percent_change_24h": 5.0, "volume_24h": 1e9},
        ]

        losers = self.fetcher.get_top_losers(coins, n_losers=10, min_volume=0)
        self.assertEqual(len(losers), 0)


if __name__ == "__main__":
    unittest.main()
