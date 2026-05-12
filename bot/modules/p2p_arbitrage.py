"""P2P arbitrage route scoring and manual cycle journal."""

from __future__ import annotations

import csv
import json
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

from bot import config
from bot.modules.event_ledger import EventLedger
from bot.modules.p2p_monitor import P2PAd, P2PSnapshot

_LOCKS_GUARD = threading.Lock()
_FILE_LOCKS: dict[str, threading.RLock] = {}


def _file_lock(path: Path) -> threading.RLock:
    key = str(path.resolve())
    with _LOCKS_GUARD:
        lock = _FILE_LOCKS.get(key)
        if lock is None:
            lock = threading.RLock()
            _FILE_LOCKS[key] = lock
        return lock


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
class P2PSweepLot:
    """One filled piece of a multi-ad paper P2P cycle."""

    side: str
    marketplace: str
    advertiser: str
    price: float
    php: float
    usdt: float


@dataclass(frozen=True)
class P2PDepthSweep:
    """A multi-ad P2P sweep using weighted average buy and sell prices."""

    size_php: float
    buy_usdt: float
    sell_usdt: float
    sell_php: float
    avg_buy_price: float
    avg_sell_price: float
    transfer_fee_usdt: float
    profit_php: float
    profit_pct: float
    grade: str
    warnings: tuple[str, ...]
    buy_lots: tuple[P2PSweepLot, ...]
    sell_lots: tuple[P2PSweepLot, ...]


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


@dataclass(frozen=True)
class P2PRealismSettings:
    """Paper-only execution buffers for manual P2P settlement friction."""

    settlement_delay_mins: float = 20.0
    cancel_rate_pct: float = 2.0
    spread_decay_pct: float = 0.03


@dataclass(frozen=True)
class P2PPaperCycle:
    timestamp: str
    size_php: float
    profit_php: float
    profit_pct: float
    balance_after_php: float
    avg_buy_price: float
    avg_sell_price: float
    warnings: tuple[str, ...]


@dataclass(frozen=True)
class P2PHoldEntry:
    """A paper buy-only P2P entry that holds USDT inventory."""

    cost_php: float
    buy_usdt: float
    avg_buy_price: float
    warnings: tuple[str, ...]
    buy_lots: tuple[P2PSweepLot, ...]


@dataclass(frozen=True)
class P2PHoldEvaluation:
    """Current sell-side mark for an open paper P2P hold."""

    timestamp: str
    cost_php: float
    usdt: float
    sold_usdt: float
    sell_php: float
    avg_buy_price: float
    avg_sell_price: float
    profit_php: float
    profit_pct: float
    target_profit_pct: float
    exit_ready: bool
    warnings: tuple[str, ...]
    sell_lots: tuple[P2PSweepLot, ...]


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


