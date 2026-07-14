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
STATE_FILE = EXECUTOR_DIR / "state.json"
DEFAULT_KILL_SWITCH = REPO_ROOT / "KILL_SWITCH"
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


# =====================================================================
# Routes
# =====================================================================
@app.get("/api/status")
def status() -> JSONResponse:
    signal = _read_json(SIGNAL_FILE)
    state = _read_json(STATE_FILE)
    return JSONResponse(
        {
            "time": datetime.now(timezone.utc).isoformat(),
            "kill_switch_active": _kill_switch_active(),
            "signal": signal,
            "last_order": (state or {}).get("last_order"),
            "state": {
                "processed_run_ids": (state or {}).get("processed_run_ids", []),
                "trades_today": (state or {}).get("trades_today", 0),
                "trades_date": (state or {}).get("trades_date", ""),
            },
            "binance": _binance_readonly_positions(),
        }
    )


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
