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
the portfolio. No manual override hook exists on purpose.
"""
import logging
from datetime import datetime, timezone

from . import config, risk, kill_switch, portfolio
from .data_feed import get_last_price
from . import strategies as strat

log = logging.getLogger("medallion_bot")


def _price_lookup(symbol):
    try:
        return get_last_price(symbol)
    except Exception:
        return None


def run_cycle(state: dict) -> dict:
    now = datetime.now(timezone.utc)
    portfolio.maybe_roll_day(state)

    # 1) mark to market + daily brake -------------------------------------------------
    def safe_price(sym):
        p = _price_lookup(sym)
        return p if p is not None else None

    live_prices = {}

    def cached_price(sym):
        if sym not in live_prices:
            p = safe_price(sym)
            live_prices[sym] = p
        return live_prices[sym] if live_prices[sym] is not None else None

    def price_or_last(sym, fallback):
        p = cached_price(sym)
        return p if p is not None else fallback

    equity = portfolio.mark_to_market(state, price_lookup=lambda s: price_or_last(s, 0))
    portfolio.record_equity(state, equity)

    if risk.daily_loss_breached(state["day_start_equity"], equity):
        if not state["halted_today"]:
            state["events"].append({
                "time": now.isoformat(),
                "message": (
                    f"DAILY LOSS BRAKE: equity {equity:.2f} vs day-start "
                    f"{state['day_start_equity']:.2f} breached -{config.MAX_DAILY_LOSS_PCT:.0%}. "
                    f"Computer says no -- no new trades until tomorrow."
                ),
            })
        state["halted_today"] = True

    kill_switch.evaluate(state)

    # 2) manage exits first -------------------------------------------------------------
    for position in list(state["open_positions"]):
        price = price_or_last(position["symbol"], position["entry"])
        exit_fn = strat.mean_reversion_exit if position["strategy"] == "mean_reversion" else strat.momentum_exit
        kind, reason = exit_fn(position["symbol"], position)
        if kind:
            portfolio.close_single(state, position, price, kind, reason)
            state["events"].append({"time": now.isoformat(), "message": f"CLOSED {position['symbol']} ({position['strategy']}): {reason}"})

    for pair_pos in list(state["open_pairs"]):
        a, b = pair_pos["pair"].split("/")
        kind, reason = strat.pairs_exit(a, b, pair_pos)
        if kind:
            long_exit = price_or_last(pair_pos["long_symbol"], pair_pos["long_entry"])
            short_exit = price_or_last(pair_pos["short_symbol"], pair_pos["short_entry"])
            portfolio.close_pair(state, pair_pos, long_exit, short_exit, kind, reason)
            state["events"].append({"time": now.isoformat(), "message": f"CLOSED PAIR {pair_pos['pair']}: {reason}"})

    # 3) scan for new entries (only if not halted for the day) -------------------------
    if not state["halted_today"]:
        equity_now = portfolio.mark_to_market(state, price_lookup=lambda s: price_or_last(s, 0))

        held_symbols = {p["symbol"] for p in state["open_positions"]}

        # mean-reversion first (the core engine gets priority per symbol)
        if kill_switch.is_enabled(state, "mean_reversion"):
            for symbol in config.MR_MOMENTUM_UNIVERSE:
                if symbol in held_symbols:
                    continue
                if not risk.can_open_new_position(state["open_positions"], "mean_reversion"):
                    break
                try:
                    sig = strat.mean_reversion_signal(symbol)
                except Exception as e:  # noqa: BLE001
                    log.warning("mean_reversion_signal(%s) failed: %s", symbol, e)
                    continue
                if sig:
                    qty = risk.position_size(equity_now, sig["entry"], sig["stop"])
                    if qty > 0 and risk.affordable(state["cash_usdt"], qty, sig["entry"]):
                        portfolio.open_single(state, sig, qty)
                        held_symbols.add(symbol)
                        state["events"].append({"time": now.isoformat(), "message": f"OPENED {symbol} (mean_reversion): {sig['reason']}"})

        # momentum on symbols not already held by mean-reversion
        if kill_switch.is_enabled(state, "momentum"):
            for symbol in config.MR_MOMENTUM_UNIVERSE:
                if symbol in held_symbols:
                    continue
                if not risk.can_open_new_position(state["open_positions"], "momentum"):
                    break
                try:
                    sig = strat.momentum_signal(symbol)
                except Exception as e:  # noqa: BLE001
                    log.warning("momentum_signal(%s) failed: %s", symbol, e)
                    continue
                if sig:
                    qty = risk.position_size(equity_now, sig["entry"], sig["stop"])
                    if qty > 0 and risk.affordable(state["cash_usdt"], qty, sig["entry"]):
                        portfolio.open_single(state, sig, qty)
                        held_symbols.add(symbol)
                        state["events"].append({"time": now.isoformat(), "message": f"OPENED {symbol} (momentum): {sig['reason']}"})

        # pairs / stat-arb
        if kill_switch.is_enabled(state, "pairs"):
            held_pairs = {p["pair"] for p in state["open_pairs"]}
            for sym_a, sym_b in config.PAIRS_UNIVERSE:
                pair_key = f"{sym_a}/{sym_b}"
                if pair_key in held_pairs:
                    continue
                if len(state["open_pairs"]) >= config.MAX_POSITIONS_PER_STRATEGY:
                    break
                try:
                    sig = strat.pairs_signal(sym_a, sym_b)
                except Exception as e:  # noqa: BLE001
                    log.warning("pairs_signal(%s,%s) failed: %s", sym_a, sym_b, e)
                    continue
                if sig:
                    notional = equity_now * config.PAIRS_NOTIONAL_PCT
                    long_qty = notional / sig["long_entry"]
                    short_qty = notional / sig["short_entry"]
                    total_cost = long_qty * sig["long_entry"] + short_qty * sig["short_entry"]
                    if risk.affordable(state["cash_usdt"], 1, total_cost):
                        portfolio.open_pair(state, sig, long_qty, short_qty)
                        state["events"].append({"time": now.isoformat(), "message": f"OPENED PAIR {pair_key}: {sig['reason']}"})

    state["events"] = state["events"][-200:]
    state["last_cycle"] = now.isoformat()

    final_equity = portfolio.mark_to_market(state, price_lookup=lambda s: price_or_last(s, 0))
    portfolio.record_equity(state, final_equity)

    return state