class P2PPaperArb:
    """Persistent paper balance for simulated P2P arbitrage cycles."""

    MAX_TRANSACTIONS = 1_000
    TRANSACTION_FIELDS = [
        "timestamp",
        "type",
        "status",
        "amount_php",
        "cash_delta_php",
        "usdt",
        "avg_buy_price",
        "avg_sell_price",
        "profit_php",
        "profit_pct",
        "balance_after_php",
        "notes",
    ]

    def __init__(
        self,
        path: Path | None = None,
        transaction_path: Path | None = None,
        event_path: Path | None = None,
    ):
        self.path = path or (config.DATA_DIR / "p2p_paper_arb.json")
        self.transaction_path = (
            transaction_path
            or (
                self.path.with_name("p2p_transaction_history.csv")
                if path
                else config.DATA_DIR / "p2p_transaction_history.csv"
            )
        )
        self.event_ledger = EventLedger(
            event_path
            or (
                self.path.with_name("event_ledger.csv")
                if path
                else config.DATA_DIR / "event_ledger.csv"
            )
        )
        self._state_lock = _file_lock(self.path)
        self._transaction_lock = _file_lock(self.transaction_path)

    def state(self, starting_php: float = 500_000.0) -> dict:
        with self._state_lock:
            if self.path.exists():
                try:
                    return self._normalize_state(
                        json.loads(self.path.read_text(encoding="utf-8")),
                        starting_php,
                    )
                except json.JSONDecodeError:
                    pass
            return self.reset(starting_php)

    def reset(self, starting_php: float = 500_000.0) -> dict:
        now = datetime.now(timezone.utc).isoformat()
        state = {
            "started_at": now,
            "starting_php": float(starting_php),
            "balance_php": float(starting_php),
            "cash_php": float(starting_php),
            "realized_profit_php": 0.0,
            "cycles": [],
            "hold_position": None,
            "hold_trades": [],
            "transactions": [],
        }
        self._append_transaction(
            state,
            kind="RESET",
            status="SET",
            amount_php=float(starting_php),
            cash_delta_php=float(starting_php),
            balance_after_php=float(starting_php),
            notes="Paper account reset",
            timestamp=now,
        )
        self._save(state)
        return state

    def sync_starting_capital(self, starting_php: float) -> tuple[dict, float]:
        """Apply a paper bankroll change without requiring a full reset.

        Cash absorbs the change first. If the bankroll is lowered while a hold
        is open and free cash is not enough, the open paper hold is scaled down
        proportionally so displayed equity follows the configured capital.
        """
        starting_php = float(starting_php)
        if starting_php <= 0:
            raise ValueError("starting PHP capital must be positive")

        state = self.state(starting_php)
        previous = float(state.get("starting_php", starting_php))
        delta = starting_php - previous
        state["starting_php"] = starting_php

        if abs(delta) < 0.01:
            state["balance_php"] = self._equity_at_cost(state)
            self._save(state)
            return state, 0.0

        cash = float(state.get("cash_php", state.get("balance_php", previous)))
        if delta > 0:
            state["cash_php"] = cash + delta
            notes = "Paper capital increased"
        else:
            state["cash_php"] = self._withdraw_equity_from_state(state, -delta, cash)
            notes = "Paper capital decreased"

        state["balance_php"] = self._equity_at_cost(state)
        self._append_transaction(
            state,
            kind="CAPITAL",
            status="ADJUSTED",
            amount_php=abs(delta),
            cash_delta_php=delta,
            balance_after_php=state["balance_php"],
            notes=notes,
        )
        self._save(state)
        return state, delta

    def execute_if_profitable(
        self,
        sweep: P2PDepthSweep,
        min_profit_pct: float,
        starting_php: float = 500_000.0,
    ) -> tuple[dict, P2PPaperCycle | None]:
        state = self.state(starting_php)
        if sweep.size_php <= 0 or sweep.profit_php <= 0 or sweep.profit_pct < min_profit_pct:
            return state, None

        cash_before = float(state.get("cash_php", state.get("balance_php", starting_php)))
        if sweep.size_php > cash_before + 0.01:
            return state, None
        cash_after = cash_before + sweep.profit_php
        cycle = P2PPaperCycle(
            timestamp=datetime.now(timezone.utc).isoformat(),
            size_php=sweep.size_php,
            profit_php=sweep.profit_php,
            profit_pct=sweep.profit_pct,
            balance_after_php=cash_after,
            avg_buy_price=sweep.avg_buy_price,
            avg_sell_price=sweep.avg_sell_price,
            warnings=sweep.warnings,
        )

        cycles = list(state.get("cycles", []))
        cycles.append({
            "timestamp": cycle.timestamp,
            "size_php": cycle.size_php,
            "profit_php": cycle.profit_php,
            "profit_pct": cycle.profit_pct,
            "balance_after_php": cycle.balance_after_php,
            "avg_buy_price": cycle.avg_buy_price,
            "avg_sell_price": cycle.avg_sell_price,
            "warnings": list(cycle.warnings),
        })
        state["cycles"] = cycles[-500:]
        state["cash_php"] = cash_after
        state["realized_profit_php"] = float(state.get("realized_profit_php", 0.0)) + sweep.profit_php
        state["balance_php"] = self._equity_at_cost(state)
        self._append_transaction(
            state,
            kind="PAPER_CYCLE",
            status="FILLED",
            amount_php=sweep.size_php,
            cash_delta_php=sweep.profit_php,
            usdt=sweep.buy_usdt,
            avg_buy_price=sweep.avg_buy_price,
            avg_sell_price=sweep.avg_sell_price,
            profit_php=sweep.profit_php,
            profit_pct=sweep.profit_pct,
            balance_after_php=state["balance_php"],
            notes="; ".join(sweep.warnings),
            timestamp=cycle.timestamp,
        )
        self._save(state)
        return state, cycle

    def open_hold(
        self,
        entry: P2PHoldEntry,
        target_profit_pct: float,
        starting_php: float = 500_000.0,
    ) -> tuple[dict, dict | None]:
        """Open one buy-now/sell-later paper USDT inventory position."""
        state = self.state(starting_php)
        if state.get("hold_position"):
            return state, None
        cash = float(state.get("cash_php", state.get("balance_php", starting_php)))
        if entry.cost_php <= 0 or entry.buy_usdt <= 0 or entry.cost_php > cash + 0.01:
            return state, None

        position = {
            "opened_at": datetime.now(timezone.utc).isoformat(),
            "cost_php": entry.cost_php,
            "usdt": entry.buy_usdt,
            "avg_buy_price": entry.avg_buy_price,
            "target_profit_pct": float(target_profit_pct),
            "warnings": list(entry.warnings),
            "buy_lots": [lot.__dict__ for lot in entry.buy_lots],
        }
        state["cash_php"] = cash - entry.cost_php
        state["hold_position"] = position
        state["balance_php"] = self._equity_at_cost(state)
        self._append_transaction(
            state,
            kind="HOLD_BUY",
            status="OPEN",
            amount_php=entry.cost_php,
            cash_delta_php=-entry.cost_php,
            usdt=entry.buy_usdt,
            avg_buy_price=entry.avg_buy_price,
            balance_after_php=state["balance_php"],
            notes="; ".join(entry.warnings),
            timestamp=position["opened_at"],
        )
        self._save(state)
        return state, position

    def evaluate_hold_exit(
        self,
        snapshots: Iterable[P2PSnapshot],
        settings: P2PRouteSettings | None = None,
        realism: P2PRealismSettings | None = None,
        starting_php: float = 500_000.0,
    ) -> tuple[dict, P2PHoldEvaluation | None]:
        """Mark an open hold and close it when the target profit is reached."""
        settings = settings or P2PRouteSettings()
        state = self.state(starting_php)
        position = state.get("hold_position")
        if not position:
            return state, None

        evaluation = build_p2p_hold_exit(position, snapshots, settings)
        if evaluation is None:
            return state, None
        if realism is not None:
            evaluation = apply_realism_to_hold_evaluation(evaluation, realism)

        position["last_checked_at"] = evaluation.timestamp
        position["last_avg_sell_price"] = evaluation.avg_sell_price
        position["last_profit_php"] = evaluation.profit_php
        position["last_profit_pct"] = evaluation.profit_pct
        position["last_warnings"] = list(evaluation.warnings)

        if evaluation.exit_ready:
            cash = float(state.get("cash_php", 0.0))
            cash_after = cash + evaluation.sell_php - settings.local_buffer_php
            trade = {
                "opened_at": position.get("opened_at"),
                "closed_at": evaluation.timestamp,
                "cost_php": evaluation.cost_php,
                "usdt": evaluation.usdt,
                "sell_php": evaluation.sell_php,
                "profit_php": evaluation.profit_php,
                "profit_pct": evaluation.profit_pct,
                "avg_buy_price": evaluation.avg_buy_price,
                "avg_sell_price": evaluation.avg_sell_price,
                "target_profit_pct": evaluation.target_profit_pct,
                "warnings": list(evaluation.warnings),
            }
            trades = list(state.get("hold_trades", []))
            trades.append(trade)
            state["hold_trades"] = trades[-500:]
            state["hold_position"] = None
            state["cash_php"] = cash_after
            state["realized_profit_php"] = (
                float(state.get("realized_profit_php", 0.0)) + evaluation.profit_php
            )
            self._append_transaction(
                state,
                kind="HOLD_SELL",
                status="CLOSED",
                amount_php=evaluation.sell_php,
                cash_delta_php=evaluation.sell_php - settings.local_buffer_php,
                usdt=evaluation.usdt,
                avg_buy_price=evaluation.avg_buy_price,
                avg_sell_price=evaluation.avg_sell_price,
                profit_php=evaluation.profit_php,
                profit_pct=evaluation.profit_pct,
                balance_after_php=self._equity_at_cost(state),
                notes="; ".join(evaluation.warnings),
                timestamp=evaluation.timestamp,
            )

        state["balance_php"] = self._equity_at_cost(state)
        self._save(state)
        return state, evaluation

    def recent_transactions(self, limit: int = 50, starting_php: float = 500_000.0) -> list[dict]:
        state = self.state(starting_php)
        return list(state.get("transactions", []))[-limit:]

    @staticmethod
    def _equity_at_cost(state: dict) -> float:
        cash = float(state.get("cash_php", state.get("balance_php", 0.0)))
        position = state.get("hold_position") or {}
        return cash + float(position.get("cost_php", 0.0))

    def _withdraw_equity_from_state(self, state: dict, amount_php: float, cash_php: float) -> float:
        if amount_php <= cash_php:
            return cash_php - amount_php

        remainder = amount_php - cash_php
        position = state.get("hold_position")
        if not position:
            return 0.0

        cost_php = float(position.get("cost_php", 0.0))
        if cost_php <= 0:
            state["hold_position"] = None
            return 0.0

        new_cost = max(0.0, cost_php - remainder)
        if new_cost <= 0.01:
            state["hold_position"] = None
            return 0.0

        self._scale_hold_position(position, new_cost / cost_php)
        return 0.0

    @staticmethod
    def _scale_hold_position(position: dict, ratio: float) -> None:
        position["cost_php"] = float(position.get("cost_php", 0.0)) * ratio
        position["usdt"] = float(position.get("usdt", 0.0)) * ratio
        for lot in position.get("buy_lots", []):
            lot["php"] = float(lot.get("php", 0.0)) * ratio
            lot["usdt"] = float(lot.get("usdt", 0.0)) * ratio
        for stale_key in (
            "last_checked_at",
            "last_avg_sell_price",
            "last_profit_php",
            "last_profit_pct",
            "last_warnings",
        ):
            position.pop(stale_key, None)

    def _normalize_state(self, state: dict, starting_php: float) -> dict:
        state.setdefault("started_at", datetime.now(timezone.utc).isoformat())
        state.setdefault("starting_php", float(starting_php))
        if "cash_php" not in state:
            state["cash_php"] = float(state.get("balance_php", starting_php))
        state.setdefault("realized_profit_php", 0.0)
        state.setdefault("cycles", [])
        state.setdefault("hold_position", None)
        state.setdefault("hold_trades", [])
        if "transactions" not in state:
            state["transactions"] = self._legacy_transactions(state)
        state["balance_php"] = self._equity_at_cost(state)
        return state

    def _save(self, state: dict) -> None:
        with self._state_lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(self.path.suffix + ".tmp")
            tmp.write_text(json.dumps(state, indent=2), encoding="utf-8")
            tmp.replace(self.path)

    def _append_transaction(
        self,
        state: dict,
        *,
        kind: str,
        status: str,
        amount_php: float = 0.0,
        cash_delta_php: float = 0.0,
        usdt: float = 0.0,
        avg_buy_price: float = 0.0,
        avg_sell_price: float = 0.0,
        profit_php: float = 0.0,
        profit_pct: float = 0.0,
        balance_after_php: float | None = None,
        notes: str = "",
        timestamp: str | None = None,
    ) -> None:
        rows = list(state.get("transactions", []))
        row = {
            "timestamp": timestamp or datetime.now(timezone.utc).isoformat(),
            "type": kind,
            "status": status,
            "amount_php": float(amount_php),
            "cash_delta_php": float(cash_delta_php),
            "usdt": float(usdt),
            "avg_buy_price": float(avg_buy_price),
            "avg_sell_price": float(avg_sell_price),
            "profit_php": float(profit_php),
            "profit_pct": float(profit_pct),
            "balance_after_php": float(
                self._equity_at_cost(state) if balance_after_php is None else balance_after_php
            ),
            "notes": notes,
        }
        rows.append(row)
        state["transactions"] = rows[-self.MAX_TRANSACTIONS:]
        self._append_transaction_csv(row)
        self._append_event_ledger(row)

    def _append_transaction_csv(self, row: dict) -> None:
        with self._transaction_lock:
            self.transaction_path.parent.mkdir(parents=True, exist_ok=True)
            write_header = not self.transaction_path.exists()
            with open(self.transaction_path, "a", newline="", encoding="utf-8") as handle:
                writer = csv.DictWriter(handle, fieldnames=self.TRANSACTION_FIELDS)
                if write_header:
                    writer.writeheader()
                writer.writerow({
                    field: row.get(field, "")
                    for field in self.TRANSACTION_FIELDS
                })

    def _append_event_ledger(self, row: dict) -> None:
        try:
            self.event_ledger.append(
                domain="p2p",
                event_type=str(row.get("type", "")),
                status=str(row.get("status", "")),
                amount=float(row.get("amount_php", 0.0) or 0.0),
                currency="PHP",
                pnl=float(row.get("profit_php", 0.0) or 0.0),
                balance=float(row.get("balance_after_php", 0.0) or 0.0),
                symbol_or_route="USDT/PHP",
                details=str(row.get("notes", "")),
                timestamp=str(row.get("timestamp", "")) or None,
            )
        except Exception:
            pass

    def _legacy_transactions(self, state: dict) -> list[dict]:
        rows: list[dict] = []
        start = float(state.get("starting_php", 0.0))
        rows.append({
            "timestamp": state.get("started_at", datetime.now(timezone.utc).isoformat()),
            "type": "RESET",
            "status": "SET",
            "amount_php": start,
            "cash_delta_php": start,
            "usdt": 0.0,
            "avg_buy_price": 0.0,
            "avg_sell_price": 0.0,
            "profit_php": 0.0,
            "profit_pct": 0.0,
            "balance_after_php": start,
            "notes": "Imported from existing paper account",
        })

        for cycle in state.get("cycles", []):
            rows.append({
                "timestamp": cycle.get("timestamp", ""),
                "type": "PAPER_CYCLE",
                "status": "FILLED",
                "amount_php": float(cycle.get("size_php", 0.0)),
                "cash_delta_php": float(cycle.get("profit_php", 0.0)),
                "usdt": 0.0,
                "avg_buy_price": float(cycle.get("avg_buy_price", 0.0)),
                "avg_sell_price": float(cycle.get("avg_sell_price", 0.0)),
                "profit_php": float(cycle.get("profit_php", 0.0)),
                "profit_pct": float(cycle.get("profit_pct", 0.0)),
                "balance_after_php": float(cycle.get("balance_after_php", 0.0)),
                "notes": "; ".join(cycle.get("warnings", [])),
            })

        for trade in state.get("hold_trades", []):
            rows.append({
                "timestamp": trade.get("opened_at", ""),
                "type": "HOLD_BUY",
                "status": "OPEN",
                "amount_php": float(trade.get("cost_php", 0.0)),
                "cash_delta_php": -float(trade.get("cost_php", 0.0)),
                "usdt": float(trade.get("usdt", 0.0)),
                "avg_buy_price": float(trade.get("avg_buy_price", 0.0)),
                "avg_sell_price": 0.0,
                "profit_php": 0.0,
                "profit_pct": 0.0,
                "balance_after_php": 0.0,
                "notes": "Imported closed hold entry",
            })
            rows.append({
                "timestamp": trade.get("closed_at", ""),
                "type": "HOLD_SELL",
                "status": "CLOSED",
                "amount_php": float(trade.get("sell_php", 0.0)),
                "cash_delta_php": float(trade.get("sell_php", 0.0)),
                "usdt": float(trade.get("usdt", 0.0)),
                "avg_buy_price": float(trade.get("avg_buy_price", 0.0)),
                "avg_sell_price": float(trade.get("avg_sell_price", 0.0)),
                "profit_php": float(trade.get("profit_php", 0.0)),
                "profit_pct": float(trade.get("profit_pct", 0.0)),
                "balance_after_php": 0.0,
                "notes": "; ".join(trade.get("warnings", [])),
            })

        hold = state.get("hold_position") or {}
        if hold:
            rows.append({
                "timestamp": hold.get("opened_at", ""),
                "type": "HOLD_BUY",
                "status": "OPEN",
                "amount_php": float(hold.get("cost_php", 0.0)),
                "cash_delta_php": -float(hold.get("cost_php", 0.0)),
                "usdt": float(hold.get("usdt", 0.0)),
                "avg_buy_price": float(hold.get("avg_buy_price", 0.0)),
                "avg_sell_price": 0.0,
                "profit_php": float(hold.get("last_profit_php", 0.0) or 0.0),
                "profit_pct": float(hold.get("last_profit_pct", 0.0) or 0.0),
                "balance_after_php": self._equity_at_cost(state),
                "notes": "; ".join(hold.get("warnings", [])),
            })

        rows.sort(key=lambda row: row.get("timestamp", ""))
        return rows[-self.MAX_TRANSACTIONS:]


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


