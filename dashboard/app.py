"""
dashboard/app.py — READ-ONLY web UI for the trading bot.

Hard rules (CLAUDE.md):
  - Binds 127.0.0.1 ONLY. Never 0.0.0.0. Access via SSH tunnel.
  - Binance key MUST be read-only (futures read, no trade), separate from executor.
  - Only endpoint that writes anything: kill switch toggle (create/delete KILL_SWITCH).
  - Kill switch toggle requires DASHBOARD_TOKEN header, checked every request.
  - Never imports/calls executor order functions (place_futures_order etc).
  - Never writes config.yaml or risk_guard config.

Run: uvicorn dashboard.app:app --host 127.0.0.1 --port 8080
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

from dotenv import load_dotenv
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse

REPO_ROOT = Path(__file__).resolve().parent.parent
EXECUTOR_DIR = REPO_ROOT / "executor"

load_dotenv(Path(__file__).resolve().parent / ".env")

SIGNAL_FILE = REPO_ROOT / "signals" / "latest_signal.json"
SCAN_FILE = REPO_ROOT / "signals" / "last_scan.json"
STATE_FILE = REPO_ROOT / "runtime" / "state.json"
EXECUTOR_LOG = REPO_ROOT / "runtime" / "executor.log"
ORDERS_HISTORY = REPO_ROOT / "runtime" / "orders_history.jsonl"
KLINES_URL = os.environ.get(
    "SCANNER_BASE_URL", "https://fapi.binance.com"
) + "/fapi/v1/klines"
DEFAULT_KILL_SWITCH = REPO_ROOT / "runtime" / "KILL_SWITCH"
KILL_SWITCH_PATH = Path(
    os.environ.get("KILL_SWITCH_FILE", str(DEFAULT_KILL_SWITCH))
)

DASHBOARD_TOKEN = os.environ.get("DASHBOARD_TOKEN", "")

app = FastAPI(title="Trading Bot Dashboard", docs_url=None, redoc_url=None)


# =====================================================================
# Helpers — file reads only (no order API).
# =====================================================================
def _read_json(path: Path) -> Optional[Dict[str, Any]]:
    if not path.exists():
        return None
    try:
        with open(path, "r") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return None


def _kill_switch_active() -> bool:
    return KILL_SWITCH_PATH.exists()


def _token_ok(token: Optional[str]) -> bool:
    if not DASHBOARD_TOKEN:
        # If no token configured, refuse all writes (fail closed).
        return False
    return bool(token) and token == DASHBOARD_TOKEN


def _binance_readonly_positions() -> Dict[str, Any]:
    """Optional: fetch live positions if a SEPARATE read-only key is set.
    Falls back gracefully (empty) if not configured. NEVER trades."""
    key = os.environ.get("DASHBOARD_BINANCE_API_KEY")
    secret = os.environ.get("DASHBOARD_BINANCE_API_SECRET")
    testnet = os.environ.get("BINANCE_TESTNET", "true").lower() in ("1", "true", "yes", "on")
    if not key or not secret:
        return {"available": False, "reason": "no read-only dashboard key configured"}
    try:
        from binance.client import Client  # read-only usage only

        client = Client(key, secret, testnet=testnet)
        acct = client.futures_account()
        positions = [
            {
                "symbol": p["symbol"],
                "positionAmt": p["positionAmt"],
                "entryPrice": p["entryPrice"],
                "unRealizedProfit": p["unRealizedProfit"],
                "leverage": p["leverage"],
            }
            for p in acct.get("positions", [])
            if float(p.get("positionAmt", 0)) != 0.0
        ]
        return {"available": True, "testnet": testnet, "positions": positions}
    except Exception as e:
        return {"available": False, "reason": f"read-only fetch failed: {e}"}


def _executor_status(poll_interval: int = 30) -> Dict[str, Any]:
    """Infer executor liveness from executor.log mtime (read-only).
    'alive' = log written within 2x poll interval. Not a process check —
    just a heartbeat proxy."""
    import time as _time

    if not EXECUTOR_LOG.exists():
        return {"available": False, "alive": False, "reason": "no executor log yet"}
    try:
        mtime = EXECUTOR_LOG.stat().st_mtime
        age = _time.time() - mtime
        # Read last few lines for recent activity.
        with open(EXECUTOR_LOG, "rb") as f:
            tail = f.read()[-2000:].decode("utf-8", errors="replace").strip().splitlines()
        return {
            "available": True,
            "alive": age < (poll_interval * 2 + 30),
            "last_log_age_seconds": round(age, 0),
            "last_lines": tail[-5:],
        }
    except OSError as e:
        return {"available": False, "alive": False, "reason": str(e)}


def _orders_history(limit: int = 20) -> list:
    """Read last `limit` orders from orders_history.jsonl (newest first)."""
    if not ORDERS_HISTORY.exists():
        return []
    out = []
    try:
        with open(ORDERS_HISTORY, "r") as f:
            lines = f.readlines()
    except OSError:
        return []
    for line in reversed(lines):
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
        if len(out) >= limit:
            break
    return out


def _fetch_klines(symbol: str, interval: str = "1h", limit: int = 100) -> Dict[str, Any]:
    """PUBLIC market data only (no key, no trading). Returns close series for chart."""
    import requests

    r = requests.get(
        KLINES_URL,
        params={"symbol": symbol.upper(), "interval": interval, "limit": int(limit)},
        timeout=15,
    )
    r.raise_for_status()
    rows = r.json()
    # Each kline: [openTime, o, h, l, c, volume, closeTime, ...]
    series = [
        {"t": k[0], "o": float(k[1]), "h": float(k[2]), "l": float(k[3]), "c": float(k[4]), "v": float(k[5])}
        for k in rows
    ]
    return {"symbol": symbol.upper(), "interval": interval, "candles": series}


# =====================================================================
# Routes
# =====================================================================
@app.get("/api/status")
def status() -> JSONResponse:
    signal = _read_json(SIGNAL_FILE)
    state = _read_json(STATE_FILE)
    poll = int((state or {}).get("poll_interval_seconds", 0) or 30)
    return JSONResponse(
        {
            "time": datetime.now(timezone.utc).isoformat(),
            "kill_switch_active": _kill_switch_active(),
            "signal": signal,
            "last_order": (state or {}).get("last_order"),
            "orders_history": _orders_history(20),
            "state": {
                "processed_run_ids": (state or {}).get("processed_run_ids", []),
                "trades_today": (state or {}).get("trades_today", 0),
                "trades_date": (state or {}).get("trades_date", ""),
            },
            "executor": _executor_status(poll),
            "binance": _binance_readonly_positions(),
        }
    )


@app.get("/api/chart")
def chart(symbol: str, interval: str = "1h", limit: int = 1000) -> JSONResponse:
    """PUBLIC price klines for charting. Read-only, no key, no trading."""
    try:
        if int(limit) > 1000:
            limit = 1000
        return JSONResponse(_fetch_klines(symbol, interval, limit))
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=502)


@app.get("/api/scan")
def scan_report() -> JSONResponse:
    """Last scan report written by the signal generator. Read-only."""
    report = _read_json(SCAN_FILE)
    if report is None:
        return JSONResponse({"available": False, "reason": "no scan run yet"})
    return JSONResponse(report)


@app.post("/api/killswitch")
def toggle_killswitch(
    request: Request,
    enable: Optional[str] = None,
    x_dashboard_token: Optional[str] = Header(default=None, alias="X-Dashboard-Token"),
) -> JSONResponse:
    """Toggle kill switch. enable=true creates the file (blocks trading);
    enable=false deletes it (resumes trading). Token required."""
    if not _token_ok(x_dashboard_token):
        raise HTTPException(status_code=401, detail="invalid or missing X-Dashboard-Token")

    # Determine desired state: query param ?enable=true|false, else body, else toggle.
    desired: Optional[bool]
    if enable is not None:
        desired = enable.lower() in ("1", "true", "yes", "on")
    else:
        desired = not _kill_switch_active()  # toggle

    if desired:
        KILL_SWITCH_PATH.parent.mkdir(parents=True, exist_ok=True)
        KILL_SWITCH_PATH.write_text(
            f"kill switch set {datetime.now(timezone.utc).isoformat()} "
            f"via dashboard\n"
        )
        action = "enabled (trading blocked)"
    else:
        if KILL_SWITCH_PATH.exists():
            KILL_SWITCH_PATH.unlink()
        action = "disabled (trading resumed)"

    return JSONResponse(
        {"kill_switch_active": _kill_switch_active(), "action": action}
    )


@app.get("/")
def index() -> FileResponse:
    return FileResponse(Path(__file__).resolve().parent / "index.html")


@app.get("/healthz")
def healthz() -> JSONResponse:
    return JSONResponse({"ok": True})


if __name__ == "__main__":
    import uvicorn

    # Bind 127.0.0.1 ONLY — enforced here and in systemd.
    uvicorn.run(app, host="127.0.0.1", port=int(os.environ.get("PORT", "8080")))
