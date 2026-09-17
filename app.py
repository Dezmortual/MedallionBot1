"""
Medallion-Flavored Trading Bot -- SINGLE FILE EDITION.

Everything (config, data feed, strategies, risk engine, kill switch,
paper portfolio, decision engine, dashboard) lives in this ONE file on
purpose: no subfolders to lose when pushing to GitHub via the web UI.

Strategy: mean-reversion core (RSI(2) dips in uptrends) + momentum overlay
+ market-neutral pairs/stat-arb, with 1% risk per trade, no leverage, a
-4% daily loss brake, and an auto kill-switch that disables a strategy
for 48h if its last-20-trades win rate drops below 35%.

IMPORTANT: the background trading cycle does slow network I/O (Binance
klines, possibly slow/blocked depending on server region). That work
NEVER happens while holding the lock a web request needs -- otherwise a
slow/blocked cycle stalls every dashboard request behind it and can take
the whole app down. See _cycle_loop() / api_status() below.

PAPER trading mode by default. Education/research tool -- not financial
advice.

Run:   pip install -r requirements.txt
       python app.py
  or:  gunicorn --workers 1 --threads 4 --timeout 60 --bind 0.0.0.0:$PORT app:app
"""

import copy
import json
import logging
import os
import threading
import time
from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd
import requests
from flask import Flask, jsonify, render_template_string


# ============================== CONFIG ==============================
"""
Central configuration for the Medallion-flavored trading bot.
Everything here is a RULE, not a feeling -- Secret 6: let the robot trade.
"""


# ---------------------------------------------------------------------------
# Universe: a diversified, largely-uncorrelated basket (Secret 5).
# Mean-reversion + momentum run independently on each symbol.
# ---------------------------------------------------------------------------
MR_MOMENTUM_UNIVERSE = [
    "BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT",
    "XRPUSDT", "ADAUSDT", "DOGEUSDT", "AVAXUSDT",
]

# Statistical-arbitrage pairs: correlated "twins" whose spread should
# mean-revert (Secret 3 + Section 6's Coke/Pepsi example). Market-neutral.
PAIRS_UNIVERSE = [
    ("ETHUSDT", "BTCUSDT"),
    ("SOLUSDT", "ETHUSDT"),
    ("BNBUSDT", "BTCUSDT"),
]

TIMEFRAME = "1h"          # candle interval used for all signals
CANDLE_LOOKBACK = 400      # bars fetched per cycle (needs 200-period MA + buffer)
CYCLE_SECONDS = 900        # 15 minutes between decision cycles

# ---------------------------------------------------------------------------
# Mean-reversion (RULE 3 / Secret 3) -- the core engine
# ---------------------------------------------------------------------------
MR_TREND_MA = 200          # only buy dips when price is above this MA (uptrend filter)
MR_RSI_PERIOD = 2
MR_RSI_ENTRY = 10          # "stretched rubber band" -- buy below this
MR_EXIT_MA = 5             # exit target: revert to this short MA
MR_STOP_LOOKBACK = 20      # stop placed below the recent N-bar low

# ---------------------------------------------------------------------------
# Momentum / trend overlay (RULE 4 / Secret 4) -- ride the wave
# ---------------------------------------------------------------------------
MOM_FAST_MA = 20
MOM_RSI_PERIOD = 14
MOM_RSI_MIN = 50
MOM_RSI_MAX = 70           # avoid buying already-overbought momentum
MOM_STOP_LOOKBACK = 10

# ---------------------------------------------------------------------------
# Pairs / stat-arb (Secret 3 + 5, Section 6)
# ---------------------------------------------------------------------------
PAIRS_ZSCORE_LOOKBACK = 100
PAIRS_ENTRY_Z = 2.0        # enter when spread is this many std devs stretched
PAIRS_EXIT_Z = 0.25        # exit once it has mostly snapped back
PAIRS_STOP_Z = 3.5         # rubber band snapped AND kept going -- bail
PAIRS_NOTIONAL_PCT = 0.02  # % of equity allocated per leg (hedged, so smaller directional risk)

# ---------------------------------------------------------------------------
# Risk engine (RULE 4 / Secret 7) -- automatic brakes, no leverage
# ---------------------------------------------------------------------------
STARTING_EQUITY_USDT = float(os.environ.get("STARTING_EQUITY_USDT", "10000"))
RISK_PER_TRADE_PCT = 0.01       # 1% of equity risked per trade
MAX_DAILY_LOSS_PCT = 0.04       # computer says no after -4% in a day
MAX_CONCURRENT_POSITIONS = 8    # diversification cap (Secret 5 / Rule 5)
MAX_POSITIONS_PER_STRATEGY = 4
MAX_NOTIONAL_PCT_PER_TRADE = 0.15  # hard cap: no single trade can eat more than 15% of equity, however tight the stop
LEVERAGE = 1.0                   # Rule 4: beginners get NO leverage. Ever, here.

# ---------------------------------------------------------------------------
# Kill switch (Secret 4: "when a style stops working, switch it off")
# ---------------------------------------------------------------------------
KILL_SWITCH_LOOKBACK_TRADES = 20
KILL_SWITCH_MIN_WINRATE = 0.35
KILL_SWITCH_COOLDOWN_HOURS = 48

# ---------------------------------------------------------------------------
# Live trading (off by default -- paper proves the edge first, Section 11)
# ---------------------------------------------------------------------------
LIVE_TRADING = os.environ.get("LIVE_TRADING", "false").lower() == "true"
BINANCE_API_KEY = os.environ.get("BINANCE_API_KEY", "")
BINANCE_API_SECRET = os.environ.get("BINANCE_API_SECRET", "")
MAX_POSITION_USDT = float(os.environ.get("MAX_POSITION_USDT", "50"))

STATE_FILE = os.environ.get("STATE_FILE", os.path.join(os.path.dirname(__file__), "..", "data", "state.json"))
BINANCE_BASE_URLS = [
    "https://api.binance.com",
    "https://api.binance.us",   # fallback for geo-blocked regions (subset of symbols)
]
BINANCE_TIMEOUT_SECONDS = 5   # fail fast -- a blocked region should not stall a whole cycle
FETCH_DEADLINE_SECONDS = 20    # HARD wall-clock bound per klines fetch, incl. DNS
                               # (requests' timeout does NOT cover DNS resolution,
                               # which is what hung the boot cycle on Render)