def build_depth_sweep(
    snapshots: Iterable[P2PSnapshot],
    settings: P2PRouteSettings | None = None,
) -> P2PDepthSweep | None:
    """Build a 500k-style sweep across multiple real P2P ads."""

    settings = settings or P2PRouteSettings()
    buy_ads: list[P2PAd] = []
    sell_ads: list[P2PAd] = []
    for snapshot in snapshots:
        buy_ads.extend(snapshot.buy_ads)
        sell_ads.extend(snapshot.sell_ads)

    buy_ads.sort(key=lambda ad: ad.price)
    sell_ads.sort(key=lambda ad: ad.price, reverse=True)

    buy_lots, spent_php, buy_usdt = _sweep_buy_side(buy_ads, settings.capital_php)
    if spent_php <= 0 or buy_usdt <= 0:
        return None

    buy_marketplaces = {lot.marketplace for lot in buy_lots}
    sell_lots, sold_usdt, sold_php = _sweep_sell_side(sell_ads, buy_usdt)
    sell_marketplaces = {lot.marketplace for lot in sell_lots}
    fee_usdt = 0.0
    if sell_marketplaces and (buy_marketplaces != sell_marketplaces or len(buy_marketplaces) > 1):
        fee_usdt = settings.cross_exchange_transfer_fee_usdt
        sell_lots, sold_usdt, sold_php = _sweep_sell_side(
            sell_ads,
            max(0.0, buy_usdt - fee_usdt),
        )
    avg_buy = spent_php / buy_usdt if buy_usdt > 0 else 0.0
    avg_sell = sold_php / sold_usdt if sold_usdt > 0 else 0.0
    profit_php = sold_php - spent_php - settings.local_buffer_php
    profit_pct = profit_php / spent_php * 100 if spent_php > 0 else 0.0

    warnings = _sweep_warnings(
        settings=settings,
        buy_lots=buy_lots,
        sell_lots=sell_lots,
        spent_php=spent_php,
        buy_usdt=buy_usdt,
        sold_usdt=sold_usdt,
        profit_php=profit_php,
        profit_pct=profit_pct,
    )
    grade = _grade_route(profit_pct, warnings)

    return P2PDepthSweep(
        size_php=spent_php,
        buy_usdt=buy_usdt,
        sell_usdt=sold_usdt,
        sell_php=sold_php,
        avg_buy_price=avg_buy,
        avg_sell_price=avg_sell,
        transfer_fee_usdt=fee_usdt,
        profit_php=profit_php,
        profit_pct=profit_pct,
        grade=grade,
        warnings=warnings,
        buy_lots=tuple(buy_lots),
        sell_lots=tuple(sell_lots),
    )


