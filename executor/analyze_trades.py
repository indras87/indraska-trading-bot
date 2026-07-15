#!/usr/bin/env python3
"""
analyze_trades.py — read-only trade performance analysis from bot.db.

Analyzes CLOSED trades (status='closed', outcome in win/loss) and breaks
performance down by symbol, side (BUY/SELL), hour-of-day, and exit type.
Answers: "where does my edge actually come from?"

Does NOT touch the order path, risk_guard, config, or any live credential.
Pure read-only SQL over the executor's SQLite DB. Safe to run anytime.

Usage:
    python3 executor/analyze_trades.py                 # default runtime/bot.db
    python3 executor/analyze_trades.py --db /path/to/bot.db
    python3 executor/analyze_trades.py --json          # machine-readable output
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any, Dict, List, Optional

# Reuse the executor's path resolver so we always point at the same DB.
sys.path.insert(0, str(Path(__file__).resolve().parent))
import store  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent


# =====================================================================
# Load
# =====================================================================
def resolve_db(arg_db: Optional[str]) -> Path:
    if arg_db:
        p = Path(arg_db)
        return p if p.is_absolute() else (REPO_ROOT / p)
    # Default: same path the executor uses.
    return store.db_path()


def load_closed(conn: sqlite3.Connection) -> List[sqlite3.Row]:
    """All closed trades, oldest-first for chronological aggregates."""
    return conn.execute(
        "SELECT * FROM orders WHERE status='closed' ORDER BY id ASC"
    ).fetchall()


def parse_hour(executed_at: Optional[str]) -> Optional[int]:
    """Extract UTC hour (0-23) from an ISO timestamp, or None."""
    if not executed_at:
        return None
    # executor writes datetime.now(timezone.utc).isoformat() → 2026-07-14T20:56:...
    try:
        return int(executed_at[11:13])
    except (ValueError, IndexError):
        return None


# =====================================================================
# Aggregate
# =====================================================================
def bucket_stats(trades: List[sqlite3.Row]) -> Dict[str, Any]:
    """Compute win/loss/PnL metrics over a group of trades."""
    wins: List[float] = []
    losses: List[float] = []
    total_pnl = 0.0
    for t in trades:
        pnl = t["realized_pnl"]
        pnl = float(pnl) if pnl is not None else 0.0
        total_pnl += pnl
        if t["outcome"] == "win":
            wins.append(pnl)
        elif t["outcome"] == "loss":
            losses.append(pnl)
        # outcome == NULL/other → counted in total PnL but not in win/loss.
    decided = len(wins) + len(losses)
    gross_profit = sum(wins)
    gross_loss = abs(sum(losses))
    profit_factor = (gross_profit / gross_loss) if gross_loss > 0 else float("inf")
    avg_win = (gross_profit / len(wins)) if wins else 0.0
    avg_loss = (gross_loss / len(losses)) if losses else 0.0
    expectancy = (total_pnl / len(trades)) if trades else 0.0
    return {
        "trades": len(trades),
        "decided": decided,
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": round(len(wins) / decided * 100, 2) if decided else 0.0,
        "total_pnl": round(total_pnl, 6),
        "avg_win": round(avg_win, 6),
        "avg_loss": round(avg_loss, 6),
        "profit_factor": round(profit_factor, 3) if profit_factor != float("inf") else None,
        "expectancy_per_trade": round(expectancy, 6),
    }


def group_by(rows: List[sqlite3.Row], key_fn) -> Dict[str, List[sqlite3.Row]]:
    out: Dict[str, List[sqlite3.Row]] = defaultdict(list)
    for r in rows:
        k = key_fn(r)
        if k is None:
            continue
        out[str(k)].append(r)
    return out


def build_report(conn: sqlite3.Connection) -> Dict[str, Any]:
    closed = load_closed(conn)
    overall = bucket_stats(closed)

    by_symbol = {k: bucket_stats(v) for k, v in sorted(group_by(closed, lambda r: r["symbol"]).items())}
    by_side = {k: bucket_stats(v) for k, v in sorted(group_by(closed, lambda r: r["side"]).items())}
    by_exit = {k: bucket_stats(v) for k, v in sorted(group_by(closed, lambda r: r["exit_type"]).items())}
    by_hour = {k: bucket_stats(v) for k, v in sorted(group_by(closed, lambda r: parse_hour(r["executed_at"])).items(),
                                                     key=lambda kv: int(kv[0]))}

    return {
        "overall": overall,
        "by_symbol": by_symbol,
        "by_side": by_side,
        "by_exit_type": by_exit,
        "by_hour_utc": by_hour,
    }


# =====================================================================
# Render
# =====================================================================
def _row(label: str, s: Dict[str, Any]) -> str:
    pf = s["profit_factor"]
    pf_str = f"{pf:.2f}" if pf is not None else "inf"
    return (
        f"  {label:<14} trades={s['trades']:<4} WR={s['win_rate']:>6}%  "
        f"PnL={s['total_pnl']:>11}  avgW={s['avg_win']:>9} avgL={s['avg_loss']:>9}  "
        f"PF={pf_str:>5}  exp={s['expectancy_per_trade']:>9}"
    )


def render_text(report: Dict[str, Any]) -> str:
    o = report["overall"]
    lines: List[str] = []
    lines.append("=" * 96)
    lines.append("TRADE PERFORMANCE REPORT  (closed trades, executor-owned outcome)")
    lines.append("=" * 96)
    if o["trades"] == 0:
        lines.append("No closed trades found. Run the executor longer to collect data.")
        return "\n".join(lines)

    lines.append(_row("OVERALL", o))
    lines.append("-" * 96)

    lines.append("BY SYMBOL")
    for k, s in report["by_symbol"].items():
        lines.append(_row(k or "?", s))
    if not report["by_symbol"]:
        lines.append("  (none)")

    lines.append("-" * 96)
    lines.append("BY SIDE (BUY/SELL)")
    for k, s in report["by_side"].items():
        lines.append(_row(k or "?", s))

    lines.append("-" * 96)
    lines.append("BY EXIT TYPE (sl / tp / manual)")
    for k, s in report["by_exit_type"].items():
        lines.append(_row(k or "?", s))

    lines.append("-" * 96)
    lines.append("BY HOUR (UTC)")
    for k, s in report["by_hour_utc"].items():
        lines.append(_row(f"{k:>2}h", s))

    lines.append("=" * 96)
    lines.append("Legend: WR=win%  PnL=total realized  avgW/avgL=avg win/loss size")
    lines.append("        PF=profit factor (gross profit / gross loss, >1 = profitable)")
    lines.append("        exp=expectancy per trade (avg PnL/trade, >0 = positive edge)")
    lines.append("=" * 96)
    return "\n".join(lines)


# =====================================================================
# Main
# =====================================================================
def main() -> int:
    ap = argparse.ArgumentParser(description="Read-only trade performance analysis.")
    ap.add_argument("--db", default=None, help="Path to bot.db (default: runtime/bot.db)")
    ap.add_argument("--json", action="store_true", help="Emit JSON instead of text")
    args = ap.parse_args()

    db = resolve_db(args.db)
    if not db.exists():
        print(f"DB not found: {db}", file=sys.stderr)
        print("Has the executor ever run? bot.db is created on first trade.",
              file=sys.stderr)
        return 1

    conn = sqlite3.connect(str(db))
    conn.row_factory = sqlite3.Row
    try:
        report = build_report(conn)
    finally:
        conn.close()

    if args.json:
        print(json.dumps(report, indent=2, default=str))
    else:
        print(render_text(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