# ============================ INDICATORS ============================
"""Small, dependency-light technical indicators used by the strategies."""




def sma(series: pd.Series, period: int) -> pd.Series:
    return series.rolling(period).mean()


def rsi(series: pd.Series, period: int) -> pd.Series:
    delta = series.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    out = 100 - (100 / (1 + rs))
    return out.fillna(50)


def rolling_zscore(series: pd.Series, period: int) -> pd.Series:
    mean = series.rolling(period).mean()
    std = series.rolling(period).std(ddof=0)
    return (series - mean) / std.replace(0, np.nan)


def rate_of_change(series: pd.Series, period: int) -> pd.Series:
    return series.pct_change(period) * 100


# =========================== DATA FEED =============================
"""Market data fetching. Public klines only -- no auth needed to read data.

Note: some regions (notably US-hosted servers) have historically had
Binance block even public market-data endpoints, not just signed trading
endpoints. We fail fast (short timeout, few fallbacks) so a blocked region
degrades gracefully instead of stalling a whole trading cycle.
"""



from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout



_session = requests.Session()
_cache = {}
_CACHE_TTL = 60  # seconds -- avoid hammering the API across strategies in one cycle


_fetch_pool = ThreadPoolExecutor(max_workers=4)


def _fetch_klines(symbol: str, interval: str, limit: int):
    last_err = None
    for base in BINANCE_BASE_URLS:
        try:
            r = _session.get(
                f"{base}/api/v3/klines",
                params={"symbol": symbol, "interval": interval, "limit": limit},
                timeout=BINANCE_TIMEOUT_SECONDS,
            )
            r.raise_for_status()
            return r.json()
        except Exception as e:  # noqa: BLE001
            last_err = e
            continue
    raise RuntimeError(f"All Binance endpoints failed for {symbol}: {last_err}")


def _fetch_klines_bounded(symbol: str, interval: str, limit: int) -> list:
    """Fetch with a HARD wall-clock deadline. requests' timeout covers connect
    and read but NOT DNS resolution -- a hung getaddrinfo at container boot
    (observed on Render) stalled the first cycle forever. Submitting to a
    pool and bounding the wait covers everything; a truly hung fetch thread
    is abandoned, not waited on."""
    fut = _fetch_pool.submit(_fetch_klines, symbol, interval, limit)
    try:
        return fut.result(timeout=FETCH_DEADLINE_SECONDS)
    except FutureTimeout:
        raise RuntimeError(
            f"klines fetch for {symbol} exceeded {FETCH_DEADLINE_SECONDS}s -- abandoned")


def get_klines(symbol: str, interval: str = None, limit: int = None) -> pd.DataFrame:
    interval = interval or TIMEFRAME
    limit = limit or CANDLE_LOOKBACK
    key = (symbol, interval, limit)
    now = time.time()
    if key in _cache and now - _cache[key][0] < _CACHE_TTL:
        return _cache[key][1].copy()

    raw = _fetch_klines_bounded(symbol, interval, limit)
    df = pd.DataFrame(raw, columns=[
        "open_time", "open", "high", "low", "close", "volume", "close_time",
        "quote_asset_volume", "trades", "taker_buy_base", "taker_buy_quote", "ignore",
    ])
    for col in ["open", "high", "low", "close", "volume"]:
        df[col] = df[col].astype(float)
    df["open_time"] = pd.to_datetime(df["open_time"], unit="ms")
    df = df.set_index("open_time")
    _cache[key] = (now, df)
    return df.copy()


def get_last_price(symbol: str) -> float:
    df = get_klines(symbol, limit=2)
    return float(df["close"].iloc[-1])


# =========================== STRATEGIES ============================
"""
The three signal engines, straight from the PDF's playbook:

  mean_reversion_signal -> Secret 3 / Rule 3  (the core engine, "rubber band")
  momentum_signal       -> Secret 4           ("ride the wave" overlay)
  pairs_signal          -> Secret 3 + Section 6 (Coke/Pepsi stat-arb, market-neutral)

Every signal returns a dict with a `reason` string in plain English --
Secret 1: "don't ask why the market moved, ask what happens next" --
so every trade in the log can be explained in one sentence (Section 10's
Golden Rule).
"""





def mean_reversion_signal(symbol: str):
    df = get_klines(symbol)
    if len(df) < MR_TREND_MA + 5:
        return None
    close = df["close"]
    trend_ma = sma(close, MR_TREND_MA)
    exit_ma = sma(close, MR_EXIT_MA)
    r = rsi(close, MR_RSI_PERIOD)

    price = close.iloc[-1]
    is_uptrend = price > trend_ma.iloc[-1]
    stretched = r.iloc[-1] < MR_RSI_ENTRY

    if is_uptrend and stretched:
        stop = df["low"].iloc[-MR_STOP_LOOKBACK:].min()
        target = exit_ma.iloc[-1]
        if stop >= price or target <= price:
            return None  # degenerate setup, skip
        return {
            "strategy": "mean_reversion",
            "symbol": symbol,
            "side": "long",
            "entry": price,
            "stop": stop,
            "target": target,
            "reason": (
                f"{symbol}: price above {MR_TREND_MA}-MA (uptrend) and "
                f"RSI({MR_RSI_PERIOD})={r.iloc[-1]:.1f} < {MR_RSI_ENTRY} "
                f"-- rubber band stretched, betting on snap-back to the {MR_EXIT_MA}-MA."
            ),
        }
    return None


def mean_reversion_exit(symbol: str, position: dict):
    df = get_klines(symbol)
    close = df["close"]
    exit_ma = sma(close, MR_EXIT_MA).iloc[-1]
    price = close.iloc[-1]
    if price <= position["stop"]:
        return "stop", "Stop-loss hit -- rubber band kept going instead of snapping back."
    if price >= exit_ma or price >= position["target"]:
        return "target", f"Reverted to the {MR_EXIT_MA}-MA as expected -- taking the small win."
    return None, None


