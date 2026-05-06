"""CoinGecko data fetcher (free, no API key required).

Fetches top coins by market cap and identifies the biggest losers
for the monthly snapshot.
"""

import json
import logging
import time
from datetime import datetime
from pathlib import Path

import requests

from bot import config

logger = logging.getLogger(__name__)

COINGECKO_BASE = "https://api.coingecko.com/api/v3"
_REQUEST_TIMEOUT = 30
_RETRY_DELAY = 10  # seconds between retries on rate-limit


class DataFetcher:
    """Fetches cryptocurrency market data from CoinGecko (free tier)."""

    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update({
            "Accept": "application/json",
            "User-Agent": "TradingBot23/1.0",
        })

    def _get(self, url: str, params: dict, retries: int = 3) -> dict:
        for attempt in range(retries):
            try:
                resp = self.session.get(url, params=params, timeout=_REQUEST_TIMEOUT)
                if resp.status_code == 429:
                    wait = int(resp.headers.get("Retry-After", _RETRY_DELAY))
                    logger.warning("CoinGecko rate limited — waiting %ds", wait)
                    time.sleep(wait)
                    continue
                resp.raise_for_status()
                return resp.json()
            except requests.RequestException as e:
                if attempt == retries - 1:
                    raise
                logger.warning("CoinGecko request failed (%s), retrying in %ds…", e, _RETRY_DELAY)
                time.sleep(_RETRY_DELAY)
        return {}

    def get_top_coins(self, limit: int | None = None) -> list[dict]:
        """Fetch top coins by market cap from CoinGecko.

        Returns a list of coin dicts with: symbol, name, market_cap,
        price, percent_change_24h, volume_24h.
        """
        limit = limit or config.TOP_N_COINS
        url = f"{COINGECKO_BASE}/coins/markets"
        params = {
            "vs_currency": "usd",
            "order": "market_cap_desc",
            "per_page": str(limit),
            "page": "1",
            "sparkline": "false",
            "price_change_percentage": "24h",
        }

        try:
            data = self._get(url, params)
        except requests.RequestException as e:
            logger.error("Failed to fetch top coins from CoinGecko: %s", e)
            raise

        coins = []
        for entry in data:
            symbol = entry.get("symbol", "").upper()

            if symbol in config.STABLECOIN_SYMBOLS:
                continue

            change_24h = entry.get("price_change_percentage_24h") or 0.0
            volume_24h = entry.get("total_volume") or 0.0

            if volume_24h < (config.MIN_VOLUME_USD if hasattr(config, "MIN_VOLUME_USD") else 0):
                continue

            coins.append({
                "symbol": symbol,
                "name": entry.get("name", symbol),
                "cmc_rank": entry.get("market_cap_rank"),
                "market_cap": entry.get("market_cap") or 0,
                "price": entry.get("current_price") or 0,
                "percent_change_24h": change_24h,
                "volume_24h": volume_24h,
            })

        logger.info("Fetched %d coins from CoinGecko (excluding stablecoins)", len(coins))
        return coins

    def get_top_losers(
        self,
        coins: list[dict] | None = None,
        n_losers: int | None = None,
        min_volume: float | None = None,
    ) -> list[dict]:
        """Identify the top N losers from the coin list."""
        if coins is None:
            coins = self.get_top_coins()

        n_losers = n_losers or config.TOP_N_LOSERS
        min_volume = min_volume if min_volume is not None else config.MIN_VOLUME_USD

        losers = [
            c for c in coins
            if c["percent_change_24h"] < 0 and c["volume_24h"] >= min_volume
        ]
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

        filepath.write_text(json.dumps(snapshot, indent=2), encoding="utf-8")
        logger.info("Snapshot saved to %s", filepath)
        return filepath

    def load_snapshot(self, year: int, month: int) -> list[dict] | None:
        """Load a saved monthly snapshot from disk."""
        filename = f"snapshot_{year:04d}_{month:02d}.json"
        filepath = config.DATA_DIR / filename

        if not filepath.exists():
            logger.warning("No snapshot found at %s", filepath)
            return None

        data = json.loads(filepath.read_text(encoding="utf-8"))
        logger.info("Loaded snapshot from %s with %d coins", filepath, len(data["coins"]))
        return data["coins"]

    def get_coin_price(self, symbol: str) -> float | None:
        """Fetch current price for a single coin via CoinGecko."""
        url = f"{COINGECKO_BASE}/simple/price"
        params = {"ids": symbol.lower(), "vs_currencies": "usd"}
        try:
            data = self._get(url, params)
            return data.get(symbol.lower(), {}).get("usd")
        except (requests.RequestException, KeyError) as e:
            logger.error("Failed to fetch price for %s: %s", symbol, e)
            return None
