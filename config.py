import math
import os
from dataclasses import dataclass
from typing import Any, Dict, List, Tuple

# Top 20 High-Volume, Liquid 1:1 USDT Perpetuals listed on BOTH Binance USDS-M & Bybit Linear
TOP_20_COINS: List[str] = [
    "BTCUSDT",
    "ETHUSDT",
    "SOLUSDT",
    "XRPUSDT",
    "BNBUSDT",
    "DOGEUSDT",
    "SUIUSDT",
    "ADAUSDT",
    "AVAXUSDT",
    "LINKUSDT",
    "LTCUSDT",
    "DOTUSDT",
    "UNIUSDT",
    "AAVEUSDT",
    "BCHUSDT",
    "NEARUSDT",
    "TRXUSDT",
    "APTUSDT",
    "TAOUSDT",
    "HYPEUSDT",
]

BINANCE_FAPI_REST = "https://fapi.binance.com"
BINANCE_FAPI_WS = "wss://fstream.binance.com/stream"

BYBIT_V5_REST = "https://api.bybit.com"
BYBIT_V5_WS_LINEAR = "wss://stream.bybit.com/v5/public/linear"

# Strictly Day-Trading / Intraday-Swing Multi-Timeframe Hierarchy (NO 1m Scalping)
TIMEFRAMES: Tuple[str, ...] = ("5m", "15m", "1h", "4h", "1d")

BYBIT_TF_MAP: Dict[str, str] = {
    "5m": "5",
    "15m": "15",
    "1h": "60",
    "4h": "240",
    "1d": "D",
}

TF_MS_MAP: Dict[str, int] = {
    "5m": 300_000,
    "15m": 900_000,
    "1h": 3_600_000,
    "4h": 14_400_000,
    "1d": 86_400_000,
}

# Dynamic CoinGlass-style Footprint bucket sizing (% of price, snapped cleanly to 1 / 2 / 2.5 / 5)
TF_ROW_PCT: Dict[str, float] = {
    "5m": 0.00050,
    "15m": 0.00080,
    "1h": 0.00150,
    "4h": 0.00300,
    "1d": 0.00600,
}

DEFAULT_SETTINGS: Dict[str, Any] = {
    "telegram_bot_token": "",
    "telegram_chat_id": "",
    "telegram_enabled": True,
    "watchlist": TOP_20_COINS,
    # Day Mode Only (1D + 4H Bias -> 1H Structure -> 15m/5m Orderflow Sniper Confirmation)
    "mode": "DAY_ONLY",
    "min_rr": 2.0,                  # Strict Minimum Risk:Reward >= 2.0
    "min_sl_pct": 0.55,             # Minimum Day-Mode SL distance (%) — blocks tight scalp stops!
    "max_sl_pct": 4.50,             # Maximum SL distance (%) — blocks runaway news spikes
    "sl_atr_mult": 1.25,            # Minimum SL must be >= 1.25x 15m ATR beyond sweep wick
    "sl_buffer_pct": 0.12,          # Extra buffer beyond sweep wick (%)
    "imbalance_ratio": 3.0,         # 300% diagonal Bid/Ask imbalance threshold
    "stacked_min_rows": 3,          # 3+ contiguous imbalanced rows = Stacked Imbalance
    "level_proximity_pct": 0.65,    # Coin enters AT_LEVEL Tier-2 Orderflow Watch within 0.65% of Key Pool
    "sweep_expiry_bars_15m": 6,     # Up to 6x 15m bars (90 min) to confirm MSS after HTF sweep
    "sweep_expiry_bars_5m": 8,      # Up to 8x 5m bars (40 min) for 5m sniper MSS after 15m/1H sweep
    "signal_cooldown_min": 90,      # Cooldown after a closed trade before re-scanning same level
    "max_tier2_streams": 8,         # Max simultaneous heavy Tier-2 Orderflow streams (prevents VPS overload)
    "cmp_threshold_pct": 0.22,      # If price is within 0.22% of Entry Mid -> CMP, else LIMIT order
    "min_confidence": 72,           # Minimum confidence (0-100) to emit Telegram & Chart signal
}


def compute_row_size(symbol: str, price: float, tf: str) -> float:
    if price <= 0 or not math.isfinite(price):
        return 1.0
    mult = TF_ROW_PCT.get(tf, 0.00080)
    raw = price * mult
    if raw <= 0:
        return 1.0
    exp = math.floor(math.log10(raw))
    base = 10.0 ** exp
    frac = raw / base
    if frac < 1.4:
        nice = 1.0
    elif frac < 2.2:
        nice = 2.0
    elif frac < 3.5:
        nice = 2.5
    elif frac < 7.0:
        nice = 5.0
    else:
        nice = 10.0
    return round(nice * base, 8)


def price_decimals(price: float) -> int:
    if price >= 1000:
        return 1
    if price >= 50:
        return 2
    if price >= 5:
        return 3
    if price >= 0.5:
        return 4
    return 5


def format_price(price: float) -> str:
    if price <= 0 or not math.isfinite(price):
        return "--"
    d = price_decimals(price)
    return f"{price:,.{d}f}"


def sanitize_watchlist(symbols: List[str], max_items: int = 20) -> List[str]:
    cleaned: List[str] = []
    for raw in symbols:
        sym = str(raw).strip().upper()
        if sym and sym.isalnum() and sym.endswith("USDT") and sym not in cleaned:
            cleaned.append(sym)
        if len(cleaned) >= max_items:
            break
    return cleaned


@dataclass
class EngineConfig:
    host: str = os.getenv("HOST", "0.0.0.0")
    port: int = int(os.getenv("PORT", "8000"))
    db_path: str = os.getenv("DB_PATH", "orderflow_v3.db")
    history_fetch_limit: int = 260   # Fetches 260 candles -> keeps >= 250 closed/live candles!
    max_candles_per_tf: int = 300
    footprint_backfill_bars: int = 250