def momentum_signal(symbol: str):
    df = get_klines(symbol)
    if len(df) < MOM_FAST_MA + 5:
        return None
    close = df["close"]
    fast_ma = sma(close, MOM_FAST_MA)
    r = rsi(close, MOM_RSI_PERIOD)

    price = close.iloc[-1]
    ma_now, ma_prev = fast_ma.iloc[-1], fast_ma.iloc[-3]
    trending_up = price > ma_now and ma_now > ma_prev
    healthy_rsi = MOM_RSI_MIN <= r.iloc[-1] <= MOM_RSI_MAX

    if trending_up and healthy_rsi:
        stop = df["low"].iloc[-MOM_STOP_LOOKBACK:].min()
        if stop >= price:
            return None
        return {
            "strategy": "momentum",
            "symbol": symbol,
            "side": "long",
            "entry": price,
            "stop": stop,
            "target": None,  # trailing exit -- ride until the MA breaks
            "reason": (
                f"{symbol}: price above rising {MOM_FAST_MA}-MA with healthy "
                f"RSI({MOM_RSI_PERIOD})={r.iloc[-1]:.1f} -- a wave is building, hopping on."
            ),
        }
    return None


def momentum_exit(symbol: str, position: dict):
    df = get_klines(symbol)
    close = df["close"]
    price = close.iloc[-1]
    fast_ma = sma(close, MOM_FAST_MA).iloc[-1]
    if price <= position["stop"]:
        return "stop", "Stop-loss hit -- the wave fizzled early."
    if price < fast_ma:
        return "target", f"Price broke back below the {MOM_FAST_MA}-MA -- jumping off before it crashes."
    return None, None


def pairs_signal(symbol_a: str, symbol_b: str):
    """Stat-arb / pairs trade: short the rich twin, buy the cheap twin,
    hedged and market-neutral, betting the ratio snaps back (Section 6)."""
    df_a = get_klines(symbol_a)
    df_b = get_klines(symbol_b)
    n = min(len(df_a), len(df_b))
    if n < PAIRS_ZSCORE_LOOKBACK + 5:
        return None
    close_a = df_a["close"].iloc[-n:]
    close_b = df_b["close"].iloc[-n:]
    ratio = (close_a.values / close_b.values)

    ratio = pd.Series(ratio, index=close_a.index)
    z = rolling_zscore(ratio, PAIRS_ZSCORE_LOOKBACK)
    z_now = z.iloc[-1]

    if abs(z_now) < PAIRS_ENTRY_Z or abs(z_now) > PAIRS_STOP_Z:
        return None

    price_a = close_a.iloc[-1]
    price_b = close_b.iloc[-1]

    if z_now > 0:
        # ratio too high -> A rich vs B -> short A, long B
        long_leg, short_leg = symbol_b, symbol_a
    else:
        long_leg, short_leg = symbol_a, symbol_b

    return {
        "strategy": "pairs",
        "pair": f"{symbol_a}/{symbol_b}",
        "long_symbol": long_leg,
        "short_symbol": short_leg,
        "long_entry": price_b if long_leg == symbol_b else price_a,
        "short_entry": price_a if short_leg == symbol_a else price_b,
        "z_entry": z_now,
        "reason": (
            f"{symbol_a}/{symbol_b} ratio z-score={z_now:.2f} (>|{PAIRS_ENTRY_Z}|) -- "
            f"twins stretched apart, betting they snap back together. "
            f"Long {long_leg}, short {short_leg}, hedged and market-neutral."
        ),
    }


def pairs_exit(symbol_a: str, symbol_b: str, position: dict):
    df_a = get_klines(symbol_a)
    df_b = get_klines(symbol_b)
    n = min(len(df_a), len(df_b))
    close_a = df_a["close"].iloc[-n:]
    close_b = df_b["close"].iloc[-n:]

    ratio = pd.Series((close_a.values / close_b.values), index=close_a.index)
    z = rolling_zscore(ratio, PAIRS_ZSCORE_LOOKBACK)
    z_now = z.iloc[-1]

    if abs(z_now) > PAIRS_STOP_Z:
        return "stop", "Spread kept widening past the stop z-score -- bailing on the pair."
    entry_sign = 1 if position["z_entry"] > 0 else -1
    if entry_sign * z_now <= PAIRS_EXIT_Z:
        return "target", f"Spread reverted (z={z_now:.2f}) -- twins back together, closing both legs."
    return None, None


# ============================ RISK ENGINE ===========================
"""
Risk engine -- Secret 7 / Rule 4: "borrow carefully, automatic brakes."
No leverage, 1% risk per trade, hard daily-loss cutoff, position caps,
and a hard per-trade notional cap so an unusually tight stop can never
size a trade up to the whole account (Rule 5: never go all-in).
This module never asks how anyone *feels* about a trade.
"""



def position_size(equity: float, entry: float, stop: float) -> float:
    """Return quantity (in base asset units) sized so a stop-out loses
    roughly RISK_PER_TRADE_PCT of equity -- but never more notional than
    MAX_NOTIONAL_PCT_PER_TRADE of equity, and never using leverage."""
    risk_amount = equity * RISK_PER_TRADE_PCT
    stop_distance = abs(entry - stop)
    if stop_distance <= 0 or entry <= 0:
        return 0.0
    qty_by_risk = (risk_amount / stop_distance) * LEVERAGE
    max_qty_by_notional_cap = (equity * MAX_NOTIONAL_PCT_PER_TRADE) / entry
    max_qty_by_full_equity = equity / entry  # absolute ceiling: never "leverage" beyond 1x
    return min(qty_by_risk, max_qty_by_notional_cap, max_qty_by_full_equity)


def affordable(cash: float, qty: float, price: float) -> bool:
    return qty * price <= cash


def daily_loss_breached(day_start_equity: float, current_equity: float) -> bool:
    if day_start_equity <= 0:
        return False
    dd = (current_equity - day_start_equity) / day_start_equity
    return dd <= -MAX_DAILY_LOSS_PCT


