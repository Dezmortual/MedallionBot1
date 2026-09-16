"""
Risk engine -- Secret 7 / Rule 4: "borrow carefully, automatic brakes."
No leverage, 1% risk per trade, hard daily-loss cutoff, position caps,
and a hard per-trade notional cap so an unusually tight stop can never
size a trade up to the whole account (Rule 5: never go all-in).
This module never asks how anyone *feels* about a trade.
"""
from . import config


def position_size(equity: float, entry: float, stop: float) -> float:
    """Return quantity (in base asset units) sized so a stop-out loses
    roughly RISK_PER_TRADE_PCT of equity -- but never more notional than
    MAX_NOTIONAL_PCT_PER_TRADE of equity, and never using leverage."""
    risk_amount = equity * config.RISK_PER_TRADE_PCT
    stop_distance = abs(entry - stop)
    if stop_distance <= 0 or entry <= 0:
        return 0.0
    qty_by_risk = (risk_amount / stop_distance) * config.LEVERAGE
    max_qty_by_notional_cap = (equity * config.MAX_NOTIONAL_PCT_PER_TRADE) / entry
    max_qty_by_full_equity = equity / entry  # absolute ceiling: never "leverage" beyond 1x
    return min(qty_by_risk, max_qty_by_notional_cap, max_qty_by_full_equity)


def affordable(cash: float, qty: float, price: float) -> bool:
    return qty * price <= cash


def daily_loss_breached(day_start_equity: float, current_equity: float) -> bool:
    if day_start_equity <= 0:
        return False
    dd = (current_equity - day_start_equity) / day_start_equity
    return dd <= -config.MAX_DAILY_LOSS_PCT


def can_open_new_position(open_positions: list, strategy: str) -> bool:
    if len(open_positions) >= config.MAX_CONCURRENT_POSITIONS:
        return False
    same_strategy = [p for p in open_positions if p.get("strategy") == strategy]
    if len(same_strategy) >= config.MAX_POSITIONS_PER_STRATEGY:
        return False
    return True