def apply_realism_to_sweep(
    sweep: P2PDepthSweep,
    realism: P2PRealismSettings | None = None,
) -> P2PDepthSweep:
    """Apply deterministic paper buffers for manual P2P settlement friction."""

    realism = realism or P2PRealismSettings()
    delay_mins = max(0.0, float(realism.settlement_delay_mins))
    cancel_rate = max(0.0, float(realism.cancel_rate_pct))
    spread_decay = max(0.0, float(realism.spread_decay_pct))

    spread_decay_php = sweep.size_php * (spread_decay / 100)
    cancel_drag_php = sweep.size_php * (cancel_rate / 100) * 0.0005
    delay_drag_php = sweep.size_php * min(delay_mins / 1440, 1.0) * 0.0002
    total_drag_php = spread_decay_php + cancel_drag_php + delay_drag_php

    profit_php = sweep.profit_php - total_drag_php
    profit_pct = profit_php / sweep.size_php * 100 if sweep.size_php > 0 else 0.0
    warnings = list(sweep.warnings)
    if total_drag_php > 0:
        warnings.append(f"realism drag {total_drag_php:.0f} PHP")
    if delay_mins > 0:
        warnings.append(f"settlement {delay_mins:.0f}m")
    if cancel_rate > 0:
        warnings.append(f"cancel risk {cancel_rate:.1f}%")

    return P2PDepthSweep(
        size_php=sweep.size_php,
        buy_usdt=sweep.buy_usdt,
        sell_usdt=sweep.sell_usdt,
        sell_php=sweep.sell_php,
        avg_buy_price=sweep.avg_buy_price,
        avg_sell_price=sweep.avg_sell_price,
        transfer_fee_usdt=sweep.transfer_fee_usdt,
        profit_php=profit_php,
        profit_pct=profit_pct,
        grade=_grade_route(profit_pct, tuple(warnings)),
        warnings=tuple(warnings),
        buy_lots=sweep.buy_lots,
        sell_lots=sweep.sell_lots,
    )