def can_open_new_position(open_positions: list, strategy: str) -> bool:
    if len(open_positions) >= MAX_CONCURRENT_POSITIONS:
        return False
    same_strategy = [p for p in open_positions if p.get("strategy") == strategy]
    if len(same_strategy) >= MAX_POSITIONS_PER_STRATEGY:
        return False
    return True


# =========================== KILL SWITCH ===========================
"""
Secret 4: "when a style stops working, switch it off automatically."
Tracks a rolling win-rate per strategy; auto-disables it for a cooldown
window if it's clearly bleeding, and auto-resumes after the cooldown.
"""




def evaluate(state: dict):
    """Mutates state['kill_switch'] based on recent closed trades per strategy."""
    now = datetime.now(timezone.utc)
    ks = state.setdefault("kill_switch", {})
    trades = state.get("closed_trades", [])

    strategies = set(t["strategy"] for t in trades) | {"mean_reversion", "momentum", "pairs"}

    for strat in strategies:
        entry = ks.setdefault(strat, {"disabled_until": None, "last_winrate": None})

        # auto-resume if cooldown passed
        if entry["disabled_until"]:
            disabled_until = datetime.fromisoformat(entry["disabled_until"])
            if now >= disabled_until:
                entry["disabled_until"] = None

        recent = [t for t in trades if t["strategy"] == strat][-KILL_SWITCH_LOOKBACK_TRADES:]
        if len(recent) < KILL_SWITCH_LOOKBACK_TRADES:
            continue  # not enough sample yet -- don't judge early

        wins = sum(1 for t in recent if t["pnl"] > 0)
        winrate = wins / len(recent)
        entry["last_winrate"] = round(winrate, 3)

        if winrate < KILL_SWITCH_MIN_WINRATE and not entry["disabled_until"]:
            until = now + timedelta(hours=KILL_SWITCH_COOLDOWN_HOURS)
            entry["disabled_until"] = until.isoformat()
            state.setdefault("events", []).append({
                "time": now.isoformat(),
                "message": (
                    f"KILL SWITCH: {strat} win-rate over last {len(recent)} trades = "
                    f"{winrate:.0%} (< {KILL_SWITCH_MIN_WINRATE:.0%}). "
                    f"Disabled until {until.isoformat()}."
                ),
            })


def is_enabled(state: dict, strategy: str) -> bool:
    ks = state.get("kill_switch", {}).get(strategy)
    if not ks or not ks.get("disabled_until"):
        return True
    disabled_until = datetime.fromisoformat(ks["disabled_until"])
    return datetime.now(timezone.utc) >= disabled_until


# =========================== PORTFOLIO =============================
"""
Paper-trading ledger. Tracks virtual cash + open positions + closed trade
history + equity curve, persisted to a JSON state file so a restart doesn't
lose the book. This is intentionally simple (no DB) -- it's a paper engine.
"""





_LOCK_STATE = None


def _default_state():
    now = datetime.now(timezone.utc)
    return {
        "mode": "LIVE" if LIVE_TRADING else "PAPER",
        "cash_usdt": STARTING_EQUITY_USDT,
        "starting_equity": STARTING_EQUITY_USDT,
        "day_start_equity": STARTING_EQUITY_USDT,
        "day_start_date": now.date().isoformat(),
        "open_positions": [],   # list of dicts, single-symbol strategies
        "open_pairs": [],       # list of dicts, pairs strategy (two legs)
        "closed_trades": [],
        "equity_history": [{"time": now.isoformat(), "equity": STARTING_EQUITY_USDT}],
        "kill_switch": {},
        "events": [],
        "halted_today": False,
        "last_cycle": None,
    }


def load_state() -> dict:
    global _LOCK_STATE
    path = STATE_FILE
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
    path = STATE_FILE
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


# ============================= ENGINE ==============================
"""
The main decision cycle. Runs on a fixed schedule (CYCLE_SECONDS), and does
exactly what the rules say -- nothing more:

  1. Mark-to-market, roll the trading day, check the daily-loss brake.
  2. Check every OPEN position/pair for its exit condition first.
  3. If not halted, scan the universe for NEW entry signals and size them
     with the risk engine, respecting diversification caps and the kill
     switch.
  4. Log every decision with its one-sentence reason (Secret 1 / Golden Rule).

100% systematic (Secret 6): this file is the only thing that ever touches
the  No manual override hook exists on purpose.
"""







log = logging.getLogger("medallion_bot")


def _price_lookup(symbol):
    try:
        return get_last_price(symbol)
    except Exception:
        return None


