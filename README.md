# Medallion-Flavored Trading Bot

A systematic crypto paper-trading bot built from the "Most Profitable
Trading Strategy Ever, Explained Simply" playbook (the Jim Simons /
Renaissance Medallion Fund approach) — **not** a copy of the actual secret
code (nobody has that outside Renaissance), but the same *principles*,
sized for a retail account:

- **Mean-reversion core** (Secret 3 / Rule 3): buys stretched RSI(2) dips
  while the trend is up, exits when price reverts to the short MA.
- **Momentum overlay** (Secret 4): rides confirmed short-term trends,
  jumps off when the trend breaks. Blending both means the bot can make
  money in choppy AND trending markets.
- **Market-neutral pairs / stat-arb** (Secret 3 + Section 6's Coke/Pepsi
  example): watches correlated crypto "twins" (ETH/BTC, SOL/ETH, BNB/BTC),
  longs the cheap one + shorts the rich one when the ratio stretches, and
  closes both legs when it snaps back. Doesn't care which way the market
  moves overall — only that the pair reconverges.
- **Strict risk engine** (Secrets 5 & 7 / Rule 4): 1% risk per trade, hard
  cap of 15% notional per single trade (so a tight stop can't size up to
  the whole account), no leverage, a **daily loss brake at -4%** that
  halts new trades for the rest of the day, and diversification caps
  across up to 8 concurrent positions.
- **Auto kill-switch** (Secret 4: "when a style stops working, switch it
  off"): if a strategy's win rate over its last 20 closed trades drops
  below 35%, it's automatically disabled for 48 hours, then re-enabled.
- **100% systematic** (Secret 6): there is no manual override hook in the
  code on purpose. Every decision — entry, exit, kill-switch — is logged
  with a one-sentence plain-English reason.

Runs in **PAPER trading mode by default** with a virtual $10,000 USDT
account. This is education/research tooling — **not financial advice**.
Past performance (including Medallion's own) never guarantees future
results, and the PDF itself is explicit that nobody outside Renaissance
has the real code — this bot is the "retail version" from Section 8 of
that guide, automated.

## How it works

Every `CYCLE_SECONDS` (default 15 minutes, see `bot/config.py`):
1. Marks the paper portfolio to market and rolls the trading day if needed.
2. Checks the daily-loss brake — if breached, halts new entries until the
   next UTC day.
3. Checks every open position/pair for its exit condition first.
4. If not halted, scans the universe for new entries (mean-reversion →
   momentum → pairs, in that priority) sized by the risk engine.
5. Logs everything to `data/state.json` and shows it on the dashboard.

## Universe

- Mean-reversion + momentum run independently on: BTC, ETH, SOL, BNB,
  XRP, ADA, DOGE, AVAX (all -USDT pairs).
- Pairs / stat-arb: ETH/BTC, SOL/ETH, BNB/BTC.

Change these in `bot/config.py` (`MR_MOMENTUM_UNIVERSE`, `PAIRS_UNIVERSE`).

## Running locally

```bash
pip install -r requirements.txt
python app.py
```

Dashboard: http://localhost:5000 — auto-refreshes every 30s.
JSON status: http://localhost:5000/api/status

## Deploying (Render, same pattern as your other bot)

1. Push this folder to a GitHub repo.
2. On Render: New → Web Service → connect the repo.
3. **Region matters only for LIVE trading** (see below) — Frankfurt or
   Singapore, not US, if you ever flip live mode on. Paper mode works
   anywhere since it only reads public market data.
4. Build command: `pip install -r requirements.txt`
   Start command: `gunicorn --workers 1 --threads 4 --timeout 60 --bind 0.0.0.0:$PORT app:app`
   (Keep `--workers 1` — the bot's background loop and state file aren't
   safe to run in more than one process.)
5. Optional env vars: `STARTING_EQUITY_USDT` (default 10000).
6. Deploy. The dashboard comes up immediately in PAPER mode; the first
   decision cycle runs within a few seconds of boot.

A `render.yaml` is included if you prefer Render's "Infra as code" flow.

## Going live (only after paper proves itself — Section 11's 30-day plan)

Live spot trading is **not wired into this bot yet on purpose** — the PDF's
own honest-warnings section is explicit that retail leverage and costs
kill small edges, and Section 11's plan says: paper trade with 1% risk
rules, journal every trade, and only fund a live account if the demo was
disciplined and profitable after costs. Watch this bot's dashboard for a
few weeks first.

If/when you want live execution wired in (real Binance spot orders on the
paper engine's exact same signals), just ask — it's the same broker
pattern used in your other bot (`five0pips`): a Binance API key with
**trading only, withdrawals disabled**, region set to Frankfurt/Singapore
(Binance blocks signed order requests from US IPs), and a
`MAX_POSITION_USDT` hard cap per trade as an extra rail on top of the risk
engine above.

## Files

```
app.py                 Flask dashboard + background scheduler
bot/config.py           All strategy/risk parameters -- the rulebook
bot/data_feed.py         Public Binance klines (no auth needed to read data)
bot/indicators.py        SMA, RSI, rolling z-score, ROC
bot/strategies.py        Mean-reversion / momentum / pairs signal + exit logic
bot/risk.py              Position sizing, notional caps, daily-loss brake
bot/kill_switch.py       Auto-disable/re-enable a losing strategy
bot/portfolio.py         Paper ledger: cash, positions, trades, equity curve
bot/engine.py            Ties it all together into one decision cycle
templates/index.html    Dashboard UI
data/state.json          Persisted paper-trading state (created on first run)
```

## Honest limitations (read this)

- This is the **retail-sized principles version**, not the Medallion
  algorithm — nobody outside Renaissance has that, and the PDF says so
  plainly. Expect a small, real edge at best, not 66%/year.
- Backtested-looking rules (RSI(2), 200-MA, pairs z-score) can and do stop
  working — that's *why* the kill-switch exists. Watch it, don't "trust
  and forget."
- Crypto pairs are more correlated with each other than stocks like
  Coke/Pepsi — during a market-wide crash, "market-neutral" pairs can
  still lose money together for a while before reconverging. The pair
  stop-out (z > 3.5) exists exactly for that.
- No leverage is used anywhere in this bot, by design.


## SINGLE-FILE EDITION (use this one)

This zip is the **single-file edition**: the entire bot lives in one
`app.py` (plus `requirements.txt`). No subfolders -- nothing can get lost
when uploading to GitHub via the web UI.

Repo layout (both files at repo root):
```
app.py             <- the entire bot + dashboard
requirements.txt
```

KEEP-ALIVE (required on Render free tier):
The free tier sleeps the service after ~15 min without web traffic, and a
sleeping bot cannot trade. Add the deployed URL's /health path to a free
pinger (UptimeRobot, cron-job.org, etc.) at a 10-minute interval.

Built-in watchdog + hang-proof cycles: if the background trading loop
ever dies OR hangs (both observed on some PaaS runtimes), any web request
-- including those keep-alive pings -- automatically triggers a fresh
cycle. Cycles run in generations, so a stuck one is abandoned and
superseded instead of blocking anything. Data fetches are hard-bounded
(including DNS, which the HTTP timeout does not cover). The dashboard
reports `loop_alive` in /api/status.

Render settings:
- Build command: `pip install -r requirements.txt`
- Start command: `gunicorn --workers 1 --threads 4 --timeout 60 --bind 0.0.0.0:$PORT app:app`
- Optional env var: `PYTHON_VERSION=3.11.16` (Render's default 3.14 also works,
  it just compiles pandas from source which is slower)
