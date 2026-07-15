"""
store.py — SQLite persistence layer for the executor.

Replaces runtime/state.json + runtime/orders_history.jsonl with a single
runtime/bot.db. The order path, risk_guard, and kill switch are untouched —
this is a persistence-only layer.

Concurrency:
  - Executor writes (RW). Dashboard reads (RO, mode=ro URI).
  - WAL journal mode so readers never block the writer.
  - Connections are short-lived (open → use → close) to avoid stale WAL locks
    across long-lived processes; init_db() is idempotent.

Schema:
  orders(id, executed_at, symbol, side, quantity, entry_price, sl_price,
         tp_price, entry_order_id, sl_order_id, tp_order_id, payload)
  processed_runs(run_id PK, ts)
  meta(k PK, trades_today, trades_date)         -- single row k='executor'
"""

from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

EXECUTOR_DIR = Path(__file__).resolve().parent
REPO_ROOT = EXECUTOR_DIR.parent
META_KEY = "executor"

DEFAULT_DB_FILE = "runtime/bot.db"


# =====================================================================
# Path / connection
# =====================================================================
def db_path(config: Optional[Dict[str, Any]] = None) -> Path:
    """Resolve DB path from config (executor.db_file), default runtime/bot.db."""
    cfg = config or {}
    raw = cfg.get("executor", {}).get("db_file", DEFAULT_DB_FILE)
    p = Path(raw)
    if not p.is_absolute():
        p = REPO_ROOT / raw
    return p


def connect(path: Path, read_only: bool = False) -> sqlite3.Connection:
    """Open a connection. read_only uses the SQLite RO URI (fails if file absent
    AFTER init). WAL set on every open for writers; harmless on RO."""
    path.parent.mkdir(parents=True, exist_ok=True)
    if read_only:
        uri = f"file:{path.as_posix()}?mode=ro"
        conn = sqlite3.connect(uri, uri=True, timeout=15)
    else:
        conn = sqlite3.connect(str(path), timeout=15)
        conn.execute("PRAGMA journal_mode=WAL;")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON;")
    conn.execute("PRAGMA busy_timeout=15000;")
    return conn


