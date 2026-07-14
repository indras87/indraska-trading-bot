"""
Unit tests for risk_guard.py.

Covers the mandatory block conditions:
  - kill switch present -> block
  - stale signal -> block
  - low confidence -> block
  - action not allowed (HOLD) -> block
  - symbol not allowed -> block
  - run_id already processed -> block
  - daily limit reached -> block
  - valid signal -> pass

Run:  .venv/bin/python -m pytest executor/test_risk_guard.py -v
"""

import json
import os
import time
from datetime import datetime, timezone

import pytest
import yaml

from risk_guard import RiskGuard


# ---------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def load_real_config():
    with open(os.path.join(REPO_ROOT, "executor", "config.yaml")) as f:
        return yaml.safe_load(f)


@pytest.fixture
def config(tmp_path):
    cfg = load_real_config()
    # Point kill switch at tmp so tests don't trip on a real repo KILL_SWITCH.
    cfg["risk_guard"]["kill_switch_file"] = str(tmp_path / "KILL_SWITCH")
    return cfg


@pytest.fixture
def guard(config):
    return RiskGuard(config)


def fresh_state():
    return {"processed_run_ids": [], "trades_today": 0, "trades_date": ""}


def make_signal(**overrides):
    base = {
        "symbol": "BTCUSDT",
        "action": "BUY",
        "confidence": 0.80,
        "reason": "test signal",
        "run_id": "run-1",
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }
    base.update(overrides)
    return base


NOW = time.time()


# ---------------------------------------------------------------------
# Passing case
# ---------------------------------------------------------------------
def test_valid_signal_passes(guard):
    state = fresh_state()
    ok, reason = guard.validate(make_signal(), state, now_ts=NOW)
    assert ok is True
    assert reason == "ok"


# ---------------------------------------------------------------------
# Kill switch
# ---------------------------------------------------------------------
def test_kill_switch_blocks(guard, config):
    ks = config["risk_guard"]["kill_switch_file"]
    with open(ks, "w") as f:
        f.write("kill")
    ok, reason = guard.validate(make_signal(), fresh_state(), now_ts=NOW)
    assert ok is False
    assert "kill_switch_active" in reason


def test_kill_switch_absent_allows(guard, config):
    ks = config["risk_guard"]["kill_switch_file"]
    assert not os.path.exists(ks)
    ok, _ = guard.validate(make_signal(), fresh_state(), now_ts=NOW)
    assert ok is True


# ---------------------------------------------------------------------
# Stale signal
# ---------------------------------------------------------------------
def test_stale_signal_blocks(guard):
    # generated 1 hour ago; max age default 300s
    old_ts = NOW - 3600
    sig = make_signal(generated_at=old_ts)
    ok, reason = guard.validate(sig, fresh_state(), now_ts=NOW)
    assert ok is False
    assert "stale_signal" in reason


def test_fresh_signal_passes(guard):
    sig = make_signal(generated_at=NOW - 10)  # 10s old
    ok, _ = guard.validate(sig, fresh_state(), now_ts=NOW)
    assert ok is True


# ---------------------------------------------------------------------
# Low confidence
# ---------------------------------------------------------------------
def test_low_confidence_blocks(guard):
    sig = make_signal(confidence=0.30)  # below 0.60 default
    ok, reason = guard.validate(sig, fresh_state(), now_ts=NOW)
    assert ok is False
    assert "low_confidence" in reason


def test_confidence_boundary_passes(guard):
    sig = make_signal(confidence=0.60)  # exactly min -> passes
    ok, _ = guard.validate(sig, fresh_state(), now_ts=NOW)
    assert ok is True


# ---------------------------------------------------------------------
# Action not allowed (HOLD)
# ---------------------------------------------------------------------
def test_hold_action_blocks(guard):
    sig = make_signal(action="HOLD")
    ok, reason = guard.validate(sig, fresh_state(), now_ts=NOW)
    assert ok is False
    assert "action_blocked" in reason


def test_unknown_action_blocks(guard):
    sig = make_signal(action="BUY_MORE")
    ok, reason = guard.validate(sig, fresh_state(), now_ts=NOW)
    assert ok is False
    assert "action_blocked" in reason


# ---------------------------------------------------------------------
# Symbol not allowed
# ---------------------------------------------------------------------
def test_disallowed_symbol_blocks(guard):
    sig = make_signal(symbol="DOGEUSDT")  # not in default allowed list
    ok, reason = guard.validate(sig, fresh_state(), now_ts=NOW)
    assert ok is False
    assert "symbol_blocked" in reason


# ---------------------------------------------------------------------
# Already processed
# ---------------------------------------------------------------------
def test_already_processed_blocks(guard):
    state = fresh_state()
    state["processed_run_ids"] = ["run-1"]
    ok, reason = guard.validate(make_signal(run_id="run-1"), state, now_ts=NOW)
    assert ok is False
    assert "already_processed" in reason


# ---------------------------------------------------------------------
# Daily limit
# ---------------------------------------------------------------------
def test_daily_limit_blocks(guard):
    today = datetime.fromtimestamp(NOW, tz=timezone.utc).strftime("%Y-%m-%d")
    state = fresh_state()
    state["trades_date"] = today
    state["trades_today"] = 10  # default max_daily_trades
    ok, reason = guard.validate(make_signal(run_id="run-new"), state, now_ts=NOW)
    assert ok is False
    assert "daily_limit_reached" in reason


def test_daily_limit_rolls_over(guard):
    # trades_date is yesterday -> counter resets, signal passes
    state = fresh_state()
    state["trades_date"] = "2020-01-01"
    state["trades_today"] = 99
    ok, _ = guard.validate(make_signal(run_id="run-new"), state, now_ts=NOW)
    assert ok is True


# ---------------------------------------------------------------------
# Missing/invalid fields
# ---------------------------------------------------------------------
@pytest.mark.parametrize("bad", [
    {"run_id": None},
    {"symbol": ""},
    {"confidence": None},
])
def test_invalid_fields_block(guard, bad):
    sig = make_signal(**bad)
    ok, reason = guard.validate(sig, fresh_state(), now_ts=NOW)
    assert ok is False
    assert "invalid_signal" in reason


def test_non_numeric_confidence_blocks(guard):
    sig = make_signal(confidence="high")
    ok, reason = guard.validate(sig, fresh_state(), now_ts=NOW)
    assert ok is False
    assert "invalid_signal" in reason


# ---------------------------------------------------------------------
# ISO and epoch parsing
# ---------------------------------------------------------------------
def test_epoch_generated_at_parsed(guard):
    sig = make_signal(generated_at=NOW - 5)
    ok, _ = guard.validate(sig, fresh_state(), now_ts=NOW)
    assert ok is True


def test_unparseable_generated_at_blocks(guard):
    sig = make_signal(generated_at="not-a-date")
    ok, reason = guard.validate(sig, fresh_state(), now_ts=NOW)
    assert ok is False
    assert "invalid_signal" in reason