def apply_realism_to_hold_evaluation(
    evaluation: P2PHoldEvaluation,
    realism: P2PRealismSettings | None = None,
) -> P2PHoldEvaluation:
    """Apply paper settlement buffers to a buy-hold sell evaluation."""

    realism = realism or P2PRealismSettings()
    delay_mins = max(0.0, float(realism.settlement_delay_mins))
    cancel_rate = max(0.0, float(realism.cancel_rate_pct))
    spread_decay = max(0.0, float(realism.spread_decay_pct))
    spread_decay_php = evaluation.cost_php * (spread_decay / 100)
    cancel_drag_php = evaluation.cost_php * (cancel_rate / 100) * 0.0005
    delay_drag_php = evaluation.cost_php * min(delay_mins / 1440, 1.0) * 0.0002
    total_drag_php = spread_decay_php + cancel_drag_php + delay_drag_php

    adjusted_sell_php = max(0.0, evaluation.sell_php - total_drag_php)
    profit_php = evaluation.profit_php - total_drag_php
    profit_pct = profit_php / evaluation.cost_php * 100 if evaluation.cost_php > 0 else 0.0
    warnings = list(evaluation.warnings)
    if total_drag_php > 0:
        warnings.append(f"realism drag {total_drag_php:.0f} PHP")
    if delay_mins > 0:
        warnings.append(f"settlement {delay_mins:.0f}m")
    if cancel_rate > 0:
        warnings.append(f"cancel risk {cancel_rate:.1f}%")
    exit_ready = (
        evaluation.sold_usdt >= evaluation.usdt * 0.99
        and profit_pct >= evaluation.target_profit_pct
    )
    avg_sell = adjusted_sell_php / evaluation.sold_usdt if evaluation.sold_usdt > 0 else 0.0

    return P2PHoldEvaluation(
        timestamp=evaluation.timestamp,
        cost_php=evaluation.cost_php,
        usdt=evaluation.usdt,
        sold_usdt=evaluation.sold_usdt,
        sell_php=adjusted_sell_php,
        avg_buy_price=evaluation.avg_buy_price,
        avg_sell_price=avg_sell,
        profit_php=profit_php,
        profit_pct=profit_pct,
        target_profit_pct=evaluation.target_profit_pct,
        exit_ready=exit_ready,
        warnings=tuple(warnings),
        sell_lots=evaluation.sell_lots,
    )


