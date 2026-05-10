"""Read-only Binance P2P monitor for USDT/PHP spreads."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Iterable

import requests

logger = logging.getLogger(__name__)

BINANCE_P2P_SEARCH_URL = "https://p2p.binance.com/bapi/c2c/v2/friendly/c2c/adv/search"
REQUEST_TIMEOUT = 15


class P2PMonitorError(RuntimeError):
    """Raised when the P2P data source cannot return usable data."""


@dataclass(frozen=True)
class P2PAd:
    """A single P2P advertisement from the user's side of the trade."""

    side: str
    asset: str
    fiat: str
    price: float
    min_limit: float
    max_limit: float
    available: float
    methods: tuple[str, ...]
    advertiser: str
    orders: int | None
    completion_rate: float | None

    @property
    def methods_text(self) -> str:
        return ", ".join(self.methods) if self.methods else "Any"


@dataclass(frozen=True)
class P2PSnapshot:
    """Best available buy/sell ads plus derived spread."""

    asset: str
    fiat: str
    buy_ads: list[P2PAd]
    sell_ads: list[P2PAd]
    as_of: datetime

    @property
    def best_buy(self) -> P2PAd | None:
        return self.buy_ads[0] if self.buy_ads else None

    @property
    def best_sell(self) -> P2PAd | None:
        return self.sell_ads[0] if self.sell_ads else None

    @property
    def spread(self) -> float | None:
        if not self.best_buy or not self.best_sell:
            return None
        return self.best_sell.price - self.best_buy.price

    @property
    def spread_pct(self) -> float | None:
        if self.spread is None or not self.best_buy or self.best_buy.price <= 0:
            return None
        return self.spread / self.best_buy.price * 100


class P2PMonitor:
    """Fetches public Binance P2P ads for one fiat/asset pair."""

    def __init__(
        self,
        asset: str = "USDT",
        fiat: str = "PHP",
        session: requests.Session | None = None,
    ):
        self.asset = asset.upper()
        self.fiat = fiat.upper()
        self.session = session or requests.Session()
        if hasattr(self.session, "headers"):
            self.session.headers.update({
                "Accept": "application/json",
                "Content-Type": "application/json",
                "User-Agent": "TradingBot23/1.0",
            })

    def fetch_snapshot(
        self,
        rows: int = 10,
        pay_types: Iterable[str] | None = None,
    ) -> P2PSnapshot:
        """Fetch buy and sell ads and compute the current raw spread."""

        buy_ads = self.fetch_ads("BUY", rows=rows, pay_types=pay_types)
        sell_ads = self.fetch_ads("SELL", rows=rows, pay_types=pay_types)
        return P2PSnapshot(
            asset=self.asset,
            fiat=self.fiat,
            buy_ads=buy_ads,
            sell_ads=sell_ads,
            as_of=datetime.now(timezone.utc),
        )

    def fetch_ads(
        self,
        side: str,
        rows: int = 10,
        pay_types: Iterable[str] | None = None,
    ) -> list[P2PAd]:
        """Fetch ads for the user's desired side.

        Binance names tradeType from the ad owner's side. Requesting BUY means
        "show sellers I can buy from"; requesting SELL means "show buyers I can
        sell to". This method keeps the side label from the user's perspective.
        """

        side = side.upper()
        if side not in {"BUY", "SELL"}:
            raise ValueError("side must be BUY or SELL")

        payload = {
            "page": 1,
            "rows": max(1, int(rows)),
            "payTypes": list(pay_types or []),
            "asset": self.asset,
            "tradeType": side,
            "fiat": self.fiat,
            "publisherType": None,
            "countries": [],
            "proMerchantAds": False,
            "shieldMerchantAds": False,
            "filterType": "all",
        }

        try:
            response = self.session.post(
                BINANCE_P2P_SEARCH_URL,
                json=payload,
                timeout=REQUEST_TIMEOUT,
            )
            response.raise_for_status()
            body = response.json()
        except requests.RequestException as exc:
            raise P2PMonitorError(f"Binance P2P request failed: {exc}") from exc
        except ValueError as exc:
            raise P2PMonitorError("Binance P2P returned invalid JSON") from exc

        if not isinstance(body, dict):
            raise P2PMonitorError("Binance P2P returned an unexpected response")

        if body.get("code") != "000000":
            message = body.get("message") or body.get("messageDetail") or "unknown error"
            raise P2PMonitorError(f"Binance P2P error: {message}")

        ads = [
            self._parse_ad(entry, side)
            for entry in body.get("data", [])
            if isinstance(entry, dict)
        ]
        ads = [ad for ad in ads if ad.price > 0]
        ads.sort(key=lambda ad: ad.price, reverse=(side == "SELL"))
        return ads

    def _parse_ad(self, entry: dict[str, Any], side: str) -> P2PAd:
        adv = entry.get("adv") or {}
        advertiser = entry.get("advertiser") or {}
        methods = self._parse_methods(adv.get("tradeMethods") or [])
        completion_rate = self._to_float(advertiser.get("monthFinishRate"), default=None)

        return P2PAd(
            side=side,
            asset=(adv.get("asset") or self.asset).upper(),
            fiat=(adv.get("fiatUnit") or self.fiat).upper(),
            price=self._to_float(adv.get("price")),
            min_limit=self._to_float(adv.get("minSingleTransAmount")),
            max_limit=self._to_float(adv.get("maxSingleTransAmount")),
            available=self._to_float(adv.get("tradableQuantity") or adv.get("surplusAmount")),
            methods=methods,
            advertiser=advertiser.get("nickName") or advertiser.get("userNo") or "Unknown",
            orders=self._to_int(advertiser.get("monthOrderCount"), default=None),
            completion_rate=completion_rate,
        )

    @staticmethod
    def _parse_methods(methods: list[dict[str, Any]]) -> tuple[str, ...]:
        names: list[str] = []
        seen: set[str] = set()
        for method in methods:
            name = (
                method.get("tradeMethodName")
                or method.get("identifier")
                or method.get("payType")
                or ""
            )
            name = str(name).strip()
            if name and name not in seen:
                seen.add(name)
                names.append(name)
        return tuple(names)

    @staticmethod
    def _to_float(value: Any, default: float | None = 0.0) -> float | None:
        if value is None or value == "":
            return default
        try:
            return float(str(value).replace(",", ""))
        except (TypeError, ValueError):
            logger.debug("Could not parse float from %r", value)
            return default

    @staticmethod
    def _to_int(value: Any, default: int | None = 0) -> int | None:
        if value is None or value == "":
            return default
        try:
            return int(float(str(value).replace(",", "")))
        except (TypeError, ValueError):
            logger.debug("Could not parse int from %r", value)
            return default
