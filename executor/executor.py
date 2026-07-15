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
def read_signals(config: Dict[str, Any]) -> Optional[list]:
    """Read signal file and normalize to a LIST of signal dicts.
    Accepts: a single signal dict, a JSON array, or {"signals":[...]}."""
    sig_path = config.get("executor", {}).get("signal_file", "signals/latest_signal.json")
    p = (REPO_ROOT / sig_path) if not os.path.isabs(sig_path) else Path(sig_path)
    if not p.exists():
        return None
    try:
        with open(p, "r") as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        logging.getLogger("executor").error("failed reading signal file %s: %s", p, e)
        return None
    if isinstance(data, dict):
        if isinstance(data.get("signals"), list):
            return data["signals"]
        return [data]
    if isinstance(data, list):
        return data
    return None


# Back-compat alias.
def read_signal(config: Dict[str, Any]) -> Optional[list]:
    return read_signals(config)


def load_state(config: Dict[str, Any]) -> Dict[str, Any]:
    # Persistence is now SQLite (runtime/bot.db). Delegated to store.py.
    # risk_guard reads the dict shape returned here — kept identical.
    import store
    return store.load_state(config)


def save_state(config: Dict[str, Any], state: Dict[str, Any]) -> None:
    import store
    store.save_state(config, state)


def append_order_history(config: Dict[str, Any], result: Dict[str, Any]) -> None:
    """Insert a filled-order record into the DB for dashboard history."""
    try:
        import store
        store.append_order(config, result)
    except Exception as e:
        logging.getLogger("executor").warning("could not write orders_history: %s", e)


def rollover_daily(state: Dict[str, Any], now_ts: float) -> Dict[str, Any]:
    today = datetime.fromtimestamp(now_ts, tz=timezone.utc).strftime("%Y-%m-%d")
    if state.get("trades_date") != today:
        state["trades_date"] = today
        state["trades_today"] = 0
    return state


def _scan_pause_path(config: Dict[str, Any]) -> Path:
    rel = config.get("executor", {}).get("scan_pause_file", "runtime/SCAN_PAUSED")
    return Path(rel) if os.path.isabs(rel) else (REPO_ROOT / rel)


def sync_scan_pause(
    config: Dict[str, Any],
    state: Dict[str, Any],
    guard: "RiskGuard",
    logger: logging.Logger,
    now_ts: float | None = None,
) -> None:
    """Create the scan-pause flag file when the daily trade limit is reached, so
    the scanner (vibe-trading) skips scanning and saves LLM quota. Removes it
    once under the limit. Idempotent; logs only on state transitions. Safe to
    call on every loop iteration — this prevents a deadlock where a paused
    scanner never produces a new signal to wake the executor for rollover."""
    if now_ts is None:
        now_ts = time.time()
    today = datetime.fromtimestamp(now_ts, tz=timezone.utc).strftime("%Y-%m-%d")
    trades_today = int(state.get("trades_today", 0)) if state.get("trades_date") == today else 0
    max_trades = int(getattr(guard, "max_daily_trades", 0))

    path = _scan_pause_path(config)
    at_limit = max_trades > 0 and trades_today >= max_trades
    try:
        if at_limit:
            if not path.exists():
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(
                    f"scan paused: daily limit reached {trades_today}/{max_trades} "
                    f"{datetime.now(timezone.utc).isoformat()}\n"
                )
                logger.info(
                    "scan paused: daily limit reached %d/%d trades", trades_today, max_trades
                )
        else:
            if path.exists():
                path.unlink()
                logger.info("scan resumed: under daily limit (%d/%d)", trades_today, max_trades)
    except OSError as e:
        logger.warning("scan_pause flag sync failed: %s", e)


# =====================================================================
# Exit reconciliation — detect closed positions, record outcome locally.
# READ-ONLY: queries positions / order status / income history. Never orders.
# =====================================================================
def _to_ms(iso_str: Any) -> int | None:
    """ISO 8601 string -> epoch milliseconds (for Binance startTime params)."""
    if not iso_str:
        return None
    try:
        return int(datetime.fromisoformat(str(iso_str).replace("Z", "+00:00")).timestamp() * 1000)
    except (ValueError, TypeError):
        return None