def build_p2p_hold_entry(
    snapshots: Iterable[P2PSnapshot],
    settings: P2PRouteSettings | None = None,
) -> P2PHoldEntry | None:
    """Build a buy-only paper entry using the cheapest available P2P sell ads."""

    settings = settings or P2PRouteSettings()
    buy_ads: list[P2PAd] = []
    for snapshot in snapshots:
        buy_ads.extend(snapshot.buy_ads)
    buy_ads.sort(key=lambda ad: ad.price)

    buy_lots, spent_php, buy_usdt = _sweep_buy_side(buy_ads, settings.capital_php)
    if spent_php <= 0 or buy_usdt <= 0:
        return None
    avg_buy = spent_php / buy_usdt
    warnings: list[str] = []
    if spent_php < settings.capital_php * 0.95:
        warnings.append("partial buy fill")
    if len(buy_lots) > 1:
        warnings.append(f"{len(buy_lots)} buy ads")
    for lot in buy_lots:
        ad = next((ad for ad in buy_ads if ad.advertiser == lot.advertiser and ad.price == lot.price), None)
        if ad is not None:
            rate = _completion_pct(ad.completion_rate)
            if rate is not None and rate < settings.min_completion_rate * 100:
                warnings.append("low buy finish")
                break
    return P2PHoldEntry(
        cost_php=spent_php,
        buy_usdt=buy_usdt,
        avg_buy_price=avg_buy,
        warnings=tuple(warnings),
        buy_lots=tuple(buy_lots),
    )


