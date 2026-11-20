from dataclasses import dataclass
from typing import Dict, List

DEFAULT_WATCHLIST: List[str] = [
    "BTCUSDT",
    "ETHUSDT",
    "SOLUSDT",
    "XRPUSDT",
    "BNBUSDT",
    "LTCUSDT",
    "HYPEUSDT",
    "DOGEUSDT",
    "SUIUSDT",
    "LINKUSDT",
]

BINANCE_FAPI_REST = "https://fapi.binance.com"
BINANCE_FAPI_WS = "wss://fstream.binance.com/stream"

# Dynamic nice tick-bucket row sizing (% of price) so Footprint rows look like CoinGlass across all coins
# e.g. BTC (~63,700) -> ~30-50, ETH (~2,000) -> ~1.0-2.0, SOL (~80-200) -> ~0.10-0.25, HYPE -> ~0.05
ROW_SIZE_OVERRIDES: Dict[str, Dict[str, float]] = {
    "BTCUSDT":  {"1m": 15.0,   "5m": 25.0,   "15m": 40.0,  "1h": 80.0},
    "ETHUSDT":  {"1m": 1.0,    "5m": 1.5,    "15m": 2.5,   "1h": 5.0},
    "SOLUSDT":  {"1m": 0.05,   "5m": 0.10,   "15m": 0.20,  "1h": 0.40},
    "XRPUSDT":  {"1m": 0.0005, "5m": 0.001,  "15m": 0.002, "1h": 0.005},
    "BNBUSDT":  {"1m": 0.20,   "5m": 0.50,   "15m": 1.0,   "1h": 2.0},
    "LTCUSDT":  {"1m": 0.05,   "5m": 0.10,   "15m": 0.20,  "1h": 0.40},
    "HYPEUSDT": {"1m": 0.01,   "5m": 0.025,  "15m": 0.05,  "1h": 0.10},
    "DOGEUSDT": {"1m": 0.0002, "5m": 0.0004, "15m": 0.0008,"1h": 0.0015},
    "SUIUSDT":  {"1m": 0.001,  "5m": 0.002,  "15m": 0.004, "1h": 0.008},
    "LINKUSDT": {"1m": 0.005,  "5m": 0.01,   "15m": 0.02,  "1h": 0.05},
}

TF_MULTIPLIERS: Dict[str, float] = {
    "1m": 0.00025,
    "5m": 0.00045,
    "15m": 0.00075,
    "1h": 0.00150,
}

def compute_row_size(symbol: str, price: float, tf: str) -> float:
    sym = symbol.upper()
    if sym in ROW_SIZE_OVERRIDES and tf in ROW_SIZE_OVERRIDES[sym]:
        return ROW_SIZE_OVERRIDES[sym][tf]
    if price <= 0:
        return 1.0
    mult = TF_MULTIPLIERS.get(tf, 0.0005)
    raw = price * mult
    # Snap to clean 1-2-5 magnitude
    import math
    exp = math.floor(math.log10(raw))
    base = 10 ** exp
    frac = raw / base
    if frac < 1.5:
        nice = 1.0
    elif frac < 3.5:
        nice = 2.0
    elif frac < 7.5:
        nice = 5.0
    else:
        nice = 10.0
    return round(nice * base, 8)

@dataclass
class EngineConfig:
    host: str = "0.0.0.0"
    port: int = 8000
    db_path: str = "orderflow_bot.db"
    max_candles_per_tf: int = 180
    history_fetch_limit: int = 120