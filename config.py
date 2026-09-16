"""
Central configuration for the Medallion-flavored trading bot.
Everything here is a RULE, not a feeling -- Secret 6: let the robot trade.
"""
import os

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
    "https://api1.binance.com",
    "https://api2.binance.com",
    "https://api.binance.us",   # fallback for geo-blocked regions (subset of symbols)
]
