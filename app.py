"""
Medallion-Flavored Trading Bot -- Flask dashboard + background scheduler.

Strategy summary (from "The Most Profitable Trading Strategy Ever, Explained
Simply" -- the Jim Simons / Medallion Fund playbook):
  - Mean-reversion core: buy stretched RSI(2) dips in an uptrend, exit on
    reversion to the short MA (Secret 3 / Rule 3).
  - Momentum overlay: ride confirmed short-term trends, jump off when the
    trend breaks (Secret 4).
  - Market-neutral pairs / stat-arb: long the cheap twin, short the rich
    twin when their ratio stretches, exit on reversion (Secret 3 + Section 6).
  - Strict risk engine: 1% risk/trade, no leverage, daily loss brake,
    diversification caps, auto kill-switch on losing strategies
    (Secrets 5, 6, 7 / Rules 4-5).

Runs in PAPER mode by default -- see bot/config.py. This is education/
research tooling, not financial advice.
"""
import logging
import os
import threading
import time
from datetime import datetime, timezone

from flask import Flask, jsonify, render_template

from bot import config, portfolio, engine, kill_switch

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("medallion_bot.app")

app = Flask(__name__)

_state_lock = threading.Lock()
_state = portfolio.load_state()
_last_error = None


def _cycle_loop():
    global _state, _last_error
    while True:
        try:
            with _state_lock:
                _state = engine.run_cycle(_state)
                portfolio.save_state(_state)
                _last_error = None
            log.info("Cycle complete. Equity history points: %d, open positions: %d, open pairs: %d",
                      len(_state["equity_history"]), len(_state["open_positions"]), len(_state["open_pairs"]))
        except Exception as e:  # noqa: BLE001
            _last_error = str(e)
            log.exception("Cycle failed: %s", e)
        time.sleep(config.CYCLE_SECONDS)


def start_background_loop():
    t = threading.Thread(target=_cycle_loop, daemon=True)
    t.start()


@app.route("/")
def dashboard():
    return render_template("index.html", mode=_state.get("mode", "PAPER"))


@app.route("/api/status")
def api_status():
    with _state_lock:
        s = _state
        equity = portfolio.mark_to_market(s)
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
                "enabled": kill_switch.is_enabled(s, strat_name),
                "disabled_until": ks.get("disabled_until"),
            }

        payload = {
            "mode": s.get("mode"),
            "live_trading": config.LIVE_TRADING,
            "equity": round(equity, 2),
            "starting_equity": s.get("starting_equity"),
            "cash_usdt": round(s.get("cash_usdt", 0), 2),
            "day_start_equity": round(s.get("day_start_equity", 0), 2),
            "halted_today": s.get("halted_today", False),
            "open_positions": s.get("open_positions", []),
            "open_pairs": s.get("open_pairs", []),
            "closed_trades": list(reversed(s.get("closed_trades", [])))[:30],
            "equity_history": s.get("equity_history", [])[-300:],
            "events": list(reversed(s.get("events", [])))[:40],
            "strategy_stats": strat_stats,
            "last_cycle": s.get("last_cycle"),
            "last_error": _last_error,
            "server_time": datetime.now(timezone.utc).isoformat(),
            "universe": config.MR_MOMENTUM_UNIVERSE,
            "pairs_universe": [f"{a}/{b}" for a, b in config.PAIRS_UNIVERSE],
        }
        return jsonify(payload)


@app.route("/health")
def health():
    return jsonify({"status": "ok", "mode": _state.get("mode")})


@app.route("/api/run-now", methods=["POST"])
def run_now():
    """Manual trigger for a single cycle -- useful for testing/demo, still
    100% rule-driven (Secret 6): this does not let a human pick trades, it
    just runs the same run_cycle() the scheduler runs."""
    global _state
    with _state_lock:
        _state = engine.run_cycle(_state)
        portfolio.save_state(_state)
    return jsonify({"ok": True})


if __name__ == "__main__":
    start_background_loop()
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
else:
    # gunicorn / production entry
    start_background_loop()
