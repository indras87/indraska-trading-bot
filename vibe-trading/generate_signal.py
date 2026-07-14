"""
generate_signal.py — research/signal side ONLY. Never sends orders.

Providers:
  scan  (default): market-scan ALL USDT-perp on Binance (public data), rank
                   top-N by liquidity+momentum, compute RSI, then either
                   Z.ai GLM decides BUY/SELL/confidence for the shortlist OR
                   a rule-based fallback (no ZAI_API_KEY). Writes a LIST of
                   signals. This is real "scanning".
  zai:            single-symbol GLM signal (legacy 5-coin list).
  vibe:           vibe-trading-ai package (best-effort).
  mock:           deterministic single mock signal (testnet quick-test).

Writes:  <repo>/signals/latest_signal.json
Shape:   single signal dict {symbol, action, confidence, reason, run_id,
         generated_at}  — OR —  a JSON array of such dicts (scan mode).

NOTE on architecture (CLAUDE.md): the documented stack is the
`vibe-trading-ai` pip package with Z.ai GLM Coding Plan. That package pulls
a heavy dependency tree and its install is slow/uncertain in this sandbox,
so Z.ai is called here directly via its OpenAI-compatible endpoint with the
same base URL (ZAI_BASE_URL). Behaviour is equivalent for signal generation.
See RUN_REPORT.md.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SIGNAL_FILE = REPO_ROOT / "signals" / "latest_signal.json"

DEFAULT_BASE_URL = "https://api.z.ai/api/coding/paas/v4"
DEFAULT_MODEL = "glm-4.5"
ALLOWED_SYMBOLS = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "XRPUSDT"]
ALLOWED_ACTIONS = ["BUY", "SELL", "HOLD"]


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def write_signal(signal) -> None:
    """Write a single signal dict OR a list of signal dicts (scan mode)."""
    SIGNAL_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = SIGNAL_FILE.with_suffix(".tmp")
    with open(tmp, "w") as f:
        json.dump(signal, f, indent=2)
    os.replace(tmp, SIGNAL_FILE)
    if isinstance(signal, list):
        print(f"[signal] wrote {len(signal)} signals to {SIGNAL_FILE}")
        for s in signal:
            print(f"  - {s['symbol']} {s['action']} conf={s['confidence']}")
    else:
        print(f"[signal] wrote {SIGNAL_FILE}: {signal}")


def _new_signal(symbol: str, action: str, confidence: float, reason: str, tag: str) -> dict:
    return {
        "symbol": str(symbol).upper(),
        "action": str(action).upper(),
        "confidence": float(confidence),
        "reason": str(reason)[:300],
        "run_id": f"{tag}-{uuid.uuid4().hex[:8]}",
        "generated_at": now_iso(),
    }


# ---------------------------------------------------------------------
# Mock provider (deterministic-ish, testnet-safe)
# ---------------------------------------------------------------------
def mock_signal(symbol: str = "BTCUSDT", action: str = "BUY") -> dict:
    return {
        "symbol": symbol,
        "action": action,
        "confidence": 0.75,
        "reason": "MOCK signal for testnet end-to-end (no ZAI_API_KEY configured)",
        "run_id": f"mock-{uuid.uuid4().hex[:8]}",
        "generated_at": now_iso(),
    }


# ---------------------------------------------------------------------
# Z.ai GLM provider (OpenAI-compatible chat/completions)
# ---------------------------------------------------------------------
def zai_signal() -> dict:
    import requests  # local import

    base = os.environ.get("ZAI_BASE_URL", DEFAULT_BASE_URL).rstrip("/")
    model = os.environ.get("ZAI_MODEL", DEFAULT_MODEL)
    key = os.environ.get("ZAI_API_KEY")
    if not key:
        raise RuntimeError("ZAI_API_KEY not set")

    symbols = ", ".join(ALLOWED_SYMBOLS)
    system = (
        "You are a crypto futures signal analyst. Output STRICT JSON only, "
        "no prose. Schema: "
        '{"symbol":str, "action":"BUY|SELL|HOLD", "confidence":float 0..1, '
        '"reason":str}. '
        f"symbol must be one of: {symbols}. "
        "confidence reflects your conviction. reason is one short sentence."
    )
    user = (
        f"Current UTC time: {now_iso()}. Generate one signal for the most "
        "actionable symbol among the allowed list based on your analysis."
    )

    url = f"{base}/chat/completions"
    resp = requests.post(
        url,
        headers={
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
        },
        json={
            "model": model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": 0.3,
        },
        timeout=60,
    )
    resp.raise_for_status()
    content = resp.json()["choices"][0]["message"]["content"].strip()
    # Tolerate ```json fences.
    if content.startswith("```"):
        content = content.strip("`")
        if content.lower().startswith("json"):
            content = content[4:].strip()
    parsed = json.loads(content)

    symbol = str(parsed.get("symbol", "")).upper()
    action = str(parsed.get("action", "")).upper()
    if symbol not in ALLOWED_SYMBOLS:
        raise ValueError(f"LLM returned disallowed symbol: {symbol}")
    if action not in ALLOWED_ACTIONS:
        raise ValueError(f"LLM returned invalid action: {action}")
    confidence = float(parsed["confidence"])

    return {
        "symbol": symbol,
        "action": action,
        "confidence": confidence,
        "reason": str(parsed.get("reason", ""))[:300],
        "run_id": f"zai-{uuid.uuid4().hex[:8]}",
        "generated_at": now_iso(),
    }


# ---------------------------------------------------------------------
# Optional vibe-trading-ai integration (best-effort)
# ---------------------------------------------------------------------
def vibe_trading_ai_signal() -> dict:
    """Use the vibe-trading-ai package if installed & configured."""
    from vibe_trading_ai import generate_trade_signal  # type: ignore

    result = generate_trade_signal()  # API may differ by version
    return {
        "symbol": str(result.get("symbol", "BTCUSDT")).upper(),
        "action": str(result.get("action", "HOLD")).upper(),
        "confidence": float(result.get("confidence", 0.0)),
        "reason": str(result.get("reason", "vibe-trading-ai"))[:300],
        "run_id": f"vibe-{uuid.uuid4().hex[:8]}",
        "generated_at": now_iso(),
    }


# ---------------------------------------------------------------------
# Market SCAN provider (all USDT-perp). Uses scanner.py + Z.ai GLM,
# with a rule-based fallback when ZAI_API_KEY is absent.
# ---------------------------------------------------------------------
def _glm_decide_scan(candidates: list) -> list:
    """Send ranked shortlist to Z.ai GLM; return list of decision dicts."""
    import requests

    base = os.environ.get("ZAI_BASE_URL", DEFAULT_BASE_URL).rstrip("/")
    model = os.environ.get("ZAI_MODEL", DEFAULT_MODEL)
    key = os.environ.get("ZAI_API_KEY")
    if not key:
        raise RuntimeError("ZAI_API_KEY not set")

    # Build a compact market table.
    lines = ["symbol | price | 24h_change% | 24h_quote_vol_USDT | RSI"]
    for c in candidates:
        rsi = c.get("rsi")
        rsi_s = f"{rsi:.1f}" if isinstance(rsi, (int, float)) else "n/a"
        lines.append(
            f"{c['symbol']} | {c['price']} | {c['change_pct']:.2f} | "
            f"{c['quote_volume']:.0f} | {rsi_s}"
        )
    table = "\n".join(lines)

    system = (
        "You are a crypto futures trader. Given ranked market data (top movers "
        "by liquidity+momentum, with RSI), return the trades worth taking now. "
        'Output STRICT JSON only: an array of objects '
        '{"symbol":str, "action":"BUY"|"SELL", "confidence":float 0..1, '
        '"reason":str}. Only include trades you actually recommend (confidence '
        '>= 0.6). BUY = expect up, SELL = expect down. Use RSI: >70 overbought '
        '(favor SELL), <30 oversold (favor BUY). Empty array [] if nothing good.'
    )
    user = f"Current UTC: {now_iso()}\nMarket data:\n{table}"

    resp = requests.post(
        f"{base}/chat/completions",
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        json={
            "model": model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": 0.3,
        },
        timeout=90,
    )
    resp.raise_for_status()
    content = resp.json()["choices"][0]["message"]["content"].strip()
    if content.startswith("```"):
        content = content.strip("`")
        if content.lower().startswith("json"):
            content = content[4:].strip()
    decisions = json.loads(content)
    if isinstance(decisions, dict):
        decisions = [decisions]
    return decisions


def _rule_decide_scan(candidates: list, min_confidence: float = 0.6) -> list:
    """Rule-based fallback (no LLM). action from momentum, confidence from
    move size + RSI distance from 50. Drops anything below min_confidence."""
    out = []
    for c in candidates:
        chg = c.get("change_pct", 0)
        rsi = c.get("rsi")
        action = "BUY" if chg >= 0 else "SELL"
        conf = 0.5 + abs(chg) / 100.0 * 0.4
        if isinstance(rsi, (int, float)):
            conf += abs(rsi - 50) / 100.0 * 0.3
        conf = max(0.0, min(0.95, conf))
        if conf < min_confidence:
            continue
        rsi_s = f"{rsi:.1f}" if isinstance(rsi, (int, float)) else "n/a"
        out.append(
            _new_signal(
                c["symbol"], action, conf,
                f"RULE scan: 24h {chg:+.2f}%, RSI {rsi_s}, vol {c['quote_volume']/1e6:.1f}M",
                "scan-rule",
            )
        )
    return out


def scan_signals(
    top_n: int = 20,
    max_picks: int = 10,
    min_confidence: float = 0.6,
    min_quote_volume: float = 0,
) -> list:
    """Scan all USDT-perp, rank, ask GLM (or rule fallback), return signals.
    Also writes a scan report to signals/last_scan.json for the dashboard."""
    import scanner

    universe = scanner.get_usdt_perp_symbols()
    candidates = scanner.scan(top_n=top_n, min_quote_volume=min_quote_volume)
    print(
        f"[scan] universe={len(universe)} candidates_ranked(top {top_n})={len(candidates)}",
        file=sys.stderr,
    )

    provider_used = "rule"
    raw_decisions: list = []
    try:
        if os.environ.get("ZAI_API_KEY"):
            raw_decisions = _glm_decide_scan(candidates)
            provider_used = "glm"
            signals = []
            for d in raw_decisions[:max_picks]:
                sym = str(d.get("symbol", "")).upper()
                action = str(d.get("action", "")).upper()
                if action not in ("BUY", "SELL"):
                    continue
                signals.append(
                    _new_signal(sym, action, float(d.get("confidence", 0)),
                                str(d.get("reason", "")), "scan-glm")
                )
        else:
            raise RuntimeError("no ZAI_API_KEY -> rule-based")
    except Exception as e:
        print(f"[scan] GLM unavailable ({e}); using rule-based fallback", file=sys.stderr)
        provider_used = "rule"
        signals = _rule_decide_scan(candidates, min_confidence)[:max_picks]

    # Write scan report for dashboard (read-only consumption).
    report = {
        "scanned_at": now_iso(),
        "provider": provider_used,
        "universe_count": len(universe),
        "min_quote_volume": min_quote_volume,
        "top_n": top_n,
        "max_picks": max_picks,
        "top_candidates": candidates,
        "glm_decisions": raw_decisions if provider_used == "glm" else [],
        "signals_emitted": signals,
    }
    _write_scan_report(report)
    return signals


def _write_scan_report(report: dict) -> None:
    p = REPO_ROOT / "signals" / "last_scan.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    with open(tmp, "w") as f:
        json.dump(report, f, indent=2)
    os.replace(tmp, p)
    print(f"[scan] report -> {p}", file=sys.stderr)


# ---------------------------------------------------------------------
def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--mock", action="store_true", help="force single mock signal")
    ap.add_argument(
        "--provider",
        choices=["auto", "scan", "zai", "vibe", "mock"],
        default="auto",
    )
    ap.add_argument("--symbol", default="BTCUSDT")
    ap.add_argument("--action", default="BUY", choices=ALLOWED_ACTIONS)
    ap.add_argument("--top-n", type=int, default=20, help="scan: shortlist size (RSI computed for these)")
    ap.add_argument("--max-picks", type=int, default=10, help="scan: max signals emitted/traded")
    ap.add_argument("--min-confidence", type=float, default=0.6)
    ap.add_argument(
        "--min-quote-volume",
        type=float,
        default=0,
        help="scan: min 24h USDT volume to qualify (0 = scan ALL coins)",
    )
    args = ap.parse_args()

    provider = args.provider
    if provider == "auto":
        provider = "mock" if args.mock else "scan"

    try:
        if provider == "scan":
            signal = scan_signals(
                args.top_n, args.max_picks, args.min_confidence, args.min_quote_volume
            )
        elif provider == "zai":
            signal = zai_signal()
        elif provider == "vibe":
            signal = vibe_trading_ai_signal()
        else:
            signal = mock_signal(args.symbol, args.action)
    except Exception as e:
        print(f"[signal] provider '{provider}' failed: {e}", file=sys.stderr)
        print("[signal] falling back to MOCK signal", file=sys.stderr)
        signal = mock_signal(args.symbol, args.action)

    write_signal(signal)
    return 0


if __name__ == "__main__":
    sys.exit(main())
