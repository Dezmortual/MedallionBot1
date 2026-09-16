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
from . import config
from .data_feed import get_klines
from .indicators import sma, rsi, rolling_zscore


def mean_reversion_signal(symbol: str):
    df = get_klines(symbol)
    if len(df) < config.MR_TREND_MA + 5:
        return None
    close = df["close"]
    trend_ma = sma(close, config.MR_TREND_MA)
    exit_ma = sma(close, config.MR_EXIT_MA)
    r = rsi(close, config.MR_RSI_PERIOD)

    price = close.iloc[-1]
    is_uptrend = price > trend_ma.iloc[-1]
    stretched = r.iloc[-1] < config.MR_RSI_ENTRY

    if is_uptrend and stretched:
        stop = df["low"].iloc[-config.MR_STOP_LOOKBACK:].min()
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
                f"{symbol}: price above {config.MR_TREND_MA}-MA (uptrend) and "
                f"RSI({config.MR_RSI_PERIOD})={r.iloc[-1]:.1f} < {config.MR_RSI_ENTRY} "
                f"-- rubber band stretched, betting on snap-back to the {config.MR_EXIT_MA}-MA."
            ),
        }
    return None


def mean_reversion_exit(symbol: str, position: dict):
    df = get_klines(symbol)
    close = df["close"]
    exit_ma = sma(close, config.MR_EXIT_MA).iloc[-1]
    price = close.iloc[-1]
    if price <= position["stop"]:
        return "stop", "Stop-loss hit -- rubber band kept going instead of snapping back."
    if price >= exit_ma or price >= position["target"]:
        return "target", f"Reverted to the {config.MR_EXIT_MA}-MA as expected -- taking the small win."
    return None, None


def momentum_signal(symbol: str):
    df = get_klines(symbol)
    if len(df) < config.MOM_FAST_MA + 5:
        return None
    close = df["close"]
    fast_ma = sma(close, config.MOM_FAST_MA)
    r = rsi(close, config.MOM_RSI_PERIOD)

    price = close.iloc[-1]
    ma_now, ma_prev = fast_ma.iloc[-1], fast_ma.iloc[-3]
    trending_up = price > ma_now and ma_now > ma_prev
    healthy_rsi = config.MOM_RSI_MIN <= r.iloc[-1] <= config.MOM_RSI_MAX

    if trending_up and healthy_rsi:
        stop = df["low"].iloc[-config.MOM_STOP_LOOKBACK:].min()
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
                f"{symbol}: price above rising {config.MOM_FAST_MA}-MA with healthy "
                f"RSI({config.MOM_RSI_PERIOD})={r.iloc[-1]:.1f} -- a wave is building, hopping on."
            ),
        }
    return None


def momentum_exit(symbol: str, position: dict):
    df = get_klines(symbol)
    close = df["close"]
    price = close.iloc[-1]
    fast_ma = sma(close, config.MOM_FAST_MA).iloc[-1]
    if price <= position["stop"]:
        return "stop", "Stop-loss hit -- the wave fizzled early."
    if price < fast_ma:
        return "target", f"Price broke back below the {config.MOM_FAST_MA}-MA -- jumping off before it crashes."
    return None, None


def pairs_signal(symbol_a: str, symbol_b: str):
    """Stat-arb / pairs trade: short the rich twin, buy the cheap twin,
    hedged and market-neutral, betting the ratio snaps back (Section 6)."""
    df_a = get_klines(symbol_a)
    df_b = get_klines(symbol_b)
    n = min(len(df_a), len(df_b))
    if n < config.PAIRS_ZSCORE_LOOKBACK + 5:
        return None
    close_a = df_a["close"].iloc[-n:]
    close_b = df_b["close"].iloc[-n:]
    ratio = (close_a.values / close_b.values)
    import pandas as pd
    ratio = pd.Series(ratio, index=close_a.index)
    z = rolling_zscore(ratio, config.PAIRS_ZSCORE_LOOKBACK)
    z_now = z.iloc[-1]

    if abs(z_now) < config.PAIRS_ENTRY_Z or abs(z_now) > config.PAIRS_STOP_Z:
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
            f"{symbol_a}/{symbol_b} ratio z-score={z_now:.2f} (>|{config.PAIRS_ENTRY_Z}|) -- "
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
    import pandas as pd
    ratio = pd.Series((close_a.values / close_b.values), index=close_a.index)
    z = rolling_zscore(ratio, config.PAIRS_ZSCORE_LOOKBACK)
    z_now = z.iloc[-1]

    if abs(z_now) > config.PAIRS_STOP_Z:
        return "stop", "Spread kept widening past the stop z-score -- bailing on the pair."
    entry_sign = 1 if position["z_entry"] > 0 else -1
    if entry_sign * z_now <= config.PAIRS_EXIT_Z:
        return "target", f"Spread reverted (z={z_now:.2f}) -- twins back together, closing both legs."
    return None, None
