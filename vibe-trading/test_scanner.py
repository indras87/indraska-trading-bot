"""
Unit tests for scanner.py — ranking + RSI logic with mock data (no network).

Run: .venv/bin/python -m pytest vibe-trading/test_scanner.py -v
"""

import sys
import os
from pathlib import Path

# Make vibe-trading importable.
sys.path.insert(0, str(Path(__file__).resolve().parent))

import scanner


# ---------------------------------------------------------------------
# rank_candidates
# ---------------------------------------------------------------------
def test_rank_filters_low_volume():
    tickers = {
        "BTCUSDT": {"price": 60000, "change_pct": 2.0, "quote_volume": 1_000_000_000},
        "DUSTUSDT": {"price": 0.01, "change_pct": 50.0, "quote_volume": 100},
    }
    ranked = scanner.rank_candidates(tickers, min_quote_volume=10_000_000)
    symbols = [r["symbol"] for r in ranked]
    assert symbols == ["BTCUSDT"]  # DUST filtered out


def test_rank_orders_by_score():
    # Equal volume -> change% dominates the score.
    tickers = {
        "AUSDT": {"price": 1, "change_pct": 5.0, "quote_volume": 100_000_000},
        "BUSDT": {"price": 1, "change_pct": 20.0, "quote_volume": 100_000_000},
        "CUSDT": {"price": 1, "change_pct": 1.0, "quote_volume": 100_000_000},
    }
    ranked = scanner.rank_candidates(tickers, min_quote_volume=0)
    assert [r["symbol"] for r in ranked] == ["BUSDT", "AUSDT", "CUSDT"]


def test_rank_respects_allowed_symbols():
    tickers = {
        "AUSDT": {"price": 1, "change_pct": 50, "quote_volume": 1e9},
        "BUSDT": {"price": 1, "change_pct": 1, "quote_volume": 1e9},
    }
    ranked = scanner.rank_candidates(tickers, allowed_symbols=["BUSDT"], min_quote_volume=0)
    assert [r["symbol"] for r in ranked] == ["BUSDT"]


# ---------------------------------------------------------------------
# compute_rsi — patch _get to return fake klines
# ---------------------------------------------------------------------
def test_rsi_all_up(monkeypatch):
    # Strictly increasing closes => RSI 100.
    closes = [100 + i for i in range(20)]
    klines = [[0, 0, 0, 0, c] for c in closes]
    monkeypatch.setattr(scanner, "_get", lambda path, params=None, timeout=20: klines)
    rsi = scanner.compute_rsi("TESTUSDT")
    assert rsi == 100.0


def test_rsi_all_down(monkeypatch):
    closes = [200 - i for i in range(20)]
    klines = [[0, 0, 0, 0, c] for c in closes]
    monkeypatch.setattr(scanner, "_get", lambda path, params=None, timeout=20: klines)
    rsi = scanner.compute_rsi("TESTUSDT")
    assert rsi == 0.0


def test_rsi_midrange(monkeypatch):
    # Alternating up/down => RSI near 50.
    closes = [100, 101, 100, 101, 100, 101, 100, 101, 100, 101,
              100, 101, 100, 101, 100, 101, 100, 101, 100, 101]
    klines = [[0, 0, 0, 0, c] for c in closes]
    monkeypatch.setattr(scanner, "_get", lambda path, params=None, timeout=20: klines)
    rsi = scanner.compute_rsi("TESTUSDT")
    assert 40.0 < rsi < 60.0


def test_rsi_insufficient_data(monkeypatch):
    klines = [[0, 0, 0, 0, 100], [0, 0, 0, 0, 101]]  # only 2 closes
    monkeypatch.setattr(scanner, "_get", lambda path, params=None, timeout=20: klines)
    assert scanner.compute_rsi("TESTUSDT") is None
