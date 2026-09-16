"""
Secret 4: "when a style stops working, switch it off automatically."
Tracks a rolling win-rate per strategy; auto-disables it for a cooldown
window if it's clearly bleeding, and auto-resumes after the cooldown.
"""
from datetime import datetime, timedelta, timezone
from . import config


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

        recent = [t for t in trades if t["strategy"] == strat][-config.KILL_SWITCH_LOOKBACK_TRADES:]
        if len(recent) < config.KILL_SWITCH_LOOKBACK_TRADES:
            continue  # not enough sample yet -- don't judge early

        wins = sum(1 for t in recent if t["pnl"] > 0)
        winrate = wins / len(recent)
        entry["last_winrate"] = round(winrate, 3)

        if winrate < config.KILL_SWITCH_MIN_WINRATE and not entry["disabled_until"]:
            until = now + timedelta(hours=config.KILL_SWITCH_COOLDOWN_HOURS)
            entry["disabled_until"] = until.isoformat()
            state.setdefault("events", []).append({
                "time": now.isoformat(),
                "message": (
                    f"KILL SWITCH: {strat} win-rate over last {len(recent)} trades = "
                    f"{winrate:.0%} (< {config.KILL_SWITCH_MIN_WINRATE:.0%}). "
                    f"Disabled until {until.isoformat()}."
                ),
            })


def is_enabled(state: dict, strategy: str) -> bool:
    ks = state.get("kill_switch", {}).get(strategy)
    if not ks or not ks.get("disabled_until"):
        return True
    disabled_until = datetime.fromisoformat(ks["disabled_until"])
    return datetime.now(timezone.utc) >= disabled_until