def build_p2p_hold_exit(
    position: dict,
    snapshots: Iterable[P2PSnapshot],
    settings: P2PRouteSettings | None = None,
) -> P2PHoldEvaluation | None:
    """Build a sell-side mark for an open paper P2P hold position."""

    settings = settings or P2PRouteSettings()
    cost_php = float(position.get("cost_php", 0.0))
    usdt = float(position.get("usdt", 0.0))
    if cost_php <= 0 or usdt <= 0:
        return None

    sell_ads: list[P2PAd] = []
    for snapshot in snapshots:
        sell_ads.extend(snapshot.sell_ads)
    sell_ads.sort(key=lambda ad: ad.price, reverse=True)
    sell_lots, sold_usdt, sold_php = _sweep_sell_side(sell_ads, usdt)
    avg_sell = sold_php / sold_usdt if sold_usdt > 0 else 0.0
    avg_buy = float(position.get("avg_buy_price", cost_php / usdt))
    profit_php = sold_php - cost_php - settings.local_buffer_php
    profit_pct = profit_php / cost_php * 100 if cost_php > 0 else 0.0
    target_profit_pct = float(position.get("target_profit_pct", settings.min_profit_pct))
    warnings: list[str] = []
    if sold_usdt < usdt * 0.99:
        warnings.append("partial sell fill")
    if profit_php <= 0:
        warnings.append("no net profit")
    if profit_pct < target_profit_pct:
        warnings.append("below target")
    if len(sell_lots) > 1:
        warnings.append(f"{len(sell_lots)} sell ads")
    exit_ready = sold_usdt >= usdt * 0.99 and profit_pct >= target_profit_pct

    return P2PHoldEvaluation(
        timestamp=datetime.now(timezone.utc).isoformat(),
        cost_php=cost_php,
        usdt=usdt,
        sold_usdt=sold_usdt,
        sell_php=sold_php,
        avg_buy_price=avg_buy,
        avg_sell_price=avg_sell,
        profit_php=profit_php,
        profit_pct=profit_pct,
        target_profit_pct=target_profit_pct,
        exit_ready=exit_ready,
        warnings=tuple(warnings),
        sell_lots=tuple(sell_lots),
    )


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


