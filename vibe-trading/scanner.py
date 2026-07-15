"""
scanner.py — market scanner for Binance USDT-M Futures.

Reads PUBLIC market data (no API key, no trading) from Binance mainnet to find
the most liquid + active USDT perpetuals, computes RSI for the top candidates,
and returns a ranked shortlist. This module NEVER sends orders and NEVER uses
credentials — it only reads public market data. Architecture stays intact:
all trading still happens in executor/.

Public endpoints used:
  GET /fapi/v1/exchangeInfo   -> symbol universe
  GET /fapi/v1/ticker/24hr    -> 24h volume + price change (one call, all symbols)
  GET /fapi/v1/klines         -> candles for RSI (only for the shortlist)

RSI = 100 - 100/(1+RS), RS = avg gain / avg loss over `rsi_period` closes.
"""

from __future__ import annotations

import logging
import os
from typing import Any, Dict, List

import requests

LOG = logging.getLogger("scanner")

# Mainnet public market data. Real prices/volume => meaningful signals.
# (Trading itself still happens on testnet via executor/.)
BASE_URL = os.environ.get("SCANNER_BASE_URL", "https://fapi.binance.com")

# Stablecoins & index tokens to skip — not real trade targets.
SKIP_ASSETS = {"USDC", "FDUSD", "TUSD", "BUSD", "USDP", "EUR", "GBP"}


def _get(path: str, params: Dict[str, Any] | None = None, timeout: int = 20) -> Any:
    url = f"{BASE_URL}{path}"
    r = requests.get(url, params=params, timeout=timeout)
    r.raise_for_status()
    return r.json()


# ---------------------------------------------------------------------
# Symbol universe
# ---------------------------------------------------------------------
def get_usdt_perp_symbols() -> List[str]:
    """All actively-trading USDT-M perpetual symbols (e.g. BTCUSDT)."""
    info = _get("/fapi/v1/exchangeInfo")
    out = []
    for s in info.get("symbols", []):
        if (
            s.get("quoteAsset") == "USDT"
            and s.get("contractType") == "PERPETUAL"
            and s.get("status") == "TRADING"
        ):
            base = s.get("baseAsset", "")
            if base in SKIP_ASSETS:
                continue
            out.append(s["symbol"])
    return sorted(out)


# ---------------------------------------------------------------------
# 24h market data (one call for all symbols)
# ---------------------------------------------------------------------
def fetch_24h_tickers() -> Dict[str, Dict[str, float]]:
    raw = _get("/fapi/v1/ticker/24hr", timeout=30)
    out: Dict[str, Dict[str, float]] = {}
    for t in raw:
        sym = t.get("symbol", "")
        try:
            out[sym] = {
                "price": float(t["lastPrice"]),
                "change_pct": float(t["priceChangePercent"]),
                "quote_volume": float(t["quoteVolume"]),  # USDT volume 24h
            }
        except (KeyError, ValueError, TypeError):
            continue
    return out


# ---------------------------------------------------------------------
# RSI
# ---------------------------------------------------------------------
def compute_rsi(symbol: str, interval: str = "1h", period: int = 14) -> float | None:
    """Classic RSI on close prices. Returns None if not enough data."""
    try:
        raw = _get(
            "/fapi/v1/klines",
            params={"symbol": symbol, "interval": interval, "limit": period * 5},
            timeout=15,
        )
    except requests.HTTPError:
        return None
    closes = [float(k[4]) for k in raw if k and len(k) > 4]
    if len(closes) < period + 1:
        return None
    gains, losses = 0.0, 0.0
    for i in range(1, period + 1):
        diff = closes[i] - closes[i - 1]
        if diff >= 0:
            gains += diff
        else:
            losses -= diff
    if losses == 0:
        return 100.0
    rs = (gains / period) / (losses / period)
    return 100.0 - 100.0 / (1.0 + rs)


# ---------------------------------------------------------------------
# Ranking
# ---------------------------------------------------------------------
def rank_candidates(
    tickers: Dict[str, Dict[str, float]],
    allowed_symbols: List[str] | None = None,
    min_quote_volume: float = 10_000_000,
) -> List[Dict[str, Any]]:
    """Rank by liquidity filter + momentum score.

    score = normalized |24h change%| weighted by log(volume) so big, active
    markets rank highest. Returns list of {symbol, price, change_pct,
    quote_volume, score} sorted desc.
    """
    import math

    rows = []
    for sym, d in tickers.items():
        if allowed_symbols is not None and sym not in allowed_symbols:
            continue
        vol = d.get("quote_volume", 0)
        if vol < min_quote_volume:
            continue
        chg = abs(d.get("change_pct", 0))
        # score rewards liquidity (log volume) and movement (change).
        score = math.log10(vol + 1) * (1 + chg / 100.0)
        rows.append(
            {
                "symbol": sym,
                "price": d["price"],
                "change_pct": d["change_pct"],
                "quote_volume": vol,
                "score": score,
            }
        )
    rows.sort(key=lambda r: r["score"], reverse=True)
    return rows


# ---------------------------------------------------------------------
# Full scan: universe -> shortlist with RSI
# ---------------------------------------------------------------------
def scan(
    top_n: int = 10,
    rsi_interval: str = "1h",
    rsi_period: int = 14,
    min_quote_volume: float = 10_000_000,
    allowed_symbols: List[str] | None = None,
    rsi_for_top: int = 20,
    return_ranked: bool = False,
) -> List[Dict[str, Any]] | tuple:
    """Run a full scan. Returns top_n candidates enriched with RSI.

    `rsi_for_top` controls how many of the ranked top we compute RSI for
    (klines are per-symbol; keep this bounded). top_n <= rsi_for_top.

    If `return_ranked=True`, returns a (top_candidates, all_ranked) tuple where
    all_ranked is every symbol passing the liquidity filter (for dashboard
    background display), each as {symbol, change_pct, score} without RSI.
    """
    tickers = fetch_24h_tickers()
    # Restrict to the real USDT-perp universe (ticker/24hr returns ALL pairs).
    universe = set(allowed_symbols) if allowed_symbols is not None else set(get_usdt_perp_symbols())
    ranked = rank_candidates(tickers, list(universe), min_quote_volume)
    shortlist = ranked[: max(rsi_for_top, top_n)]
    LOG.info("scan: %d symbols after liquidity filter, top shortlist %d",
             len(ranked), len(shortlist))
    for row in shortlist[:top_n]:
        row["rsi"] = compute_rsi(row["symbol"], rsi_interval, rsi_period)
    if return_ranked:
        # Lightweight projection of the full ranked universe (no RSI/price bloat).
        all_ranked = [
            {"symbol": r["symbol"], "change_pct": r["change_pct"], "score": r["score"]}
            for r in ranked
        ]
        return shortlist[:top_n], all_ranked
    return shortlist[:top_n]
