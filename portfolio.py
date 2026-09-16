"""
Paper-trading ledger. Tracks virtual cash + open positions + closed trade
history + equity curve, persisted to a JSON state file so a restart doesn't
lose the book. This is intentionally simple (no DB) -- it's a paper engine.
"""
import json
import os
from datetime import datetime, timezone
from . import config

_LOCK_STATE = None


def _default_state():
    now = datetime.now(timezone.utc)
    return {
        "mode": "LIVE" if config.LIVE_TRADING else "PAPER",
        "cash_usdt": config.STARTING_EQUITY_USDT,
        "starting_equity": config.STARTING_EQUITY_USDT,
        "day_start_equity": config.STARTING_EQUITY_USDT,
        "day_start_date": now.date().isoformat(),
        "open_positions": [],   # list of dicts, single-symbol strategies
        "open_pairs": [],       # list of dicts, pairs strategy (two legs)
        "closed_trades": [],
        "equity_history": [{"time": now.isoformat(), "equity": config.STARTING_EQUITY_USDT}],
        "kill_switch": {},
        "events": [],
        "halted_today": False,
        "last_cycle": None,
    }


def load_state() -> dict:
    global _LOCK_STATE
    path = config.STATE_FILE
    if os.path.exists(path):
        try:
            with open(path, "r") as f:
                state = json.load(f)
            # backfill any new keys added after this state file was created
            for k, v in _default_state().items():
                state.setdefault(k, v)
            return state
        except Exception:
            pass
    return _default_state()


def save_state(state: dict):
    path = config.STATE_FILE
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(state, f, indent=2, default=str)
    os.replace(tmp, path)


def maybe_roll_day(state: dict):
    today = datetime.now(timezone.utc).date().isoformat()
    if state.get("day_start_date") != today:
        state["day_start_date"] = today
        state["day_start_equity"] = mark_to_market(state)
        state["halted_today"] = False


def mark_to_market(state: dict, price_lookup=None) -> float:
    """Equity = cash + value of open positions/pairs at last-seen prices.
    price_lookup(symbol) -> float, optional live re-pricing."""
    equity = state["cash_usdt"]
    for p in state["open_positions"]:
        price = price_lookup(p["symbol"]) if price_lookup else p["entry"]
        equity += p["qty"] * price
    for pr in state["open_pairs"]:
        long_price = price_lookup(pr["long_symbol"]) if price_lookup else pr["long_entry"]
        short_price = price_lookup(pr["short_symbol"]) if price_lookup else pr["short_entry"]
        equity += pr["long_qty"] * long_price
        equity += pr["short_qty"] * (pr["short_entry"] - short_price)  # short PnL added to cash-equiv
    return equity


def record_equity(state: dict, equity: float):
    state["equity_history"].append({"time": datetime.now(timezone.utc).isoformat(), "equity": equity})
    state["equity_history"] = state["equity_history"][-2000:]  # cap history size


def close_single(state: dict, position: dict, exit_price: float, exit_kind: str, reason: str):
    pnl = (exit_price - position["entry"]) * position["qty"]
    state["cash_usdt"] += position["qty"] * exit_price
    trade = {
        **position,
        "exit_price": exit_price,
        "exit_kind": exit_kind,
        "exit_reason": reason,
        "closed_at": datetime.now(timezone.utc).isoformat(),
        "pnl": pnl,
    }
    state["closed_trades"].append(trade)
    state["closed_trades"] = state["closed_trades"][-500:]
    state["open_positions"] = [p for p in state["open_positions"] if p is not position]


def open_single(state: dict, signal: dict, qty: float):
    position = {
        **signal,
        "qty": qty,
        "opened_at": datetime.now(timezone.utc).isoformat(),
    }
    state["cash_usdt"] -= qty * signal["entry"]
    state["open_positions"].append(position)
    return position


def close_pair(state: dict, pair_pos: dict, long_exit: float, short_exit: float, exit_kind: str, reason: str):
    long_pnl = (long_exit - pair_pos["long_entry"]) * pair_pos["long_qty"]
    short_pnl = (pair_pos["short_entry"] - short_exit) * pair_pos["short_qty"]
    pnl = long_pnl + short_pnl
    state["cash_usdt"] += pair_pos["long_qty"] * long_exit
    state["cash_usdt"] += pair_pos["short_qty"] * pair_pos["short_entry"]  # release short collateral
    state["cash_usdt"] += short_pnl  # settle short pnl
    trade = {
        "strategy": "pairs",
        "pair": pair_pos["pair"],
        "long_symbol": pair_pos["long_symbol"],
        "short_symbol": pair_pos["short_symbol"],
        "long_entry": pair_pos["long_entry"],
        "short_entry": pair_pos["short_entry"],
        "long_exit": long_exit,
        "short_exit": short_exit,
        "exit_kind": exit_kind,
        "exit_reason": reason,
        "opened_at": pair_pos["opened_at"],
        "closed_at": datetime.now(timezone.utc).isoformat(),
        "pnl": pnl,
    }
    state["closed_trades"].append(trade)
    state["closed_trades"] = state["closed_trades"][-500:]
    state["open_pairs"] = [p for p in state["open_pairs"] if p is not pair_pos]


def open_pair(state: dict, signal: dict, long_qty: float, short_qty: float):
    pair_pos = {
        **signal,
        "long_qty": long_qty,
        "short_qty": short_qty,
        "opened_at": datetime.now(timezone.utc).isoformat(),
    }
    state["cash_usdt"] -= long_qty * signal["long_entry"]
    state["cash_usdt"] -= short_qty * signal["short_entry"]  # collateral set aside for the short leg
    state["open_pairs"].append(pair_pos)
    return pair_pos
