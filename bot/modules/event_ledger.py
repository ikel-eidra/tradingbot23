"""Shared append-only event ledger for TradingBot23."""

from __future__ import annotations

import csv
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from bot import config


FIELDS = [
    "timestamp",
    "domain",
    "event_type",
    "status",
    "amount",
    "currency",
    "pnl",
    "balance",
    "symbol_or_route",
    "details",
]

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


class EventLedger:
    """Small CSV ledger used by futures, P2P, Telegram, and reports."""

    def __init__(self, path: Path | None = None):
        self.path = path or (config.DATA_DIR / "event_ledger.csv")
        self._lock = _file_lock(self.path)

    def append(
        self,
        *,
        domain: str,
        event_type: str,
        status: str = "",
        amount: float = 0.0,
        currency: str = "",
        pnl: float = 0.0,
        balance: float = 0.0,
        symbol_or_route: str = "",
        details: str = "",
        timestamp: str | None = None,
    ) -> Path:
        row = {
            "timestamp": timestamp or datetime.now(timezone.utc).isoformat(),
            "domain": domain,
            "event_type": event_type,
            "status": status,
            "amount": float(amount or 0.0),
            "currency": currency,
            "pnl": float(pnl or 0.0),
            "balance": float(balance or 0.0),
            "symbol_or_route": symbol_or_route,
            "details": details,
        }
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            write_header = not self.path.exists()
            with open(self.path, "a", newline="", encoding="utf-8") as handle:
                writer = csv.DictWriter(handle, fieldnames=FIELDS)
                if write_header:
                    writer.writeheader()
                writer.writerow({field: row.get(field, "") for field in FIELDS})
        return self.path

    def recent(self, limit: int = 100) -> list[dict[str, str]]:
        with self._lock:
            if not self.path.exists():
                return []
            with open(self.path, "r", newline="", encoding="utf-8") as handle:
                rows = list(csv.DictReader(handle))
        return rows[-limit:]


def append_event(**kwargs: Any) -> Path:
    return EventLedger().append(**kwargs)


def recent_events(limit: int = 100) -> list[dict[str, str]]:
    return EventLedger().recent(limit=limit)
