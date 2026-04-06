"""Tests for the strategy engine."""

import unittest
from datetime import datetime, timezone

from bot.modules.strategy import Strategy


class TestStrategy(unittest.TestCase):
    """Tests for Strategy."""

    def test_should_refresh_basket_when_empty(self):
        """Should refresh when basket is empty."""
        strategy = Strategy()
        self.assertTrue(strategy.should_refresh_basket())

    def test_should_refresh_basket_on_new_month(self):
        """Should refresh when month changes."""
        strategy = Strategy()
        strategy.basket = [{"symbol": "BTC"}]
        strategy.basket_month = 3
        strategy.basket_year = 2026

        april = datetime(2026, 4, 1, tzinfo=timezone.utc)
        self.assertTrue(strategy.should_refresh_basket(april))

    def test_should_not_refresh_same_month(self):
        """Should not refresh within the same month."""
        strategy = Strategy()
        strategy.basket = [{"symbol": "BTC"}]
        strategy.basket_month = 3
        strategy.basket_year = 2026

        mid_march = datetime(2026, 3, 15, tzinfo=timezone.utc)
        self.assertFalse(strategy.should_refresh_basket(mid_march))

    def test_detect_dips_from_prices(self):
        """Should detect dips at -2% threshold."""
        strategy = Strategy()
        strategy.basket = [
            {"symbol": "BTC", "price": 65000},
            {"symbol": "ETH", "price": 3000},
            {"symbol": "SOL", "price": 100},
        ]

        price_data = {
            "BTC": {"price": 63000, "change_pct": -3.1},   # Dipping
            "ETH": {"price": 2970, "change_pct": -1.0},    # Not enough
            "SOL": {"price": 95, "change_pct": -5.0},      # Dipping
        }

        dips = strategy.detect_dips_from_prices(price_data)

        symbols = [d["symbol"] for d in dips]
        self.assertIn("BTC", symbols)
        self.assertIn("SOL", symbols)
        self.assertNotIn("ETH", symbols)

    def test_detect_dips_empty_basket(self):
        """Should return empty list when basket is empty."""
        strategy = Strategy()
        dips = strategy.detect_dips_from_prices({"BTC": {"price": 60000, "change_pct": -5.0}})
        self.assertEqual(dips, [])


if __name__ == "__main__":
    unittest.main()