def run_cycle(state: dict) -> dict:
    now = datetime.now(timezone.utc)
    maybe_roll_day(state)

    # 1) mark to market + daily brake -------------------------------------------------
    def safe_price(sym):
        p = _price_lookup(sym)
        return p if p is not None else None

    live_prices = {}
    fetch_attempts = {"ok": 0, "fail": 0}

    def cached_price(sym):
        if sym not in live_prices:
            p = safe_price(sym)
            live_prices[sym] = p
            if p is None:
                fetch_attempts["fail"] += 1
            else:
                fetch_attempts["ok"] += 1
        return live_prices[sym] if live_prices[sym] is not None else None

    def price_or_last(sym, fallback):
        p = cached_price(sym)
        return p if p is not None else fallback

    equity = mark_to_market(state, price_lookup=lambda s: price_or_last(s, 0))
    record_equity(state, equity)

    if daily_loss_breached(state["day_start_equity"], equity):
        if not state["halted_today"]:
            state["events"].append({
                "time": now.isoformat(),
                "message": (
                    f"DAILY LOSS BRAKE: equity {equity:.2f} vs day-start "
                    f"{state['day_start_equity']:.2f} breached -{MAX_DAILY_LOSS_PCT:.0%}. "
                    f"Computer says no -- no new trades until tomorrow."
                ),
            })
        state["halted_today"] = True

    evaluate(state)

    # 2) manage exits first -------------------------------------------------------------
    for position in list(state["open_positions"]):
        price = price_or_last(position["symbol"], position["entry"])
        exit_fn = mean_reversion_exit if position["strategy"] == "mean_reversion" else momentum_exit
        kind, reason = exit_fn(position["symbol"], position)
        if kind:
            close_single(state, position, price, kind, reason)
            state["events"].append({"time": now.isoformat(), "message": f"CLOSED {position['symbol']} ({position['strategy']}): {reason}"})

    for pair_pos in list(state["open_pairs"]):
        a, b = pair_pos["pair"].split("/")
        kind, reason = pairs_exit(a, b, pair_pos)
        if kind:
            long_exit = price_or_last(pair_pos["long_symbol"], pair_pos["long_entry"])
            short_exit = price_or_last(pair_pos["short_symbol"], pair_pos["short_entry"])
            close_pair(state, pair_pos, long_exit, short_exit, kind, reason)
            state["events"].append({"time": now.isoformat(), "message": f"CLOSED PAIR {pair_pos['pair']}: {reason}"})

    # 3) scan for new entries (only if not halted for the day) -------------------------
    if not state["halted_today"]:
        equity_now = mark_to_market(state, price_lookup=lambda s: price_or_last(s, 0))

        held_symbols = {p["symbol"] for p in state["open_positions"]}

        # mean-reversion first (the core engine gets priority per symbol)
        if is_enabled(state, "mean_reversion"):
            for symbol in MR_MOMENTUM_UNIVERSE:
                if symbol in held_symbols:
                    continue
                if not can_open_new_position(state["open_positions"], "mean_reversion"):
                    break
                try:
                    sig = mean_reversion_signal(symbol)
                except Exception as e:  # noqa: BLE001
                    log.warning("mean_reversion_signal(%s) failed: %s", symbol, e)
                    continue
                if sig:
                    qty = position_size(equity_now, sig["entry"], sig["stop"])
                    if qty > 0 and affordable(state["cash_usdt"], qty, sig["entry"]):
                        open_single(state, sig, qty)
                        held_symbols.add(symbol)
                        state["events"].append({"time": now.isoformat(), "message": f"OPENED {symbol} (mean_reversion): {sig['reason']}"})

        # momentum on symbols not already held by mean-reversion
        if is_enabled(state, "momentum"):
            for symbol in MR_MOMENTUM_UNIVERSE:
                if symbol in held_symbols:
                    continue
                if not can_open_new_position(state["open_positions"], "momentum"):
                    break
                try:
                    sig = momentum_signal(symbol)
                except Exception as e:  # noqa: BLE001
                    log.warning("momentum_signal(%s) failed: %s", symbol, e)
                    continue
                if sig:
                    qty = position_size(equity_now, sig["entry"], sig["stop"])
                    if qty > 0 and affordable(state["cash_usdt"], qty, sig["entry"]):
                        open_single(state, sig, qty)
                        held_symbols.add(symbol)
                        state["events"].append({"time": now.isoformat(), "message": f"OPENED {symbol} (momentum): {sig['reason']}"})

        # pairs / stat-arb
        if is_enabled(state, "pairs"):
            held_pairs = {p["pair"] for p in state["open_pairs"]}
            for sym_a, sym_b in PAIRS_UNIVERSE:
                pair_key = f"{sym_a}/{sym_b}"
                if pair_key in held_pairs:
                    continue
                if len(state["open_pairs"]) >= MAX_POSITIONS_PER_STRATEGY:
                    break
                try:
                    sig = pairs_signal(sym_a, sym_b)
                except Exception as e:  # noqa: BLE001
                    log.warning("pairs_signal(%s,%s) failed: %s", sym_a, sym_b, e)
                    continue
                if sig:
                    notional = equity_now * PAIRS_NOTIONAL_PCT
                    long_qty = notional / sig["long_entry"]
                    short_qty = notional / sig["short_entry"]
                    total_cost = long_qty * sig["long_entry"] + short_qty * sig["short_entry"]
                    if affordable(state["cash_usdt"], 1, total_cost):
                        open_pair(state, sig, long_qty, short_qty)
                        state["events"].append({"time": now.isoformat(), "message": f"OPENED PAIR {pair_key}: {sig['reason']}"})

    # surface a clear signal if the data feed is unreachable (e.g. region-blocked host)
    if fetch_attempts["fail"] > 0 and fetch_attempts["ok"] == 0:
        state["data_feed_status"] = "unreachable"
        if not state.get("_feed_warned"):
            state["events"].append({
                "time": now.isoformat(),
                "message": (
                    "DATA FEED UNREACHABLE: could not fetch any Binance prices from this "
                    "server (likely region-blocked). No new signals can be evaluated until "
                    "this resolves -- try redeploying to a Frankfurt or Singapore region."
                ),
            })
            state["_feed_warned"] = True
    else:
        state["data_feed_status"] = "ok"
        state["_feed_warned"] = False

    state["events"] = state["events"][-200:]
    state["last_cycle"] = now.isoformat()

    final_equity = mark_to_market(state, price_lookup=lambda s: price_or_last(s, 0))
    record_equity(state, final_equity)

    return state


kill_switch_is_enabled = is_enabled  # engine calls it by its full name


