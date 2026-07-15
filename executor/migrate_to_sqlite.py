"""
migrate_to_sqlite.py — one-shot import of legacy file state into bot.db.

Reads (if present):
  runtime/state.json            -> meta + processed_runs
  runtime/orders_history.jsonl  -> orders

Then renames the legacy files to *.bak (never deletes — proof + rollback).

Idempotent: re-running on already-migrated *.bak files is a no-op for data
(inserts use INSERT OR IGNORE for run_ids; orders are not re-inserted because
the source .jsonl is only read if present, and it gets renamed to .bak after
the first run).

Usage:
    python executor/migrate_to_sqlite.py [--dry-run]
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

EXECUTOR_DIR = Path(__file__).resolve().parent
REPO_ROOT = EXECUTOR_DIR.parent
sys.path.insert(0, str(EXECUTOR_DIR))

import store  # noqa: E402

STATE_LEGACY = REPO_ROOT / "runtime" / "state.json"
HISTORY_LEGACY = REPO_ROOT / "runtime" / "orders_history.jsonl"
# Older default location (pre-config-change). Check both; first found wins.
STATE_LEGACY_ALT = EXECUTOR_DIR / "state.json"
HISTORY_LEGACY_ALT = EXECUTOR_DIR / "orders_history.jsonl"


def _resolve_legacy(preferred: Path, alt: Path) -> Path:
    return preferred if preferred.exists() else (alt if alt.exists() else preferred)


def migrate(dry_run: bool) -> None:
    cfg = {}  # store uses default runtime/bot.db
    db = store.db_path(cfg)
    print(f"[migrate] DB target: {db}")
    print(f"[migrate] dry_run={dry_run}")

    if dry_run:
        # Still init so we can count, but report only.
        store.init_db(db)

    state_imported = runs_imported = 0
    orders_imported = 0

    # --- state.json -> meta + processed_runs ---
    state_src = _resolve_legacy(STATE_LEGACY, STATE_LEGACY_ALT)
    if state_src.exists():
        try:
            with open(state_src) as f:
                legacy = json.load(f)
        except (json.JSONDecodeError, OSError) as e:
            print(f"[migrate] WARN: could not parse {state_src}: {e}")
            legacy = {}
        if not dry_run:
            store.save_state(cfg, {
                "processed_run_ids": legacy.get("processed_run_ids", []),
                "trades_today": int(legacy.get("trades_today", 0) or 0),
                "trades_date": legacy.get("trades_date", ""),
            })
        state_imported = 1
        runs_imported = len(legacy.get("processed_run_ids", []))
        print(f"[migrate] {state_src.name}: trades_today={legacy.get('trades_today')}, "
              f"trades_date={legacy.get('trades_date')}, "
              f"processed_run_ids={runs_imported}")
    else:
        print(f"[migrate] no legacy state.json found (skipping state import)")

    # --- orders_history.jsonl -> orders ---
    hist_src = _resolve_legacy(HISTORY_LEGACY, HISTORY_LEGACY_ALT)
    if hist_src.exists():
        try:
            with open(hist_src) as f:
                lines = f.readlines()
        except OSError as e:
            print(f"[migrate] WARN: could not read {hist_src}: {e}")
            lines = []
        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not dry_run:
                store.append_order(cfg, rec)
            orders_imported += 1
        print(f"[migrate] {hist_src.name}: imported {orders_imported} order(s)")
    else:
        print(f"[migrate] no legacy orders_history.jsonl found (skipping history import)")

    # --- rename legacy to .bak (proof + rollback) ---
    if not dry_run:
        for src in (state_src, hist_src):
            if src.exists():
                bak = src.with_suffix(src.suffix + ".bak")
                # If .bak already exists, do not clobber — append a counter.
                if bak.exists():
                    i = 1
                    while True:
                        cand = src.with_suffix(src.suffix + f".bak{i}")
                        if not cand.exists():
                            bak = cand
                            break
                        i += 1
                src.rename(bak)
                print(f"[migrate] renamed {src.name} -> {bak.name}")

    # --- verify ---
    with store.connect(db, read_only=True) as conn:
        meta = conn.execute("SELECT trades_today, trades_date FROM meta WHERE k='executor'").fetchone()
        n_orders = conn.execute("SELECT COUNT(*) FROM orders").fetchone()[0]
        n_runs = conn.execute("SELECT COUNT(*) FROM processed_runs").fetchone()[0]
    print("[migrate] === verification ===")
    print(f"[migrate]   meta: {dict(meta) if meta else None}")
    print(f"[migrate]   orders rows: {n_orders}")
    print(f"[migrate]   processed_runs rows: {n_runs}")
    print("[migrate] done.")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="report only, do not write/rename")
    args = ap.parse_args()
    migrate(args.dry_run)
