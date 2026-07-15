"""
dashboard/app.py — READ-ONLY web UI for the trading bot.

Hard rules (CLAUDE.md):
  - Binds 127.0.0.1 ONLY. Never 0.0.0.0. Public access is via a reverse proxy
    (Caddy/Nginx) that terminates HTTPS on the same host and proxies to
    127.0.0.1:8080 — the dashboard process itself never binds a public iface.
  - When exposed publicly, HTTP Basic Auth MUST be enabled
    (DASHBOARD_AUTH_USER / DASHBOARD_AUTH_PASS). Without it, auth is disabled
    and the app is safe ONLY behind an SSH tunnel.
  - Binance key MUST be read-only (futures read, no trade), separate from executor.
  - Only endpoint that writes anything: kill switch toggle (create/delete KILL_SWITCH).
  - Kill switch toggle requires DASHBOARD_TOKEN header, checked every request
    (second factor on top of Basic Auth).
  - Never imports/calls executor order functions (place_futures_order etc).
  - Never writes config.yaml or risk_guard config.

Run: uvicorn dashboard.app:app --host 127.0.0.1 --port 8080
"""

from __future__ import annotations

import base64
import json
import os
import secrets
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from dotenv import load_dotenv
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse

REPO_ROOT = Path(__file__).resolve().parent.parent
EXECUTOR_DIR = REPO_ROOT / "executor"

# Import the executor's read-only persistence layer (store.py). This module
# contains NO order/trade functions — only SQLite reads/writes of state +
# order history. Safe to import from the read-only dashboard.
sys.path.insert(0, str(EXECUTOR_DIR))
import store  # noqa: E402

load_dotenv(Path(__file__).resolve().parent / ".env")

SIGNAL_FILE = REPO_ROOT / "signals" / "latest_signal.json"
SCAN_FILE = REPO_ROOT / "signals" / "last_scan.json"
EXECUTOR_LOG = REPO_ROOT / "runtime" / "executor.log"
KLINES_URL = os.environ.get(
    "SCANNER_BASE_URL", "https://fapi.binance.com"
) + "/fapi/v1/klines"
DEFAULT_KILL_SWITCH = REPO_ROOT / "runtime" / "KILL_SWITCH"
KILL_SWITCH_PATH = Path(
    os.environ.get("KILL_SWITCH_FILE", str(DEFAULT_KILL_SWITCH))
)

DASHBOARD_TOKEN = os.environ.get("DASHBOARD_TOKEN", "")

# HTTP Basic Auth — gates ALL access when deployed behind a public reverse proxy.
# Backward compatible: if unset, auth is disabled (local/tunnel use only).
DASHBOARD_AUTH_USER = os.environ.get("DASHBOARD_AUTH_USER", "")
DASHBOARD_AUTH_PASS = os.environ.get("DASHBOARD_AUTH_PASS", "")
BASIC_AUTH_ENABLED = bool(DASHBOARD_AUTH_USER and DASHBOARD_AUTH_PASS)

app = FastAPI(title="Trading Bot Dashboard", docs_url=None, redoc_url=None)


def _basic_auth_ok(authorization: Optional[str]) -> bool:
    """Constant-time check of HTTP Basic credentials. Returns False if auth
    is disabled OR credentials are missing/wrong."""
    if not BASIC_AUTH_ENABLED:
        return True
    if not authorization or not authorization.startswith("Basic "):
        return False
    try:
        decoded = base64.b64decode(authorization[6:]).decode("utf-8")
    except Exception:
        return False
    user, sep, pw = decoded.partition(":")
    if not sep:
        return False
    return secrets.compare_digest(user, DASHBOARD_AUTH_USER) and secrets.compare_digest(
        pw, DASHBOARD_AUTH_PASS
    )


@app.middleware("http")
async def _require_basic_auth(request: Request, call_next):
    # /healthz stays open for reverse-proxy health checks (returns no secrets).
    if request.url.path == "/healthz" or _basic_auth_ok(request.headers.get("authorization")):
        return await call_next(request)
    return JSONResponse(
        {"detail": "authentication required"},
        status_code=401,
        headers={"WWW-Authenticate": 'Basic realm="tbot-dashboard"'},
    )


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


def _readonly_client():
    """Build a Binance client from the SEPARATE dashboard read-only key.
    Returns (client, testnet) or (None, None) if not configured. NEVER trades."""
    key = os.environ.get("DASHBOARD_BINANCE_API_KEY")
    secret = os.environ.get("DASHBOARD_BINANCE_API_SECRET")
    testnet = os.environ.get("BINANCE_TESTNET", "true").lower() in ("1", "true", "yes", "on")
    if not key or not secret:
        return None, None
    from binance.client import Client  # read-only usage only

    return Client(key, secret, testnet=testnet), testnet


