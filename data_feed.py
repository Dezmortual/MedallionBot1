"""Market data fetching. Public klines only -- no auth, no geo-block issues
for reading data (only signed order endpoints get region-blocked)."""
import time
import requests
import pandas as pd

from . import config

_session = requests.Session()
_cache = {}
_CACHE_TTL = 60  # seconds -- avoid hammering the API across strategies in one cycle


def _fetch_klines(symbol: str, interval: str, limit: int):
    last_err = None
    for base in config.BINANCE_BASE_URLS:
        try:
            r = _session.get(
                f"{base}/api/v3/klines",
                params={"symbol": symbol, "interval": interval, "limit": limit},
                timeout=10,
            )
            r.raise_for_status()
            return r.json()
        except Exception as e:  # noqa: BLE001
            last_err = e
            continue
    raise RuntimeError(f"All Binance endpoints failed for {symbol}: {last_err}")


def get_klines(symbol: str, interval: str = None, limit: int = None) -> pd.DataFrame:
    interval = interval or config.TIMEFRAME
    limit = limit or config.CANDLE_LOOKBACK
    key = (symbol, interval, limit)
    now = time.time()
    if key in _cache and now - _cache[key][0] < _CACHE_TTL:
        return _cache[key][1].copy()

    raw = _fetch_klines(symbol, interval, limit)
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
