"""
risk_guard.py — the mandatory gate before ANY Binance Futures order.

EVERY code path that ends in client.futures_create_order(...) MUST call
RiskGuard.validate() first. There is no bypass path, not even for "quick
testing". Kill switch check is never removed.

Validation rules (all must pass or order is blocked):
  1. Kill switch file absent
  2. Signal not stale (age <= max_signal_age_seconds)
  3. Confidence >= min_confidence
  4. Action in allowed_actions (BUY/SELL) — HOLD blocks
  5. Symbol in symbols_allowed
  6. run_id not already processed
  7. Daily trade count < max_daily_trades

State (processed run_ids + daily trade counter) is passed in by the caller
(executor.py owns persistence). This keeps risk_guard pure & unit-testable
without file I/O except the inherent kill-switch file check.
"""

from __future__ import annotations

import os
import time
from datetime import datetime, timezone
from typing import Tuple, Dict, Any


class RiskGuardError(Exception):
    """Raised only on programming errors, never on a blocked trade."""


class RiskGuard:
    def __init__(self, config: Dict[str, Any]):
        rg = config.get("risk_guard", {})
        trading = config.get("trading", {})

        self.min_confidence: float = float(rg.get("min_confidence", 0.6))
        self.max_signal_age_seconds: int = int(rg.get("max_signal_age_seconds", 300))
        self.max_daily_trades: int = int(rg.get("max_daily_trades", 10))
        self.kill_switch_file: str = rg.get("kill_switch_file", "KILL_SWITCH")
        self.allowed_actions = set(rg.get("allowed_actions", ["BUY", "SELL"]))
        self.symbols_allowed = set(trading.get("symbols_allowed", []))

    # ------------------------------------------------------------------
    # Kill switch — file based, never skipped.
    # ------------------------------------------------------------------
    def kill_switch_active(self, kill_path: str | None = None) -> bool:
        path = kill_path if kill_path is not None else self.kill_switch_file
        return os.path.exists(path)

    # ------------------------------------------------------------------
    # Core validation. Returns (ok, reason). reason is human-readable.
    # state = {"processed_run_ids": [...], "trades_today": int, "trades_date": "YYYY-MM-DD"}
    # now_ts = epoch seconds (injectable for testing)
    # ------------------------------------------------------------------
    def validate(
        self,
        signal: Dict[str, Any],
        state: Dict[str, Any],
        now_ts: float | None = None,
    ) -> Tuple[bool, str]:
        if now_ts is None:
            now_ts = time.time()

        # 1. Kill switch — checked first, always.
        if self.kill_switch_active(self.kill_switch_file):
            return False, "kill_switch_active: KILL_SWITCH file present, all trading blocked"

        # Basic shape sanity.
        run_id = signal.get("run_id")
        if not run_id:
            return False, "invalid_signal: missing run_id"

        action = str(signal.get("action", "")).upper()
        symbol = str(signal.get("symbol", "")).upper()
        confidence = signal.get("confidence")

        if not symbol:
            return False, "invalid_signal: missing symbol"
        if confidence is None:
            return False, "invalid_signal: missing confidence"

        try:
            confidence = float(confidence)
        except (TypeError, ValueError):
            return False, f"invalid_signal: confidence not numeric ({confidence!r})"

        # 2. Stale signal.
        generated_at = signal.get("generated_at")
        age = self._signal_age_seconds(generated_at, now_ts)
        if age is None:
            return False, f"invalid_signal: cannot parse generated_at ({generated_at!r})"
        if age > self.max_signal_age_seconds:
            return False, (
                f"stale_signal: age {age:.0f}s > max {self.max_signal_age_seconds}s"
            )

        # 3. Confidence.
        if confidence < self.min_confidence:
            return False, (
                f"low_confidence: {confidence:.3f} < min {self.min_confidence:.3f}"
            )

        # 4. Action allowed (HOLD or unknown blocks).
        if action not in self.allowed_actions:
            return False, f"action_blocked: action {action!r} not in {sorted(self.allowed_actions)}"

        # 5. Symbol allowed.
        if self.symbols_allowed and symbol not in self.symbols_allowed:
            return False, f"symbol_blocked: {symbol} not in allowed list"

        # 6. Already processed.
        processed = set(state.get("processed_run_ids", []))
        if run_id in processed:
            return False, f"already_processed: run_id {run_id} already executed"

        # 7. Daily trade limit.
        today = datetime.fromtimestamp(now_ts, tz=timezone.utc).strftime("%Y-%m-%d")
        trades_date = state.get("trades_date")
        trades_today = int(state.get("trades_today", 0)) if trades_date == today else 0
        if trades_today >= self.max_daily_trades:
            return False, (
                f"daily_limit_reached: {trades_today}/{self.max_daily_trades} trades today"
            )

        return True, "ok"

    # ------------------------------------------------------------------
    def _signal_age_seconds(self, generated_at: Any, now_ts: float) -> float | None:
        """Parse generated_at (ISO 8601 or epoch) and return age in seconds."""
        if generated_at is None:
            return None
        # Epoch number?
        if isinstance(generated_at, (int, float)):
            try:
                return max(0.0, now_ts - float(generated_at))
            except (TypeError, ValueError):
                return None
        s = str(generated_at).strip()
        # Try epoch-as-string first.
        try:
            return max(0.0, now_ts - float(s))
        except ValueError:
            pass
        # ISO 8601. Accept trailing Z.
        iso = s.replace("Z", "+00:00")
        try:
            dt = datetime.fromisoformat(iso)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return max(0.0, now_ts - dt.timestamp())
        except ValueError:
            return None