def _sweep_buy_side(buy_ads: list[P2PAd], target_php: float) -> tuple[list[P2PSweepLot], float, float]:
    lots: list[P2PSweepLot] = []
    remaining_php = max(0.0, target_php)
    spent_php = 0.0
    buy_usdt = 0.0

    for ad in buy_ads:
        if remaining_php <= 0:
            break
        capacity_php = min(ad.max_limit, ad.available * ad.price)
        if capacity_php < ad.min_limit:
            continue
        spend_php = min(remaining_php, capacity_php)
        if spend_php < ad.min_limit:
            continue
        usdt = spend_php / ad.price
        lots.append(P2PSweepLot("BUY", ad.marketplace, ad.advertiser, ad.price, spend_php, usdt))
        spent_php += spend_php
        buy_usdt += usdt
        remaining_php -= spend_php

    return lots, spent_php, buy_usdt


def _sweep_sell_side(sell_ads: list[P2PAd], target_usdt: float) -> tuple[list[P2PSweepLot], float, float]:
    lots: list[P2PSweepLot] = []
    remaining_usdt = max(0.0, target_usdt)
    sold_usdt = 0.0
    sold_php = 0.0

    for ad in sell_ads:
        if remaining_usdt <= 0:
            break
        capacity_php = min(ad.max_limit, ad.available * ad.price)
        if capacity_php < ad.min_limit:
            continue
        capacity_usdt = capacity_php / ad.price
        lot_usdt = min(remaining_usdt, capacity_usdt)
        lot_php = lot_usdt * ad.price
        if lot_php < ad.min_limit:
            continue
        lots.append(P2PSweepLot("SELL", ad.marketplace, ad.advertiser, ad.price, lot_php, lot_usdt))
        sold_usdt += lot_usdt
        sold_php += lot_php
        remaining_usdt -= lot_usdt

    return lots, sold_usdt, sold_php


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


def _sweep_warnings(
    settings: P2PRouteSettings,
    buy_lots: list[P2PSweepLot],
    sell_lots: list[P2PSweepLot],
    spent_php: float,
    buy_usdt: float,
    sold_usdt: float,
    profit_php: float,
    profit_pct: float,
) -> tuple[str, ...]:
    warnings: list[str] = []
    if profit_php <= 0:
        warnings.append("no net profit")
    if profit_pct < settings.min_profit_pct:
        warnings.append("below min pct")
    if spent_php < settings.capital_php * 0.95:
        warnings.append("partial buy fill")
    if buy_usdt > 0 and sold_usdt < buy_usdt * 0.95:
        warnings.append("partial sell fill")
    if len(buy_lots) > 1:
        warnings.append(f"{len(buy_lots)} buy ads")
    if len(sell_lots) > 1:
        warnings.append(f"{len(sell_lots)} sell ads")
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
