"""
test_store.py — unit tests for the SQLite persistence layer.

Uses a temp DB per test (store.db_path honors executor.db_file from config).
No network, no Binance. Run:
    python3 -m pytest executor/test_store.py -v
or
    python3 executor/test_store.py            (unittest)
"""

import json
import sys
import tempfile
import unittest
from pathlib import Path

EXECUTOR_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(EXECUTOR_DIR))
import store  # noqa: E402


def _cfg(tmpdir: Path) -> dict:
    return {"executor": {"db_file": str(tmpdir / "test.db")}}


SAMPLE_ORDER = {
    "executed_at": "2026-07-15T10:00:00+00:00",
    "symbol": "BTCUSDT",
    "side": "BUY",
    "quantity": 0.001,
    "entry_price": 60000.0,
    "sl_price": 58800.0,
    "tp_price": 62400.0,
    "entry_order_id": "123",
    "sl_order_id": "124",
    "tp_order_id": "125",
    "confidence": 0.82,
}


class StoreTest(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.tmp = Path(self._td.name)
        self.cfg = _cfg(self.tmp)

    def tearDown(self):
        self._td.cleanup()

    def test_init_creates_tables(self):
        store.init_db(store.db_path(self.cfg))
        self.assertTrue(store.db_path(self.cfg).exists())
        with store.connect(store.db_path(self.cfg), read_only=True) as c:
            tabs = {r[0] for r in c.execute(
                "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
        self.assertIn("orders", tabs)
        self.assertIn("processed_runs", tabs)
        self.assertIn("meta", tabs)

    def test_state_round_trip(self):
        state = {"processed_run_ids": ["run-1", "run-2"], "trades_today": 3, "trades_date": "2026-07-15"}
        store.save_state(self.cfg, state)
        loaded = store.load_state(self.cfg)
        self.assertEqual(sorted(loaded["processed_run_ids"]), ["run-1", "run-2"])
        self.assertEqual(loaded["trades_today"], 3)
        self.assertEqual(loaded["trades_date"], "2026-07-15")

    def test_load_state_empty_when_no_db(self):
        # load_state creates+inits the DB (executor is the writer). Verify defaults.
        loaded = store.load_state(self.cfg)
        self.assertEqual(loaded["processed_run_ids"], [])
        self.assertEqual(loaded["trades_today"], 0)
        self.assertEqual(loaded["trades_date"], "")

    def test_save_state_idempotent(self):
        state = {"processed_run_ids": ["run-1"], "trades_today": 1, "trades_date": "d"}
        store.save_state(self.cfg, state)
        store.save_state(self.cfg, state)  # re-save same
        with store.connect(store.db_path(self.cfg), read_only=True) as c:
            n = c.execute("SELECT COUNT(*) FROM processed_runs").fetchone()[0]
        self.assertEqual(n, 1)  # not duplicated

    def test_mark_processed_idempotent(self):
        store.mark_processed(self.cfg, "run-x")
        store.mark_processed(self.cfg, "run-x")
        with store.connect(store.db_path(self.cfg), read_only=True) as c:
            n = c.execute("SELECT COUNT(*) FROM processed_runs WHERE run_id='run-x'").fetchone()[0]
        self.assertEqual(n, 1)

    def test_append_and_recent_orders(self):
        store.append_order(self.cfg, SAMPLE_ORDER)
        o2 = dict(SAMPLE_ORDER, entry_order_id="999", executed_at="2026-07-15T11:00:00+00:00")
        store.append_order(self.cfg, o2)
        recent = store.recent_orders(self.cfg, limit=10)
        self.assertEqual(len(recent), 2)
        # newest first
        self.assertEqual(recent[0]["entry_order_id"], "999")
        # payload-merged key preserved
        self.assertEqual(recent[0]["confidence"], 0.82)

    def test_latest_order(self):
        self.assertIsNone(store.latest_order(self.cfg))  # no DB yet -> None
        store.append_order(self.cfg, SAMPLE_ORDER)
        latest = store.latest_order(self.cfg)
        self.assertEqual(latest["symbol"], "BTCUSDT")

    def test_recent_orders_missing_db(self):
        # No DB created yet -> [] without raising.
        self.assertEqual(store.recent_orders(self.cfg, limit=5), [])

    def test_wal_mode(self):
        store.init_db(store.db_path(self.cfg))
        with store.connect(store.db_path(self.cfg), read_only=True) as c:
            mode = c.execute("PRAGMA journal_mode").fetchone()[0]
        self.assertEqual(mode.lower(), "wal")


if __name__ == "__main__":
    unittest.main()
