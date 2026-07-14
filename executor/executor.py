"""
executor.py — the ONLY component that talks to Binance Futures API.

Polls signals/latest_signal.json, runs each new signal through RiskGuard,
and ONLY if validation passes calls place_futures_order() + attaches SL/TP.

Hard rules enforced here:
  - Default testnet. testnet flag comes from BINANCE_TESTNET env var.
  - Every order path goes through RiskGuard.validate(). No bypass.
  - Kill switch checked inside validate(); never removed.
  - Leverage/position size come from config.yaml (conservative defaults).

Usage:
    python executor.py            # daemon loop (poll every N seconds)
    python executor.py --once     # process one signal then exit (testing)
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

import yaml
from dotenv import load_dotenv

from risk_guard import RiskGuard

# Resolve repo root = parent of this file's dir.
EXECUTOR_DIR = Path(__file__).resolve().parent
REPO_ROOT = EXECUTOR_DIR.parent


# =====================================================================
# Config / env
# =====================================================================
def load_config() -> Dict[str, Any]:
    cfg_path = EXECUTOR_DIR / "config.yaml"
    with open(cfg_path, "r") as f:
        return yaml.safe_load(f)


def resolve_testnet(config: Dict[str, Any]) -> bool:
    """Env var BINANCE_TESTNET wins. Default stays True. Never hardcoded False."""
    env_val = os.environ.get("BINANCE_TESTNET")
    if env_val is not None:
        return env_val.strip().lower() in ("1", "true", "yes", "on")
    return bool(config.get("binance", {}).get("testnet", True))


# =====================================================================
# Logging
# =====================================================================
def setup_logging(config: Dict[str, Any]) -> logging.Logger:
    log_file = config.get("executor", {}).get("log_file", "executor/executor.log")
    log_path = (REPO_ROOT / log_file) if not os.path.isabs(log_file) else Path(log_file)
    log_path.parent.mkdir(parents=True, exist_ok=True)

    logger = logging.getLogger("executor")
    logger.setLevel(logging.INFO)
    if logger.handlers:
        return logger
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    fh = logging.FileHandler(log_path)
    fh.setFormatter(fmt)
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    logger.addHandler(fh)
    logger.addHandler(sh)
    return logger


# =====================================================================
# Signal / state persistence
# =====================================================================
def read_signal(config: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    sig_path = config.get("executor", {}).get("signal_file", "signals/latest_signal.json")
    p = (REPO_ROOT / sig_path) if not os.path.isabs(sig_path) else Path(sig_path)
    if not p.exists():
        return None
    try:
        with open(p, "r") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        logging.getLogger("executor").error("failed reading signal file %s: %s", p, e)
        return None


def load_state(config: Dict[str, Any]) -> Dict[str, Any]:
    state_path = config.get("executor", {}).get("state_file", "executor/state.json")
    p = (REPO_ROOT / state_path) if not os.path.isabs(state_path) else Path(state_path)
    if not p.exists():
        return {"processed_run_ids": [], "trades_today": 0, "trades_date": ""}
    try:
        with open(p, "r") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return {"processed_run_ids": [], "trades_today": 0, "trades_date": ""}


def save_state(config: Dict[str, Any], state: Dict[str, Any]) -> None:
    state_path = config.get("executor", {}).get("state_file", "executor/state.json")
    p = (REPO_ROOT / state_path) if not os.path.isabs(state_path) else Path(state_path)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    with open(tmp, "w") as f:
        json.dump(state, f, indent=2)
    os.replace(tmp, p)


def rollover_daily(state: Dict[str, Any], now_ts: float) -> Dict[str, Any]:
    today = datetime.fromtimestamp(now_ts, tz=timezone.utc).strftime("%Y-%m-%d")
    if state.get("trades_date") != today:
        state["trades_date"] = today
        state["trades_today"] = 0
    return state


# =====================================================================
# Binance client
# =====================================================================
def init_client(testnet: bool, logger: logging.Logger):
    from binance.client import Client  # imported lazily

    api_key = os.environ.get("BINANCE_API_KEY")
    api_secret = os.environ.get("BINANCE_API_SECRET")
    if not api_key or not api_secret:
        raise RuntimeError(
            "BINANCE_API_KEY / BINANCE_API_SECRET missing from environment"
        )

    client = Client(api_key, api_secret, testnet=testnet)
    # Sanity ping (public endpoint — proves connectivity, NOT auth).
    try:
        client.futures_ping()
    except Exception as e:
        logger.error("Binance futures ping failed (testnet=%s): %s", testnet, e)
        raise
    # Preflight AUTH check (read-only). futures_ping is public, so a bad key
    # only surfaces at order time with a cryptic -2015. Fail fast here with a
    # clear message instead.
    try:
        client.futures_account()
    except Exception as e:
        logger.error(
            "Binance futures AUTH check FAILED (testnet=%s): %s", testnet, e
        )
        logger.error(
            "Key/secret rejected. Common causes: "
            "(1) key generated on the SPOT testnet (testnet.binance.vision) "
            "instead of the FUTURES testnet (testnet.binancefuture.com) — they "
            "are separate systems; (2) key lacks Futures permission; "
            "(3) IP restriction on the key. Regenerate at "
            "https://testnet.binancefuture.com and re-paste into executor/.env."
        )
        raise
    logger.info(
        "Binance Futures client ready + auth OK (testnet=%s, url=%s)",
        testnet,
        client._create_futures_api_uri(""),
    )
    return client


# =====================================================================
# Precision helpers
# =====================================================================
_SYMBOL_INFO_CACHE: Dict[str, Dict[str, Any]] = {}


def get_symbol_info(client, symbol: str) -> Dict[str, Any]:
    if symbol in _SYMBOL_INFO_CACHE:
        return _SYMBOL_INFO_CACHE[symbol]
    info = client.futures_exchange_info()
    for s in info.get("symbols", []):
        if s["symbol"] == symbol:
            filters = {f["filterType"]: f for f in s.get("filters", [])}
            result = {
                "stepSize": float(filters["LOT_SIZE"]["stepSize"]),
                "minQty": float(filters["LOT_SIZE"]["minQty"]),
                "tickSize": float(filters["PRICE_FILTER"]["tickSize"]),
                "pricePrecision": int(s.get("pricePrecision", 2)),
                "quantityPrecision": int(s.get("quantityPrecision", 3)),
            }
            _SYMBOL_INFO_CACHE[symbol] = result
            return result
    raise RuntimeError(f"symbol {symbol} not found in exchange info")


def round_step(value: float, step: float) -> float:
    if step <= 0:
        return value
    steps = round(value / step)
    return round(steps * step, 10)


# =====================================================================
# Order placement — the single order entrypoint.
# Goes through RiskGuard.validate() in process_signal() BEFORE this is called.
# =====================================================================
def place_futures_order(
    client,
    logger: logging.Logger,
    config: Dict[str, Any],
    signal: Dict[str, Any],
) -> Dict[str, Any]:
    """Place MARKET order + SL + TP. Returns dict describing the result."""
    trading = config["trading"]
    symbol = str(signal["symbol"]).upper()
    action = str(signal["action"]).upper()
    side = "BUY" if action == "BUY" else "SELL"
    leverage = int(trading["leverage"])
    notional_usdt = float(trading["position_size_usdt"])
    sl_pct = float(trading["sl_percent"])
    tp_pct = float(trading["tp_percent"])

    sinfo = get_symbol_info(client, symbol)

    # Mark price for notional->qty and SL/TP reference.
    mark = client.futures_mark_price(symbol=symbol)
    entry_price = float(mark["markPrice"])
    if entry_price <= 0:
        raise RuntimeError(f"invalid mark price for {symbol}: {entry_price}")

    # Set leverage (per-symbol, isolated margin type for predictability).
    try:
        client.futures_change_leverage(symbol=symbol, leverage=leverage)
        logger.info("leverage set %s %dx", symbol, leverage)
    except Exception as e:
        # Often "No need to change leverage" — log, continue.
        logger.info("change_leverage %s note: %s", symbol, e)
    try:
        client.futures_change_margin_type(symbol=symbol, marginType="ISOLATED")
        logger.info("margin type ISOLATED for %s", symbol)
    except Exception as e:
        logger.info("margin_type %s note: %s", symbol, e)

    # Quantity from notional.
    raw_qty = notional_usdt / entry_price
    quantity = round_step(raw_qty, sinfo["stepSize"])
    if quantity < sinfo["minQty"]:
        raise RuntimeError(
            f"quantity {quantity} below minQty {sinfo['minQty']} for {symbol} "
            f"(notional {notional_usdt} USDT too small at price {entry_price})"
        )

    # SL / TP stop prices.
    if side == "BUY":
        sl_price = entry_price * (1 - sl_pct / 100.0)
        tp_price = entry_price * (1 + tp_pct / 100.0)
    else:
        sl_price = entry_price * (1 + sl_pct / 100.0)
        tp_price = entry_price * (1 - tp_pct / 100.0)
    sl_price = round_step(sl_price, sinfo["tickSize"])
    tp_price = round_step(tp_price, sinfo["tickSize"])

    logger.info(
        "ORDER %s %s qty=%s entry~%s SL=%s TP=%s",
        side, symbol, quantity, entry_price, sl_price, tp_price,
    )

    # 1) Entry market order.
    entry_order = client.futures_create_order(
        symbol=symbol,
        side=side,
        type="MARKET",
        quantity=quantity,
    )
    entry_id = entry_order.get("orderId")
    logger.info("entry order filled orderId=%s status=%s", entry_id, entry_order.get("status"))

    # 2) Stop loss (closePosition=True closes the whole position).
    sl_order = client.futures_create_order(
        symbol=symbol,
        side="SELL" if side == "BUY" else "BUY",
        type="STOP_MARKET",
        stopPrice=sl_price,
        closePosition=True,
        workingType="MARK_PRICE",
    )
    logger.info("SL placed orderId=%s stopPrice=%s", sl_order.get("orderId"), sl_price)

    # 3) Take profit.
    tp_order = client.futures_create_order(
        symbol=symbol,
        side="SELL" if side == "BUY" else "BUY",
        type="TAKE_PROFIT_MARKET",
        stopPrice=tp_price,
        closePosition=True,
        workingType="MARK_PRICE",
    )
    logger.info("TP placed orderId=%s stopPrice=%s", tp_order.get("orderId"), tp_price)

    return {
        "symbol": symbol,
        "side": side,
        "quantity": quantity,
        "entry_price": entry_price,
        "sl_price": sl_price,
        "tp_price": tp_price,
        "leverage": leverage,
        "entry_order_id": entry_id,
        "sl_order_id": sl_order.get("orderId"),
        "tp_order_id": tp_order.get("orderId"),
        "status": "filled",
        "executed_at": datetime.now(timezone.utc).isoformat(),
        "run_id": signal.get("run_id"),
    }


# =====================================================================
# Signal processing — the gate.
# =====================================================================
def process_signal(
    client,
    logger: logging.Logger,
    config: Dict[str, Any],
    guard: RiskGuard,
    signal: Dict[str, Any],
    state: Dict[str, Any],
    now_ts: float | None = None,
) -> Dict[str, Any]:
    if now_ts is None:
        now_ts = time.time()
    state = rollover_daily(state, now_ts)

    run_id = signal.get("run_id", "unknown")
    ok, reason = guard.validate(signal, state, now_ts=now_ts)
    if not ok:
        logger.info("BLOCKED run_id=%s reason=%s", run_id, reason)
        return {"run_id": run_id, "accepted": False, "reason": reason}

    logger.info("ACCEPTED run_id=%s — placing order", run_id)
    try:
        result = place_futures_order(client, logger, config, signal)
    except Exception as e:
        logger.error("order FAILED run_id=%s: %s", run_id, e)
        return {"run_id": run_id, "accepted": True, "placed": False, "error": str(e)}

    # Update state only after a successful placement.
    state.setdefault("processed_run_ids", []).append(run_id)
    state["trades_today"] = int(state.get("trades_today", 0)) + 1
    state["last_order"] = result
    logger.info("DONE run_id=%s entry=%s SL=%s TP=%s",
                run_id, result["entry_order_id"], result["sl_order_id"], result["tp_order_id"])
    return {"run_id": run_id, "accepted": True, "placed": True, "result": result}


# =====================================================================
# Main loop
# =====================================================================
def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--once", action="store_true", help="process one signal then exit")
    args = parser.parse_args()

    load_dotenv(EXECUTOR_DIR / ".env")
    config = load_config()
    logger = setup_logging(config)

    testnet = resolve_testnet(config)
    if not testnet:
        # Refuse to silently run mainnet from an automated session.
        logger.error(
            "REFUSING: BINANCE_TESTNET is not true. Automated executor runs "
            "testnet only. Set BINANCE_TESTNET=true for safety."
        )
        return 2

    guard = RiskGuard(config)
    client = init_client(testnet, logger)

    poll = int(config.get("executor", {}).get("poll_interval_seconds", 30))
    last_run_id = None

    logger.info("executor started (testnet=%s, poll=%ss)", testnet, poll)
    while True:
        try:
            now_ts = time.time()
            signal = read_signal(config)
            if signal is None:
                logger.debug("no signal file")
            else:
                run_id = signal.get("run_id")
                if run_id != last_run_id:
                    state = load_state(config)
                    outcome = process_signal(
                        client, logger, config, guard, signal, state, now_ts=now_ts
                    )
                    save_state(config, state)
                    last_run_id = run_id
                    if args.once:
                        logger.info("--once mode: exiting after one signal")
                        return 0 if outcome.get("placed") or not outcome.get("accepted") else 1
        except KeyboardInterrupt:
            logger.info("interrupted, exiting")
            return 0
        except Exception as e:
            logger.error("loop error: %s", e)

        if args.once:
            logger.info("--once mode: no actionable signal, exiting")
            return 0
        time.sleep(poll)


if __name__ == "__main__":
    sys.exit(main())
