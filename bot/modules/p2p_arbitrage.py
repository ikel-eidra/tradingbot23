"""P2P arbitrage route scoring and manual cycle journal."""

from __future__ import annotations

import csv
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

from bot import config
from bot.modules.p2p_monitor import P2PAd, P2PSnapshot


@dataclass(frozen=True)
class P2PRoute:
    """A buy-side ad paired with a sell-side ad."""

    buy_ad: P2PAd
    sell_ad: P2PAd
    size_php: float
    buy_usdt: float
    sell_usdt: float
    transfer_fee_usdt: float
    sell_php: float
    profit_php: float
    profit_pct: float
    spread_php: float
    spread_pct: float
    grade: str
    warnings: tuple[str, ...]

    @property
    def route_label(self) -> str:
        return f"{self.buy_ad.marketplace} -> {self.sell_ad.marketplace}"

    @property
    def is_cross_exchange(self) -> bool:
        return self.buy_ad.marketplace != self.sell_ad.marketplace

    @property
    def payment_text(self) -> str:
        buy_methods = self.buy_ad.methods_text
        sell_methods = self.sell_ad.methods_text
        return buy_methods if buy_methods == sell_methods else f"{buy_methods} / {sell_methods}"


@dataclass(frozen=True)
class P2PRouteSettings:
    capital_php: float = 500_000.0
    min_profit_php: float = 100.0
    min_profit_pct: float = 0.10
    min_completion_rate: float = 0.97
    min_orders: int = 20
    cross_exchange_transfer_fee_usdt: float = 1.0
    local_buffer_php: float = 0.0
    allowed_methods: tuple[str, ...] = ()


class P2PJournal:
    """Small CSV journal for planned or completed manual P2P cycles."""

    FIELDS = [
        "timestamp",
        "status",
        "route",
        "size_php",
        "expected_profit_php",
        "expected_profit_pct",
        "buy_marketplace",
        "buy_price",
        "buy_advertiser",
        "sell_marketplace",
        "sell_price",
        "sell_advertiser",
        "payment_methods",
        "warnings",
        "notes",
    ]

    def __init__(self, path: Path | None = None):
        self.path = path or (config.DATA_DIR / "p2p_cycle_journal.csv")

    def append(self, route: P2PRoute, status: str = "WATCHLIST", notes: str = "") -> Path:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        write_header = not self.path.exists()
        with open(self.path, "a", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=self.FIELDS)
            if write_header:
                writer.writeheader()
            writer.writerow({
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "status": status,
                "route": route.route_label,
                "size_php": f"{route.size_php:.2f}",
                "expected_profit_php": f"{route.profit_php:.2f}",
                "expected_profit_pct": f"{route.profit_pct:.4f}",
                "buy_marketplace": route.buy_ad.marketplace,
                "buy_price": f"{route.buy_ad.price:.4f}",
                "buy_advertiser": route.buy_ad.advertiser,
                "sell_marketplace": route.sell_ad.marketplace,
                "sell_price": f"{route.sell_ad.price:.4f}",
                "sell_advertiser": route.sell_ad.advertiser,
                "payment_methods": route.payment_text,
                "warnings": "; ".join(route.warnings),
                "notes": notes,
            })
        return self.path

    def recent(self, limit: int = 20) -> list[dict[str, str]]:
        if not self.path.exists():
            return []
        with open(self.path, "r", newline="", encoding="utf-8") as handle:
            rows = list(csv.DictReader(handle))
        return rows[-limit:]


def build_p2p_routes(
    snapshots: Iterable[P2PSnapshot],
    settings: P2PRouteSettings | None = None,
    max_routes: int = 20,
) -> list[P2PRoute]:
    """Build ranked P2P arbitrage routes from available marketplace snapshots."""

    settings = settings or P2PRouteSettings()
    buys: list[P2PAd] = []
    sells: list[P2PAd] = []
    for snapshot in snapshots:
        buys.extend(snapshot.buy_ads)
        sells.extend(snapshot.sell_ads)

    routes: list[P2PRoute] = []
    for buy_ad in buys:
        for sell_ad in sells:
            route = _build_route(buy_ad, sell_ad, settings)
            if route and route.profit_php >= settings.min_profit_php:
                routes.append(route)

    routes.sort(key=lambda r: (r.grade, r.profit_php), reverse=True)
    routes.sort(key=lambda r: r.profit_php, reverse=True)
    return routes[:max_routes]