def _binance_readonly_account(trade_limit: int = 100) -> Dict[str, Any]:
    """Optional: fetch live positions + realized-PnL trade history if a SEPARATE
    read-only key is set. Falls back gracefully (empty) if not configured.
    NEVER trades. Uses futures_income_history(REALIZED_PNL) — each record is a
    closing fill, so sum(income)=total realized PnL, count>0=winning trades."""
    client, testnet = _readonly_client()
    if client is None:
        return {"available": False, "reason": "no read-only dashboard key configured"}

    result: Dict[str, Any] = {"available": True, "testnet": testnet}
    try:
        acct = client.futures_account()
        result["positions"] = [
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
    except Exception as e:
        result["positions"] = []
        result["positions_error"] = f"positions fetch failed: {e}"

    # Realized PnL trade history (income history, REALIZED_PNL only).
    try:
        income = client.futures_income_history(
            incomeType="REALIZED_PNL", limit=int(trade_limit)
        )
        trades = []
        total_pnl = 0.0
        wins = 0
        losses = 0
        gross_profit = 0.0
        gross_loss = 0.0
        for r in income or []:
            amt = float(r.get("income", 0) or 0)
            total_pnl += amt
            if amt > 0:
                wins += 1
                gross_profit += amt
            elif amt < 0:
                losses += 1
                gross_loss += abs(amt)
            trades.append(
                {
                    "symbol": r.get("symbol"),
                    "income": round(amt, 6),
                    "tradeId": r.get("tradeId"),
                    "incomeType": r.get("incomeType"),
                    "time": datetime.fromtimestamp(
                        r.get("time", 0) / 1000, tz=timezone.utc
                    ).isoformat()
                    if r.get("time")
                    else None,
                }
            )
        # Newest first.
        trades.reverse()
        decided = wins + losses
        result["pnl"] = {
            "total_realized": round(total_pnl, 6),
            "trade_count": decided,
            "wins": wins,
            "losses": losses,
            "win_rate": round(wins / decided * 100, 2) if decided else 0.0,
            "gross_profit": round(gross_profit, 6),
            "gross_loss": round(gross_loss, 6),
            "profit_factor": round(gross_profit / gross_loss, 4)
            if gross_loss > 0
            else None,
            "trades": trades,
        }
    except Exception as e:
        result["pnl"] = {"trade_count": 0, "trades": []}
        result["pnl_error"] = f"income history fetch failed: {e}"
    return result


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


def _orders_history(limit: int = 20) -> List[Dict[str, Any]]:
    """Read last `limit` orders from the SQLite DB (newest first).
    Read-only; returns [] if the DB does not exist yet."""
    if not store.db_path().exists():
        return []
    try:
        return store.recent_orders(limit=limit)
    except Exception as e:
        return [{"error": f"orders read failed: {e}"}]


def _executor_state() -> Dict[str, Any]:
    """Read executor state (meta + processed_run_ids) from the DB.
    Read-only; returns zeros if the DB does not exist yet (never creates it)."""
    if not store.db_path().exists():
        return {"processed_run_ids": [], "trades_today": 0, "trades_date": ""}
    try:
        return store.load_state()
    except Exception as e:
        return {"processed_run_ids": [], "trades_today": 0, "trades_date": "", "error": str(e)}


def _latest_order() -> Optional[Dict[str, Any]]:
    """Most recent order from the DB, or None (single source of truth)."""
    if not store.db_path().exists():
        return None
    try:
        return store.latest_order()
    except Exception:
        return None


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
    poll = 30  # executor heartbeat threshold (matches config poll_interval_seconds)
    state = _executor_state()
    return JSONResponse(
        {
            "time": datetime.now(timezone.utc).isoformat(),
            "kill_switch_active": _kill_switch_active(),
            "signal": signal,
            "last_order": _latest_order(),
            "orders_history": _orders_history(20),
            "state": {
                "processed_run_ids": state.get("processed_run_ids", []),
                "trades_today": state.get("trades_today", 0),
                "trades_date": state.get("trades_date", ""),
            },
            "executor": _executor_status(poll),
            "binance": _binance_readonly_account(),
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

    if BASIC_AUTH_ENABLED:
        print("[dashboard] HTTP Basic Auth ENABLED — safe for public reverse proxy", file=sys.stderr)
    else:
        print("[dashboard] WARNING: Basic Auth DISABLED — use SSH tunnel only, do NOT expose", file=sys.stderr)
    # Bind 127.0.0.1 ONLY — enforced here and in systemd. Public access via
    # reverse proxy (see dashboard/Caddyfile.example).
    uvicorn.run(app, host="127.0.0.1", port=int(os.environ.get("PORT", "8080")))