TEMPLATE_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Medallion-Flavored Bot</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4"></script>
<style>
  :root {
    --bg: #0b0e14; --card: #131722; --border: #232838; --text: #e6e8ee;
    --muted: #8a92a6; --primary: #22c55e; --danger: #ef4444; --warn: #f59e0b;
  }
  * { box-sizing: border-box; }
  body { background: var(--bg); color: var(--text); font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif; margin: 0; padding: 24px; }
  h1 { font-size: 20px; margin: 0 0 4px; }
  .sub { color: var(--muted); font-size: 13px; margin-bottom: 20px; }
  .grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); gap: 12px; margin-bottom: 20px; }
  .card { background: var(--card); border: 1px solid var(--border); border-radius: 10px; padding: 16px; }
  .card .label { color: var(--muted); font-size: 12px; text-transform: uppercase; letter-spacing: .04em; }
  .card .value { font-size: 24px; font-weight: 600; margin-top: 6px; }
  .value.pos { color: var(--primary); } .value.neg { color: var(--danger); }
  .badge { display: inline-block; padding: 2px 8px; border-radius: 6px; font-size: 11px; font-weight: 700; letter-spacing: .03em; }
  .badge.paper { background: #1e3a8a33; color: #60a5fa; }
  .badge.live { background: #7f1d1d33; color: #f87171; }
  .badge.halted { background: #7f1d1d33; color: #f87171; margin-left: 8px; }
  .badge.ok { background: #14532d33; color: #4ade80; }
  section { background: var(--card); border: 1px solid var(--border); border-radius: 10px; padding: 16px; margin-bottom: 20px; }
  section h2 { font-size: 14px; margin: 0 0 12px; color: var(--muted); text-transform: uppercase; letter-spacing: .04em; }
  table { width: 100%; border-collapse: collapse; font-size: 13px; }
  th, td { text-align: left; padding: 8px 6px; border-bottom: 1px solid var(--border); }
  th { color: var(--muted); font-weight: 500; }
  .pnl-pos { color: var(--primary); } .pnl-neg { color: var(--danger); }
  .strategy-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(220px,1fr)); gap: 12px; }
  .strat-card { background: #0f1420; border: 1px solid var(--border); border-radius: 8px; padding: 12px; }
  .strat-card .name { font-weight: 600; margin-bottom: 6px; display:flex; justify-content: space-between; align-items:center;}
  .event { padding: 6px 0; border-bottom: 1px solid var(--border); font-size: 12.5px; color: #c7cbdb; }
  .event time { color: var(--muted); margin-right: 8px; }
  canvas { max-height: 260px; }
  .footer-note { color: var(--muted); font-size: 12px; margin-top: 20px; line-height: 1.5; }
  .empty { color: var(--muted); font-size: 13px; padding: 8px 0; }
</style>
</head>
<body>
  <h1>Medallion-Flavored Trading Bot <span id="mode-badge" class="badge paper">PAPER</span><span id="halt-badge"></span></h1>
  <div class="sub">Mean-reversion core + momentum overlay + market-neutral pairs, risk-capped. Education / research tool -- not financial advice.</div>
  <div id="feed-banner" style="display:none; background:#7f1d1d33; border:1px solid #7f1d1d; color:#f87171; border-radius:8px; padding:10px 14px; font-size:13px; margin-bottom:16px;"></div>

  <div class="grid">
    <div class="card"><div class="label">Equity</div><div class="value" id="equity">--</div></div>
    <div class="card"><div class="label">Total Return</div><div class="value" id="return">--</div></div>
    <div class="card"><div class="label">Cash (USDT)</div><div class="value" id="cash">--</div></div>
    <div class="card"><div class="label">Today's P&L</div><div class="value" id="daily-pnl">--</div></div>
    <div class="card"><div class="label">Open Positions</div><div class="value" id="open-count">--</div></div>
    <div class="card"><div class="label">Last Cycle (UTC)</div><div class="value" id="last-cycle" style="font-size:14px;">--</div></div>
  </div>

  <section>
    <h2>Equity Curve</h2>
    <canvas id="equityChart"></canvas>
  </section>

  <section>
    <h2>Strategy Performance (Secret 4: kill dead styles automatically)</h2>
    <div class="strategy-grid" id="strategy-grid"></div>
  </section>

  <section>
    <h2>Open Positions &amp; Pairs</h2>
    <table id="open-table"><thead><tr><th>Type</th><th>Symbol / Pair</th><th>Strategy</th><th>Entry</th><th>Stop</th><th>Opened</th></tr></thead><tbody></tbody></table>
    <div class="empty" id="open-empty" style="display:none;">No open positions right now -- waiting for a stretched rubber band or a building wave.</div>
  </section>

  <section>
    <h2>Recent Closed Trades</h2>
    <table id="trades-table"><thead><tr><th>Symbol/Pair</th><th>Strategy</th><th>Exit</th><th>PnL</th><th>Reason</th></tr></thead><tbody></tbody></table>
    <div class="empty" id="trades-empty" style="display:none;">No trades closed yet.</div>
  </section>

  <section>
    <h2>Decision Log (every entry/exit explained in one sentence)</h2>
    <div id="events"></div>
    <div class="empty" id="events-empty" style="display:none;">No events yet -- the bot runs its first cycle shortly after boot.</div>
  </section>

  <div class="footer-note">
    Universe: <span id="universe"></span><br>
    Pairs: <span id="pairs-universe"></span><br>
    Rules: 1% risk per trade &middot; no leverage &middot; max 15% notional per trade &middot; daily loss brake at -4% &middot; auto kill-switch below 35% win-rate over last 20 trades.<br>
    This dashboard is for education/research only and is not financial advice. Past performance (including Medallion's) does not guarantee future results.
  </div>

<script>
let chart;
async function refresh() {
  try {
    const res = await fetch('/api/status');
    const s = await res.json();

    document.getElementById('mode-badge').textContent = s.mode + (s.live_trading ? '' : '');
    document.getElementById('mode-badge').className = 'badge ' + (s.mode === 'LIVE' ? 'live' : 'paper');
    document.getElementById('halt-badge').innerHTML = s.halted_today ? '<span class="badge halted">DAILY LOSS BRAKE ACTIVE</span>' : '';

    const feedBanner = document.getElementById('feed-banner');
    if (s.data_feed_status === 'unreachable') {
      feedBanner.style.display = 'block';
      feedBanner.textContent = 'Cannot reach Binance from this server -- likely region-blocked. No new signals can be evaluated until this resolves. Try redeploying to a Frankfurt or Singapore region on Render.';
    } else if (s.last_error) {
      feedBanner.style.display = 'block';
      feedBanner.textContent = 'Last cycle error: ' + s.last_error;
    } else {
      feedBanner.style.display = 'none';
    }

    document.getElementById('equity').textContent = '$' + s.equity.toLocaleString(undefined, {maximumFractionDigits:2});
    const ret = ((s.equity - s.starting_equity) / s.starting_equity) * 100;
    const retEl = document.getElementById('return');
    retEl.textContent = (ret >= 0 ? '+' : '') + ret.toFixed(2) + '%';
    retEl.className = 'value ' + (ret >= 0 ? 'pos' : 'neg');

    document.getElementById('cash').textContent = '$' + s.cash_usdt.toLocaleString(undefined, {maximumFractionDigits:2});

    const dailyPnl = s.equity - s.day_start_equity;
    const dailyEl = document.getElementById('daily-pnl');
    dailyEl.textContent = (dailyPnl >= 0 ? '+$' : '-$') + Math.abs(dailyPnl).toFixed(2);
    dailyEl.className = 'value ' + (dailyPnl >= 0 ? 'pos' : 'neg');

    document.getElementById('open-count').textContent = (s.open_positions.length + s.open_pairs.length);
    document.getElementById('last-cycle').textContent = s.last_cycle ? new Date(s.last_cycle).toLocaleString() : '--';

    document.getElementById('universe').textContent = s.universe.join(', ');
    document.getElementById('pairs-universe').textContent = s.pairs_universe.join(', ');

    // strategy cards
    const grid = document.getElementById('strategy-grid');
    grid.innerHTML = '';
    for (const [name, st] of Object.entries(s.strategy_stats)) {
      const div = document.createElement('div');
      div.className = 'strat-card';
      const statusBadge = st.enabled ? '<span class="badge ok">ACTIVE</span>' : '<span class="badge halted">KILLED</span>';
      div.innerHTML = `
        <div class="name"><span>${name.replace('_',' ')}</span>${statusBadge}</div>
        <div style="color:var(--muted); font-size:12px;">Trades: ${st.trades} &middot; Win rate: ${st.win_rate !== null ? (st.win_rate*100).toFixed(0)+'%' : 'n/a'}</div>
        <div style="font-size:13px; margin-top:4px;">Total PnL: <span class="${st.total_pnl>=0?'pnl-pos':'pnl-neg'}">$${st.total_pnl.toFixed(2)}</span></div>
      `;
      grid.appendChild(div);
    }

    // open positions/pairs table
    const openBody = document.querySelector('#open-table tbody');
    openBody.innerHTML = '';
    let anyOpen = false;
    for (const p of s.open_positions) {
      anyOpen = true;
      const tr = document.createElement('tr');
      tr.innerHTML = `<td>Single</td><td>${p.symbol}</td><td>${p.strategy}</td><td>$${p.entry.toFixed(4)}</td><td>$${p.stop.toFixed(4)}</td><td>${new Date(p.opened_at).toLocaleString()}</td>`;
      openBody.appendChild(tr);
    }
    for (const pr of s.open_pairs) {
      anyOpen = true;
      const tr = document.createElement('tr');
      tr.innerHTML = `<td>Pair</td><td>${pr.pair}</td><td>pairs (long ${pr.long_symbol} / short ${pr.short_symbol})</td><td>--</td><td>--</td><td>${new Date(pr.opened_at).toLocaleString()}</td>`;
      openBody.appendChild(tr);
    }
    document.getElementById('open-empty').style.display = anyOpen ? 'none' : 'block';

    // closed trades table
    const tradesBody = document.querySelector('#trades-table tbody');
    tradesBody.innerHTML = '';
    for (const t of s.closed_trades) {
      const tr = document.createElement('tr');
      const label = t.symbol || t.pair;
      tr.innerHTML = `<td>${label}</td><td>${t.strategy}</td><td>${t.exit_kind}</td><td class="${t.pnl>=0?'pnl-pos':'pnl-neg'}">$${t.pnl.toFixed(2)}</td><td>${t.exit_reason || ''}</td>`;
      tradesBody.appendChild(tr);
    }
    document.getElementById('trades-empty').style.display = s.closed_trades.length ? 'none' : 'block';

    // events
    const eventsDiv = document.getElementById('events');
    eventsDiv.innerHTML = '';
    for (const e of s.events) {
      const div = document.createElement('div');
      div.className = 'event';
      div.innerHTML = `<time>${new Date(e.time).toLocaleString()}</time>${e.message}`;
      eventsDiv.appendChild(div);
    }
    document.getElementById('events-empty').style.display = s.events.length ? 'none' : 'block';

    // equity chart
    const labels = s.equity_history.map(p => new Date(p.time).toLocaleString());
    const data = s.equity_history.map(p => p.equity);
    if (!chart) {
      const ctx = document.getElementById('equityChart').getContext('2d');
      chart = new Chart(ctx, {
        type: 'line',
        data: { labels, datasets: [{ label: 'Equity (USDT)', data, borderColor: '#22c55e', backgroundColor: 'rgba(34,197,94,0.08)', fill: true, pointRadius: 0, tension: 0.15 }] },
        options: {
          responsive: true,
          scales: {
            x: { ticks: { color: '#8a92a6', maxTicksLimit: 8 }, grid: { color: '#232838' } },
            y: { ticks: { color: '#8a92a6' }, grid: { color: '#232838' } },
          },
          plugins: { legend: { labels: { color: '#e6e8ee' } } },
        }
      });
    } else {
      chart.data.labels = labels;
      chart.data.datasets[0].data = data;
      chart.update();
    }
  } catch (err) {
    console.error('refresh failed', err);
  }
}
refresh();
setInterval(refresh, 30000);
</script>
</body>
</html>
"""

# ============================ FLASK APP ==============================
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("medallion_bot.app")

app = Flask(__name__)
HTML_TEMPLATE = TEMPLATE_HTML

_state_lock = threading.Lock()  # brief holds only -- never during network I/O
_state = load_state()
_last_error = None
_cycle_generation = 0           # bumped by every cycle start; commits must match
_last_watchdog_kick = 0.0
LOOP_BOOT_DELAY_SECONDS = 30    # let the container's network settle at boot


def _get_state_snapshot():
    with _state_lock:
        return _state


def _begin_cycle():
    """Reserve the next cycle generation and take a private working copy of
    the state. Commit is rejected if a newer generation started meanwhile,
    so concurrent or hung cycles can never corrupt or block each other."""
    global _cycle_generation
    with _state_lock:
        _cycle_generation += 1
        gen = _cycle_generation
        snapshot = copy.deepcopy(_state)
    return gen, snapshot


def _commit_cycle(gen, new_state):
    """Publish a finished cycle's state -- unless a newer cycle superseded it."""
    global _state
    with _state_lock:
        if gen != _cycle_generation:
            log.info("Discarding stale cycle commit (gen %d superseded by gen %d)",
                     gen, _cycle_generation)
            return False
        _state = new_state
        save_state(_state)
        return True


def _run_cycle_and_commit(gen, snapshot):
    """Run one trading cycle and try to commit it. Never holds a lock during
    network I/O; never raises (errors are recorded instead)."""
    global _last_error
    try:
        result = run_cycle(snapshot)
        if _commit_cycle(gen, result):
            _last_error = None
            log.info("Cycle complete (gen %d). Open positions: %d, open pairs: %d",
                     gen, len(_state["open_positions"]), len(_state["open_pairs"]))
    except Exception as e:  # noqa: BLE001
        _last_error = str(e)
        log.exception("Cycle failed (gen %d): %s", gen, e)


def _cycle_loop():
    time.sleep(LOOP_BOOT_DELAY_SECONDS)  # container network may not be ready at t=0
    while True:
        gen, snapshot = _begin_cycle()
        _run_cycle_and_commit(gen, snapshot)  # runs in this thread; watchdog runs its own
        time.sleep(CYCLE_SECONDS)


def start_background_loop():
    t = threading.Thread(target=_cycle_loop, daemon=True, name="trading_loop")
    t.start()


def _loop_alive():
    return any(t.name == "trading_loop" and t.is_alive() for t in threading.enumerate())


def _cycle_is_stale():
    s = _get_state_snapshot()
    lc = s.get("last_cycle")
    if not lc:
        return True
    try:
        age = (datetime.now(timezone.utc) - datetime.fromisoformat(lc)).total_seconds()
    except Exception:  # noqa: BLE001
        return False
    return age > CYCLE_SECONDS + 300  # cycle interval + 5 min grace


def _watchdog_kick():
    """Fire-and-forget: if no cycle has completed recently, start a fresh
    generation cycle in its own thread. A hung older cycle can't block this
    one -- its commit will simply be discarded."""
    global _last_watchdog_kick
    now = time.time()
    if not _cycle_is_stale():
        return
    if now - _last_watchdog_kick < 120:  # throttle failed kicks; success stops kicks ~20 min
        return
    _last_watchdog_kick = now
    log.warning("Watchdog: no recent cycle (loop_alive=%s) -- kicking one now", _loop_alive())
    gen, snapshot = _begin_cycle()
    threading.Thread(
        target=_run_cycle_and_commit, args=(gen, snapshot),
        daemon=True, name="watchdog_cycle",
    ).start()


@app.before_request
def _watchdog():
    try:
        _watchdog_kick()
    except Exception:  # noqa: BLE001
        pass


@app.route("/")
def dashboard():
    s = _get_state_snapshot()
    return render_template_string(HTML_TEMPLATE, mode=s.get("mode", "PAPER"))


@app.route("/api/status")
def api_status():
    s = _get_state_snapshot()
    equity = mark_to_market(s)
    strat_stats = {}
    for strat_name in ("mean_reversion", "momentum", "pairs"):
        trades = [t for t in s["closed_trades"] if t["strategy"] == strat_name]
        wins = [t for t in trades if t["pnl"] > 0]
        losses = [t for t in trades if t["pnl"] <= 0]
        avg_win = sum(t["pnl"] for t in wins) / len(wins) if wins else 0
        avg_loss = sum(t["pnl"] for t in losses) / len(losses) if losses else 0
        ks = s.get("kill_switch", {}).get(strat_name, {})
        strat_stats[strat_name] = {
            "trades": len(trades),
            "win_rate": round(len(wins) / len(trades), 3) if trades else None,
            "avg_win": round(avg_win, 2),
            "avg_loss": round(avg_loss, 2),
            "total_pnl": round(sum(t["pnl"] for t in trades), 2),
            "enabled": kill_switch_is_enabled(s, strat_name),
            "disabled_until": ks.get("disabled_until"),
        }

    payload = {
        "mode": s.get("mode"),
        "live_trading": LIVE_TRADING,
        "equity": round(equity, 2),
        "starting_equity": s.get("starting_equity"),
        "cash_usdt": round(s.get("cash_usdt", 0), 2),
        "day_start_equity": round(s.get("day_start_equity", 0), 2),
        "halted_today": s.get("halted_today", False),
        "data_feed_status": s.get("data_feed_status", "unknown"),
        "loop_alive": _loop_alive(),
        "open_positions": s.get("open_positions", []),
        "open_pairs": s.get("open_pairs", []),
        "closed_trades": list(reversed(s.get("closed_trades", [])))[:30],
        "equity_history": s.get("equity_history", [])[-300:],
        "events": list(reversed(s.get("events", [])))[:40],
        "strategy_stats": strat_stats,
        "last_cycle": s.get("last_cycle"),
        "last_error": _last_error,
        "server_time": datetime.now(timezone.utc).isoformat(),
        "universe": MR_MOMENTUM_UNIVERSE,
        "pairs_universe": [f"{a}/{b}" for a, b in PAIRS_UNIVERSE],
    }
    return jsonify(payload)


@app.route("/health")
def health():
    s = _get_state_snapshot()
    return jsonify({"status": "ok", "mode": s.get("mode")})


@app.route("/api/run-now", methods=["POST"])
def run_now():
    """Manual trigger for a single cycle -- same rules as the automatic ones.
    Works even if a previous cycle is stuck: this one supersedes it and the
    stuck cycle's result is discarded."""
    global _last_error
    gen, snapshot = _begin_cycle()
    try:
        result = run_cycle(snapshot)
    except Exception as e:  # noqa: BLE001
        _last_error = str(e)
        return jsonify({"ok": False, "error": str(e)}), 500
    committed = _commit_cycle(gen, result)
    if committed:
        _last_error = None
    return jsonify({"ok": True, "committed": committed})


if __name__ == "__main__":
    start_background_loop()
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
else:
    # gunicorn / production entry
    start_background_loop()