def init_db(path: Path) -> None:
    """Create tables/indexes + seed meta row. Idempotent."""
    with connect(path) as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS orders (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                executed_at     TEXT NOT NULL,
                symbol          TEXT,
                side            TEXT,
                quantity        REAL,
                entry_price     REAL,
                sl_price        REAL,
                tp_price        REAL,
                entry_order_id  TEXT,
                sl_order_id     TEXT,
                tp_order_id     TEXT,
                payload         TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_orders_time ON orders(executed_at);

            CREATE TABLE IF NOT EXISTS processed_runs (
                run_id  TEXT PRIMARY KEY,
                ts      TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS meta (
                k            TEXT PRIMARY KEY,
                trades_today INTEGER NOT NULL DEFAULT 0,
                trades_date  TEXT NOT NULL DEFAULT ''
            );
            INSERT OR IGNORE INTO meta (k, trades_today, trades_date)
            VALUES (?, 0, '');
            """
        )
        conn.execute("PRAGMA optimize;")


def _ensure(config: Optional[Dict[str, Any]]) -> Path:
    p = db_path(config)
    init_db(p)
    return p


# =====================================================================
# State (meta + processed_runs) — dict shape kept for risk_guard compat
# =====================================================================
def load_state(config: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Return the legacy state dict shape:
    {processed_run_ids: [...], trades_today: int, trades_date: str}.
    risk_guard reads exactly these keys, so do NOT change them."""
    p = _ensure(config)
    with connect(p, read_only=True) as conn:
        meta = conn.execute(
            "SELECT trades_today, trades_date FROM meta WHERE k=?", (META_KEY,)
        ).fetchone()
        rows = conn.execute("SELECT run_id FROM processed_runs").fetchall()
    if meta is None:
        meta = {"trades_today": 0, "trades_date": ""}
    return {
        "processed_run_ids": [r["run_id"] for r in rows],
        "trades_today": int(meta["trades_today"]),
        "trades_date": meta["trades_date"],
    }


def save_state(config: Dict[str, Any], state: Dict[str, Any]) -> None:
    """Persist meta (trades_today, trades_date) and any new processed_run_ids.
    Idempotent: re-saving the same state is a no-op."""
    p = _ensure(config)
    trades_today = int(state.get("trades_today", 0))
    trades_date = str(state.get("trades_date", ""))
    run_ids = state.get("processed_run_ids", []) or []
    now_iso = datetime.now(timezone.utc).isoformat()
    with connect(p) as conn:
        conn.execute(
            "INSERT INTO meta (k, trades_today, trades_date) VALUES (?, ?, ?) "
            "ON CONFLICT(k) DO UPDATE SET trades_today=excluded.trades_today, "
            "trades_date=excluded.trades_date",
            (META_KEY, trades_today, trades_date),
        )
        conn.executemany(
            "INSERT OR IGNORE INTO processed_runs (run_id, ts) VALUES (?, ?)",
            [(str(rid), now_iso) for rid in run_ids],
        )


def mark_processed(config: Dict[str, Any], run_id: str) -> None:
    """Record a run_id as processed (idempotent)."""
    p = _ensure(config)
    now_iso = datetime.now(timezone.utc).isoformat()
    with connect(p) as conn:
        conn.execute(
            "INSERT OR IGNORE INTO processed_runs (run_id, ts) VALUES (?, ?)",
            (str(run_id), now_iso),
        )


# =====================================================================
# Orders
# =====================================================================
def append_order(config: Dict[str, Any], result: Dict[str, Any]) -> None:
    """Insert one filled-order record. Stores known columns + full JSON payload."""
    p = _ensure(config)
    executed_at = result.get("executed_at") or datetime.now(timezone.utc).isoformat()
    with connect(p) as conn:
        conn.execute(
            """
            INSERT INTO orders (
                executed_at, symbol, side, quantity, entry_price, sl_price,
                tp_price, entry_order_id, sl_order_id, tp_order_id, payload
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                executed_at,
                result.get("symbol"),
                result.get("side"),
                _to_float(result.get("quantity")),
                _to_float(result.get("entry_price")),
                _to_float(result.get("sl_price")),
                _to_float(result.get("tp_price")),
                result.get("entry_order_id"),
                result.get("sl_order_id"),
                result.get("tp_order_id"),
                json.dumps(result, default=str),
            ),
        )


def _to_float(v: Any) -> Optional[float]:
    try:
        if v is None or v == "":
            return None
        return float(v)
    except (TypeError, ValueError):
        return None


def _row_to_order(row: sqlite3.Row) -> Dict[str, Any]:
    """Prefer structured columns; fall back to payload JSON for anything missing
    (keeps the shape the frontend/executor expects)."""
    base = {
        "executed_at": row["executed_at"],
        "symbol": row["symbol"],
        "side": row["side"],
        "quantity": row["quantity"],
        "entry_price": row["entry_price"],
        "sl_price": row["sl_price"],
        "tp_price": row["tp_price"],
        "entry_order_id": row["entry_order_id"],
        "sl_order_id": row["sl_order_id"],
        "tp_order_id": row["tp_order_id"],
    }
    try:
        payload = json.loads(row["payload"]) if row["payload"] else {}
    except (json.JSONDecodeError, TypeError):
        payload = {}
    # Merge payload for keys not stored as columns but present in the original
    # result (e.g. strategy, confidence). Payload wins only for missing keys.
    for k, v in payload.items():
        base.setdefault(k, v)
    return base


def recent_orders(config: Optional[Dict[str, Any]] = None, limit: int = 20) -> List[Dict[str, Any]]:
    """Newest-first order history."""
    p = db_path(config) if config else db_path()
    if not p.exists():
        return []
    with connect(p, read_only=True) as conn:
        rows = conn.execute(
            "SELECT * FROM orders ORDER BY id DESC LIMIT ?", (int(limit),)
        ).fetchall()
    return [_row_to_order(r) for r in rows]


def latest_order(config: Optional[Dict[str, Any]] = None) -> Optional[Dict[str, Any]]:
    """Most recent order, or None."""
    rows = recent_orders(config, limit=1)
    return rows[0] if rows else None
