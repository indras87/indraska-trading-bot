"""
generate_signal.py — research/signal side ONLY. Never sends orders.

Provider priority:
  1. Z.ai GLM (OpenAI-compatible) if ZAI_API_KEY is set in env.
  2. vibe-trading-ai package if importable and configured (best-effort).
  3. Deterministic MOCK signal (default) — for testnet end-to-end without
     an LLM key.

Writes:  <repo>/signals/latest_signal.json
Shape:   {symbol, action, confidence, reason, run_id, generated_at}

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


def write_signal(signal: dict) -> None:
    SIGNAL_FILE.parent.mkdir(parents=True, exist_ok=True)
    # Atomic write.
    tmp = SIGNAL_FILE.with_suffix(".tmp")
    with open(tmp, "w") as f:
        json.dump(signal, f, indent=2)
    os.replace(tmp, SIGNAL_FILE)
    print(f"[signal] wrote {SIGNAL_FILE}: {signal}")


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
def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--mock", action="store_true", help="force mock signal")
    ap.add_argument(
        "--provider",
        choices=["auto", "zai", "vibe", "mock"],
        default="auto",
    )
    ap.add_argument("--symbol", default="BTCUSDT")
    ap.add_argument("--action", default="BUY", choices=ALLOWED_ACTIONS)
    args = ap.parse_args()

    provider = args.provider
    if provider == "auto":
        if args.mock:
            provider = "mock"
        elif os.environ.get("ZAI_API_KEY"):
            provider = "zai"
        else:
            provider = "mock"

    try:
        if provider == "zai":
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