def _build_route(
    buy_ad: P2PAd,
    sell_ad: P2PAd,
    settings: P2PRouteSettings,
) -> P2PRoute | None:
    if buy_ad.asset != sell_ad.asset or buy_ad.fiat != sell_ad.fiat:
        return None
    if buy_ad.price <= 0 or sell_ad.price <= 0:
        return None

    size_php = _usable_size_php(buy_ad, sell_ad, settings.capital_php)
    if size_php <= 0:
        return None

    fee_usdt = settings.cross_exchange_transfer_fee_usdt
    if buy_ad.marketplace == sell_ad.marketplace:
        fee_usdt = 0.0

    buy_usdt = size_php / buy_ad.price
    sell_usdt = max(0.0, buy_usdt - fee_usdt)
    sell_php = sell_usdt * sell_ad.price
    profit_php = sell_php - size_php - settings.local_buffer_php
    profit_pct = profit_php / size_php * 100 if size_php > 0 else 0.0
    spread_php = sell_ad.price - buy_ad.price
    spread_pct = spread_php / buy_ad.price * 100
    warnings = _warnings(buy_ad, sell_ad, settings, size_php, profit_php, profit_pct)
    grade = _grade_route(profit_pct, warnings)

    return P2PRoute(
        buy_ad=buy_ad,
        sell_ad=sell_ad,
        size_php=size_php,
        buy_usdt=buy_usdt,
        sell_usdt=sell_usdt,
        transfer_fee_usdt=fee_usdt,
        sell_php=sell_php,
        profit_php=profit_php,
        profit_pct=profit_pct,
        spread_php=spread_php,
        spread_pct=spread_pct,
        grade=grade,
        warnings=warnings,
    )


def _usable_size_php(buy_ad: P2PAd, sell_ad: P2PAd, capital_php: float) -> float:
    max_buy_php = min(buy_ad.max_limit, buy_ad.available * buy_ad.price)
    max_sell_php = min(sell_ad.max_limit, sell_ad.available * sell_ad.price)
    size_php = min(capital_php, max_buy_php, max_sell_php)
    required_min = max(buy_ad.min_limit, sell_ad.min_limit)
    return size_php if size_php >= required_min else 0.0


def _warnings(
    buy_ad: P2PAd,
    sell_ad: P2PAd,
    settings: P2PRouteSettings,
    size_php: float,
    profit_php: float,
    profit_pct: float,
) -> tuple[str, ...]:
    warnings: list[str] = []
    if profit_php <= 0:
        warnings.append("no net profit")
    if profit_pct < settings.min_profit_pct:
        warnings.append("below min pct")
    for ad, label in [(buy_ad, "buy"), (sell_ad, "sell")]:
        rate = _completion_pct(ad.completion_rate)
        if rate is not None and rate < settings.min_completion_rate * 100:
            warnings.append(f"low {label} finish")
        if ad.orders is not None and ad.orders < settings.min_orders:
            warnings.append(f"low {label} orders")
        if settings.allowed_methods and not _method_allowed(ad, settings.allowed_methods):
            warnings.append(f"{label} method not allowed")
    if size_php < settings.capital_php * 0.2:
        warnings.append("small capacity")
    return tuple(warnings)


def _completion_pct(rate: float | None) -> float | None:
    if rate is None:
        return None
    return rate * 100 if rate <= 1 else rate


def _method_allowed(ad: P2PAd, allowed_methods: tuple[str, ...]) -> bool:
    allowed = {method.lower() for method in allowed_methods}
    return any(method.lower() in allowed for method in ad.methods)


def _grade_route(profit_pct: float, warnings: tuple[str, ...]) -> str:
    if profit_pct <= 0:
        return "SKIP"
    serious = {"low buy finish", "low sell finish", "buy method not allowed", "sell method not allowed"}
    if any(w in serious for w in warnings):
        return "REVIEW"
    if profit_pct >= 0.35:
        return "A"
    if profit_pct >= 0.20:
        return "B"
    if profit_pct >= 0.10:
        return "C"
    return "WATCH"