def _resolve_exit(client, logger: logging.Logger, symbol: str, sl_id: Any, tp_id: Any):
    """Return (exit_type, exit_price). exit_type in {TP, SL, MANUAL, UNKNOWN}.
    Determines which conditional order filled by checking recent order status."""
    try:
        recent = client.futures_get_all_orders(symbol=symbol, limit=20)
    except Exception as e:
        logger.warning("reconcile: get_all_orders %s failed: %s", symbol, e)
        return "UNKNOWN", None
    by_id = {str(o.get("orderId")): o for o in (recent or [])}
    sl = by_id.get(str(sl_id)) if sl_id else None
    tp = by_id.get(str(tp_id)) if tp_id else None
    if tp and tp.get("status") == "FILLED":
        try:
            return "TP", float(tp.get("avgPrice") or 0) or None
        except (TypeError, ValueError):
            return "TP", None
    if sl and sl.get("status") == "FILLED":
        try:
            return "SL", float(sl.get("avgPrice") or 0) or None
        except (TypeError, ValueError):
            return "SL", None
    # Neither conditional filled -> closed another way (manual / liquidation).
    return "MANUAL", None


def _resolve_pnl(client, logger: logging.Logger, symbol: str, executed_at: Any) -> float | None:
    """Realized PnL (USDT) for this position from REALIZED_PNL income records
    after entry time. v1 limit: if the same symbol was re-entered before this
    close, this may aggregate multiple closes (documented limitation)."""
    start_ms = _to_ms(executed_at)
    if start_ms is None:
        return None
    try:
        inc = client.futures_income_history(
            symbol=symbol, incomeType="REALIZED_PNL", startTime=start_ms, limit=10
        )
    except Exception as e:
        logger.warning("reconcile: income %s failed: %s", symbol, e)
        return None
    vals = [
        float(r.get("income", 0) or 0)
        for r in (inc or [])
        if int(r.get("time", 0) or 0) >= start_ms
    ]
    if not vals:
        return 0.0
    return round(sum(vals), 6)


def reconcile_exits(client, logger: logging.Logger, config: Dict[str, Any]) -> None:
    """Poll open orders; for any whose position is gone, resolve the exit
    (which conditional filled + realized PnL + outcome) and mark it closed
    in the local DB. Called every loop iteration; network-safe (per-order
    try/except so one failure doesn't abort the pass)."""
    import store

    try:
        open_orders = store.open_orders(config)
    except Exception as e:
        logger.warning("reconcile: open_orders read failed: %s", e)
        return
    if not open_orders:
        return

    try:
        positions = client.futures_position_information()
    except Exception as e:
        logger.warning("reconcile: position fetch failed: %s", e)
        return

    open_syms = {
        p.get("symbol")
        for p in (positions or [])
        if abs(float(p.get("positionAmt", 0) or 0)) > 0
    }

    for o in open_orders:
        sym = o.get("symbol")
        if sym in open_syms:
            continue  # position still open
        # Position gone: cancel any orphan conditional orders before
        # resolving the exit, so they cannot trigger -4130 later.
        _cancel_open_orders(client, logger, sym)
        try:
            exit_type, exit_price = _resolve_exit(
                client, logger, sym, o.get("sl_order_id"), o.get("tp_order_id")
            )
            realized = _resolve_pnl(client, logger, sym, o.get("executed_at"))
        except Exception as e:
            logger.warning("reconcile: resolve failed %s: %s", sym, e)
            continue
        pnl = realized if realized is not None else 0.0
        outcome = "win" if pnl > 0 else ("loss" if pnl < 0 else "breakeven")
        closed_at = datetime.now(timezone.utc).isoformat()
        try:
            store.mark_closed(
                config,
                o["id"],
                exit_type=exit_type,
                realized_pnl=realized,
                outcome=outcome,
                closed_at=closed_at,
                exit_price=exit_price,
            )
            logger.info(
                "CLOSED run_id=%s symbol=%s exit=%s outcome=%s pnl=%s",
                o.get("run_id") or o.get("id"), sym, exit_type, outcome, realized,
            )
        except Exception as e:
            logger.error("reconcile: mark_closed failed %s: %s", sym, e)


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


