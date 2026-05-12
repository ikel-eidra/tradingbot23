"""Tests for the shared event ledger."""

import tempfile
import unittest
from pathlib import Path

from bot.modules.event_ledger import EventLedger


class TestEventLedger(unittest.TestCase):
    def test_append_and_read_recent_events(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            ledger = EventLedger(Path(tmpdir) / "events.csv")
            ledger.append(
                domain="futures",
                event_type="OPEN",
                status="OPEN",
                amount=100,
                currency="USD",
                pnl=0,
                balance=5000,
                symbol_or_route="BTC",
                details="test",
            )
            rows = ledger.recent()

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["domain"], "futures")
        self.assertEqual(rows[0]["event_type"], "OPEN")
        self.assertEqual(rows[0]["symbol_or_route"], "BTC")


if __name__ == "__main__":
    unittest.main()
