"""CoinMarketCap data fetcher.

Fetches top coins by market cap and identifies the biggest losers
for the monthly snapshot.
"""

import json
import logging
from datetime import datetime
from pathlib import Path

import requests

from bot import config

logger = logging.getLogger(__name__)

CMC_BASE_URL = "https://pro-api.coinmarketcap.com"


class DataFetcher:
    """Fetches cryptocurrency market data from CoinMarketCap."""

    def __init__(self, api_key: str | None = None):
        self.api_key = api_key or config.CMC_API_KEY
        self.session = requests.Session()
        self.session.headers.update({
            "X-CMC_PRO_API_KEY": self.api_key,
            "Accept": "application/json",
        })

    def get_top_coins(self, limit: int | None = None) -> list[dict]:
        """Fetch top coins by market cap from CoinMarketCap.

        Returns a list of coin dicts with: symbol, name, market_cap,
        price, percent_change_24h, volume_24h.
        """
        limit = limit or config.TOP_N_COINS
        url = f"{CMC_BASE_URL}/v1/cryptocurrency/listings/latest"
        params = {
            "limit": str(limit),
            "convert": "USD",
            "sort": "market_cap",
            "sort_dir": "desc",
        }

        try:
            resp = self.session.get(url, params=params, timeout=30)
            resp.raise_for_status()
            data = resp.json()
        except requests.RequestException as e:
            logger.error("Failed to fetch top coins from CMC: %s", e)
            raise

        coins = []
        for entry in data.get("data", []):
            quote = entry.get("quote", {}).get("USD", {})
            symbol = entry["symbol"]

            # Skip stablecoins
            if symbol in config.STABLECOIN_SYMBOLS:
                continue

            change_24h = quote.get("percent_change_24h")
            volume_24h = quote.get("volume_24h", 0)

            coins.append({
                "symbol": symbol,
                "name": entry["name"],
                "cmc_rank": entry.get("cmc_rank"),
                "market_cap": quote.get("market_cap", 0),
                "price": quote.get("price", 0),
                "percent_change_24h": change_24h if change_24h is not None else 0,
                "volume_24h": volume_24h if volume_24h is not None else 0,
            })

        logger.info("Fetched %d coins from CoinMarketCap (excluding stablecoins)", len(coins))
        return coins

    def get_top_losers(
        self,
        coins: list[dict] | None = None,
        n_losers: int | None = None,
        min_volume: float | None = None,
    ) -> list[dict]:
        """Identify the top N losers from the coin list.

        Args:
            coins: Pre-fetched coin list. If None, fetches fresh data.
            n_losers: Number of losers to return (default from config).
            min_volume: Minimum 24h volume filter (default from config).

        Returns:
            List of top N losers sorted by most negative 24h change.
        """
        if coins is None:
            coins = self.get_top_coins()

        n_losers = n_losers or config.TOP_N_LOSERS
        min_volume = min_volume if min_volume is not None else config.MIN_VOLUME_USD

        # Filter: negative 24h change and meets volume threshold
        losers = [
            c for c in coins
            if c["percent_change_24h"] < 0 and c["volume_24h"] >= min_volume
        ]

        # Sort by 24h change ascending (most negative first)
        losers.sort(key=lambda c: c["percent_change_24h"])

        top_losers = losers[:n_losers]

        logger.info(
            "Top %d losers identified: %s",
            len(top_losers),
            [f"{c['symbol']} ({c['percent_change_24h']:+.2f}%)" for c in top_losers],
        )
        return top_losers

    def save_snapshot(self, losers: list[dict], snapshot_date: datetime | None = None) -> Path:
        """Save a monthly snapshot to disk as JSON."""
        snapshot_date = snapshot_date or datetime.now()
        filename = f"snapshot_{snapshot_date.strftime('%Y_%m')}.json"
        filepath = config.DATA_DIR / filename

        snapshot = {
            "date": snapshot_date.isoformat(),
            "coins": losers,
        }

        filepath.write_text(json.dumps(snapshot, indent=2))
        logger.info("Snapshot saved to %s", filepath)
        return filepath

    def load_snapshot(self, year: int, month: int) -> list[dict] | None:
        """Load a saved monthly snapshot from disk."""
        filename = f"snapshot_{year:04d}_{month:02d}.json"
        filepath = config.DATA_DIR / filename

        if not filepath.exists():
            logger.warning("No snapshot found at %s", filepath)
            return None

        data = json.loads(filepath.read_text())
        logger.info("Loaded snapshot from %s with %d coins", filepath, len(data["coins"]))
        return data["coins"]

    def get_coin_price(self, symbol: str) -> float | None:
        """Fetch current price for a single coin."""
        url = f"{CMC_BASE_URL}/v1/cryptocurrency/quotes/latest"
        params = {"symbol": symbol, "convert": "USD"}

        try:
            resp = self.session.get(url, params=params, timeout=30)
            resp.raise_for_status()
            data = resp.json()
            return data["data"][symbol]["quote"]["USD"]["price"]
        except (requests.RequestException, KeyError) as e:
            logger.error("Failed to fetch price for %s: %s", symbol, e)
            return None