def _cond_id(order: Dict[str, Any]) -> Any:
    """Conditional (STOP/TP) orders return `algoId` on the Futures Demo
    testnet and `orderId` elsewhere. Handle both."""
    return order.get("algoId") or order.get("orderId")


def _cond_status(order: Dict[str, Any]) -> Any:
    return order.get("algoStatus") or order.get("status")


def _cond_stop(order: Dict[str, Any]) -> Any:
    return order.get("triggerPrice") or order.get("stopPrice")


def _cancel_open_orders(client, logger: logging.Logger, symbol: str) -> None:
    """Cancel all open (conditional SL/TP) orders for a symbol.

    Clears leftover conditional orders from a previously-closed position so
    that placing fresh SL/TP does not hit Binance API error -4130
    ('An open stop or take profit order with GTE and closePosition in the
    direction is existing'). Safe: cancels pending orders only, never
    affects an open position.
    """
    try:
        client.futures_cancel_all_open_orders(symbol=symbol)
        logger.info("cleared open orders for %s", symbol)
    except Exception as e:
        # Common: 'No open orders to cancel'. Log, continue.
        logger.warning("cancel_open_orders %s note: %s", symbol, e)


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

    # Clear any leftover conditional SL/TP from a prior position on this
    # symbol before placing fresh ones (prevents API error -4130).
    _cancel_open_orders(client, logger, symbol)

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
    sl_id = _cond_id(sl_order)
    logger.info("SL placed id=%s stopPrice=%s status=%s", sl_id, sl_price, _cond_status(sl_order))

    # 3) Take profit.
    tp_order = client.futures_create_order(
        symbol=symbol,
        side="SELL" if side == "BUY" else "BUY",
        type="TAKE_PROFIT_MARKET",
        stopPrice=tp_price,
        closePosition=True,
        workingType="MARK_PRICE",
    )
    tp_id = _cond_id(tp_order)
    logger.info("TP placed id=%s stopPrice=%s status=%s", tp_id, tp_price, _cond_status(tp_order))

    return {
        "symbol": symbol,
        "side": side,
        "quantity": quantity,
        "entry_price": entry_price,
        "sl_price": sl_price,
        "tp_price": tp_price,
        "leverage": leverage,
        "entry_order_id": entry_id,
        "sl_order_id": sl_id,
        "tp_order_id": tp_id,
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
    # NOTE: last_order is no longer stored in state — the dashboard derives it
    # from the latest row of the orders table (single source of truth).
    state.setdefault("processed_run_ids", []).append(run_id)
    state["trades_today"] = int(state.get("trades_today", 0)) + 1
    append_order_history(config, result)
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
    max_signals_per_run = int(config.get("executor", {}).get("max_signals_per_run", 10))
    last_batch: tuple | None = None

    logger.info(
        "executor started (testnet=%s, poll=%ss, max_signals_per_run=%d)",
        testnet, poll, max_signals_per_run,
    )
    while True:
        try:
            now_ts = time.time()
            # Load + roll over state every iteration so the scan-pause flag is
            # synced even when no new signal arrives (prevents rollover deadlock).
            state = rollover_daily(load_state(config), now_ts)
            placed_any = False
            signals = read_signals(config)
            if signals is None:
                logger.debug("no signal file")
            else:
                batch = tuple(s.get("run_id") for s in signals)
                if batch != last_batch:
                    last_batch = batch
                    logger.info("new signal batch: %d signal(s)", len(signals))
                    for signal in signals[:max_signals_per_run]:
                        outcome = process_signal(
                            client, logger, config, guard, signal, state, now_ts=now_ts
                        )
                        if outcome.get("placed"):
                            placed_any = True
            # Sync scan-pause flag after any potential trades, then persist.
            sync_scan_pause(config, state, guard, logger, now_ts=now_ts)
            save_state(config, state)
            # Detect closed positions and record outcomes locally (read-only).
            reconcile_exits(client, logger, config)
            if args.once:
                logger.info("--once mode: exiting after batch")
                return 0 if placed_any else 0
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
