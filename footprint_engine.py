import math
import statistics
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Deque, Dict, List, Optional, Set, Tuple

from config import TF_MS_MAP, compute_row_size


def snap_bucket(price: float, row_size: float) -> float:
    if row_size <= 0:
        return round(price, 8)
    return round(math.floor((price + 1e-12) / row_size) * row_size, 8)


@dataclass
class FootprintCandle:
    symbol: str
    tf: str
    open_time: int
    close_time: int
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0
    bybit_volume: float = 0.0
    quote_volume: float = 0.0
    taker_buy_vol: float = 0.0
    row_size: float = 1.0
    is_closed: bool = False
    fp_source: str = "NONE"      # "NONE" | "EST" | "LIVE"
    min_bn_agg_id: Optional[int] = None
    max_bn_agg_id: Optional[int] = None

    # Aggregated Dual-Exchange bucket_price -> [bid_vol (sell taker), ask_vol (buy taker)]
    rows: Dict[float, List[float]] = field(default_factory=dict)

    delta: float = 0.0
    binance_delta: float = 0.0
    bybit_delta: float = 0.0
    cvd: float = 0.0
    poc_price: float = 0.0
    poc_vol: float = 0.0
    val_price: float = 0.0
    vah_price: float = 0.0
    buy_imbalances: List[float] = field(default_factory=list)
    sell_imbalances: List[float] = field(default_factory=list)
    stacked_zones: List[Dict[str, Any]] = field(default_factory=list)
    absorption: Optional[str] = None
    absorption_price: Optional[float] = None
    trapped: Optional[str] = None
    trapped_zone: Optional[Tuple[float, float]] = None
    unfinished_high: bool = False
    unfinished_low: bool = False
    exhaustion: Optional[str] = None
    divergence: Optional[str] = None
    swing_div: Optional[str] = None
    liq_buy_usd: float = 0.0
    liq_sell_usd: float = 0.0
    _dirty: bool = False

    def add_trade(
        self,
        price: float,
        qty: float,
        is_sell_taker: bool,
        exchange: str = "BINANCE",
        agg_id: Optional[int] = None,
    ) -> None:
        if exchange == "BINANCE" and agg_id is not None:
            if self.min_bn_agg_id is not None and self.max_bn_agg_id is not None:
                if self.min_bn_agg_id <= agg_id <= self.max_bn_agg_id:
                    return
            if self.min_bn_agg_id is None or agg_id < self.min_bn_agg_id:
                self.min_bn_agg_id = agg_id
            if self.max_bn_agg_id is None or agg_id > self.max_bn_agg_id:
                self.max_bn_agg_id = agg_id

        if self.fp_source == "EST":
            self.rows.clear()
            self.binance_delta = 0.0
            self.bybit_delta = 0.0

        if self.row_size <= 0:
            self.row_size = compute_row_size(self.symbol, price, self.tf)
        bucket = snap_bucket(price, self.row_size)
        if bucket not in self.rows:
            self.rows[bucket] = [0.0, 0.0]

        if is_sell_taker:
            self.rows[bucket][0] += qty
            if exchange == "BYBIT":
                self.bybit_delta -= qty
            else:
                self.binance_delta -= qty
            self.delta -= qty
        else:
            self.rows[bucket][1] += qty
            if exchange == "BYBIT":
                self.bybit_delta += qty
            else:
                self.binance_delta += qty
                self.taker_buy_vol += qty
            self.delta += qty

        self.fp_source = "LIVE"
        self._dirty = True

        if self.open <= 0:
            self.open = price
        if self.high <= 0 or price > self.high:
            self.high = price
        if self.low <= 0 or price < self.low:
            self.low = price
        self.close = price

        if exchange == "BYBIT":
            self.bybit_volume += qty
        else:
            self.volume += qty
            self.quote_volume += price * qty

    def add_backfill_trade(self, price: float, qty: float, is_sell_taker: bool, agg_id: int) -> None:
        if self.min_bn_agg_id is not None and self.max_bn_agg_id is not None:
            if self.min_bn_agg_id <= agg_id <= self.max_bn_agg_id:
                return
        if self.min_bn_agg_id is None or agg_id < self.min_bn_agg_id:
            self.min_bn_agg_id = agg_id
        if self.max_bn_agg_id is None or agg_id > self.max_bn_agg_id:
            self.max_bn_agg_id = agg_id

        if self.fp_source == "EST":
            self.rows.clear()
            self.binance_delta = 0.0
            self.bybit_delta = 0.0

        if self.row_size <= 0:
            self.row_size = compute_row_size(self.symbol, price, self.tf)
        bucket = snap_bucket(price, self.row_size)
        if bucket not in self.rows:
            self.rows[bucket] = [0.0, 0.0]
        if is_sell_taker:
            self.rows[bucket][0] += qty
            self.binance_delta -= qty
        else:
            self.rows[bucket][1] += qty
            self.binance_delta += qty

        self.fp_source = "LIVE"
        self._dirty = True

    def build_estimated_rows(self) -> None:
        if self.fp_source == "LIVE" or self.volume <= 0 or self.high <= self.low:
            return
        if self.row_size <= 0:
            self.row_size = compute_row_size(self.symbol, self.close, self.tf)
        lo_b = snap_bucket(self.low, self.row_size)
        hi_b = snap_bucket(self.high, self.row_size)
        steps = max(1, min(32, int(round((hi_b - lo_b) / self.row_size)) + 1))

        ask_tot = max(0.0, min(self.volume, self.taker_buy_vol))
        bid_tot = max(0.0, self.volume - ask_tot)
        tp = (self.high + self.low + self.close) / 3.0
        span = max(self.high - self.low, self.row_size)

        weights: List[float] = []
        prices: List[float] = []
        for s in range(steps):
            bp = round(lo_b + s * self.row_size, 8)
            prices.append(bp)
            dist = abs((bp + self.row_size * 0.5) - tp) / span
            weights.append(max(0.15, 1.0 - dist * 0.75))
        w_sum = sum(weights) or 1.0

        self.rows = {}
        for bp, w in zip(prices, weights):
            frac = w / w_sum
            self.rows[bp] = [round(bid_tot * frac, 4), round(ask_tot * frac, 4)]
        self.fp_source = "EST"
        self.delta = round(ask_tot - bid_tot, 6)
        self.binance_delta = self.delta

    def compute_metrics(self, imbalance_ratio: float = 3.0, stacked_min: int = 3) -> None:
        self._dirty = False
        if not self.rows:
            self.delta = round(2.0 * self.taker_buy_vol - self.volume, 6)
            self.binance_delta = self.delta
            self.poc_price = round((self.high + self.low + self.close) / 3.0, 8)
            self.val_price = self.low
            self.vah_price = self.high
            return

        sorted_prices = sorted(self.rows.keys())
        total_bid = 0.0
        total_ask = 0.0
        row_totals: List[Tuple[float, float, float, float]] = []

        max_vol = -1.0
        poc_idx = 0
        for idx, p in enumerate(sorted_prices):
            bid, ask = self.rows[p]
            tot = bid + ask
            total_bid += bid
            total_ask += ask
            row_totals.append((p, bid, ask, tot))
            if tot > max_vol:
                max_vol = tot
                poc_idx = idx

        row_total_vol = total_bid + total_ask
        if self.fp_source == "LIVE":
            if self.volume <= 0 or row_total_vol >= self.volume * 0.50:
                self.delta = round(total_ask - total_bid, 6)
            else:
                self.delta = round(2.0 * self.taker_buy_vol - self.volume + self.bybit_delta, 6)
        else:
            self.delta = round(2.0 * self.taker_buy_vol - self.volume, 6)

        self.poc_price = row_totals[poc_idx][0]
        self.poc_vol = round(max_vol, 6)
        avg_row_vol = (row_total_vol / len(row_totals)) if row_totals else 0.0

        if row_total_vol > 0 and row_totals:
            target_va = row_total_vol * 0.70
            va_vol = row_totals[poc_idx][3]
            lo, hi = poc_idx, poc_idx
            while va_vol < target_va and (lo > 0 or hi < len(row_totals) - 1):
                dv = row_totals[lo - 1][3] if lo > 0 else -1.0
                uv = row_totals[hi + 1][3] if hi < len(row_totals) - 1 else -1.0
                if uv >= dv and hi < len(row_totals) - 1:
                    hi += 1
                    va_vol += row_totals[hi][3]
                elif lo > 0:
                    lo -= 1
                    va_vol += row_totals[lo][3]
                else:
                    break
            self.val_price = row_totals[lo][0]
            self.vah_price = round(row_totals[hi][0] + self.row_size, 8)
        else:
            self.val_price = self.low
            self.vah_price = self.high

        self.buy_imbalances = []
        self.sell_imbalances = []
        self.stacked_zones = []
        self.absorption = None
        self.absorption_price = None
        self.trapped = None
        self.trapped_zone = None
        self.unfinished_high = False
        self.unfinished_low = False
        self.exhaustion = None

        if self.fp_source != "LIVE":
            return

        if row_totals:
            lo_bid, lo_ask = row_totals[0][1], row_totals[0][2]
            hi_bid, hi_ask = row_totals[-1][1], row_totals[-1][2]
            min_edge_vol = avg_row_vol * 0.10
            self.unfinished_low = lo_bid > min_edge_vol and lo_ask > min_edge_vol
            self.unfinished_high = hi_bid > min_edge_vol and hi_ask > min_edge_vol

            if len(row_totals) >= 4 and avg_row_vol > 0:
                if row_totals[-1][3] < avg_row_vol * 0.22 and row_totals[-2][3] > avg_row_vol * 0.60:
                    self.exhaustion = "HIGH_EXHAUSTION"
                elif row_totals[0][3] < avg_row_vol * 0.22 and row_totals[1][3] > avg_row_vol * 0.60:
                    self.exhaustion = "LOW_EXHAUSTION"

        min_imb_vol = avg_row_vol * 0.35
        buy_imbs: List[float] = []
        sell_imbs: List[float] = []

        for i in range(len(row_totals)):
            p, bid_i, ask_i, _ = row_totals[i]
            if i > 0 and abs((p - row_totals[i - 1][0]) - self.row_size) <= self.row_size * 0.15:
                bid_below = row_totals[i - 1][1]
                if ask_i >= min_imb_vol:
                    if (bid_below == 0 and ask_i >= avg_row_vol * 0.8) or (
                        bid_below > 0 and ask_i >= imbalance_ratio * bid_below
                    ):
                        buy_imbs.append(p)

            if i < len(row_totals) - 1 and abs((row_totals[i + 1][0] - p) - self.row_size) <= self.row_size * 0.15:
                ask_above = row_totals[i + 1][2]
                if bid_i >= min_imb_vol:
                    if (ask_above == 0 and bid_i >= avg_row_vol * 0.8) or (
                        ask_above > 0 and bid_i >= imbalance_ratio * ask_above
                    ):
                        sell_imbs.append(p)

        self.buy_imbalances = buy_imbs
        self.sell_imbalances = sell_imbs

        stacked: List[Dict[str, Any]] = []
        stacked.extend(self._find_stacked(buy_imbs, "BULL", stacked_min))
        stacked.extend(self._find_stacked(sell_imbs, "BEAR", stacked_min))
        self.stacked_zones = stacked

        candle_range = max(self.high - self.low, self.row_size)
        upper_wick = self.high - max(self.open, self.close)
        lower_wick = min(self.open, self.close) - self.low
        upper_wick_ratio = upper_wick / candle_range
        lower_wick_ratio = lower_wick / candle_range

        top_threshold = self.high - candle_range * 0.35
        bot_threshold = self.low + candle_range * 0.35

        if upper_wick_ratio >= 0.28 and row_total_vol > 0:
            top_ask_vol = sum(r[2] for r in row_totals if r[0] >= top_threshold)
            top_bid_vol = sum(r[1] for r in row_totals if r[0] >= top_threshold)
            if (top_ask_vol + top_bid_vol) >= row_total_vol * 0.22 and (
                top_ask_vol > top_bid_vol * 1.12 or self.close < self.open
            ):
                self.absorption = "BEAR"
                max_top = max((r for r in row_totals if r[0] >= top_threshold), key=lambda x: x[3], default=None)
                self.absorption_price = max_top[0] if max_top else self.high
                trapped_asks = [p for p in buy_imbs if p > max(self.open, self.close)]
                if trapped_asks or (self.poc_price > max(self.open, self.close) and self.close <= self.open):
                    self.trapped = "TRAPPED_LONGS"
                    z_low = min(trapped_asks) if trapped_asks else max(self.open, self.close)
                    self.trapped_zone = (round(z_low, 8), round(self.high, 8))

        if lower_wick_ratio >= 0.28 and row_total_vol > 0 and self.absorption is None:
            bot_bid_vol = sum(r[1] for r in row_totals if r[0] <= bot_threshold)
            bot_ask_vol = sum(r[2] for r in row_totals if r[0] <= bot_threshold)
            if (bot_bid_vol + bot_ask_vol) >= row_total_vol * 0.22 and (
                bot_bid_vol > bot_ask_vol * 1.12 or self.close > self.open
            ):
                self.absorption = "BULL"
                max_bot = max((r for r in row_totals if r[0] <= bot_threshold), key=lambda x: x[3], default=None)
                self.absorption_price = max_bot[0] if max_bot else self.low
                trapped_bids = [p for p in sell_imbs if p < min(self.open, self.close)]
                if trapped_bids or (self.poc_price < min(self.open, self.close) and self.close >= self.open):
                    self.trapped = "TRAPPED_SHORTS"
                    z_high = max(trapped_bids) + self.row_size if trapped_bids else min(self.open, self.close)
                    self.trapped_zone = (round(self.low, 8), round(z_high, 8))

        self.divergence = None
        if self.close > self.open and self.delta < -avg_row_vol * 0.5:
            self.divergence = "BEAR_DIV"
        elif self.close < self.open and self.delta > avg_row_vol * 0.5:
            self.divergence = "BULL_DIV"

    def _find_stacked(self, prices: List[float], direction: str, min_count: int) -> List[Dict[str, Any]]:
        if len(prices) < min_count:
            return []
        zones: List[Dict[str, Any]] = []
        run: List[float] = [prices[0]]
        for p in prices[1:]:
            if abs((p - run[-1]) - self.row_size) <= self.row_size * 0.15:
                run.append(p)
            else:
                if len(run) >= min_count:
                    zones.append({
                        "direction": direction,
                        "low": run[0],
                        "high": round(run[-1] + self.row_size, 8),
                        "bar_time": self.open_time,
                        "count": len(run),
                    })
                run = [p]
        if len(run) >= min_count:
            zones.append({
                "direction": direction,
                "low": run[0],
                "high": round(run[-1] + self.row_size, 8),
                "bar_time": self.open_time,
                "count": len(run),
            })
        return zones

    def to_dict(self, include_rows: bool = True) -> Dict[str, Any]:
        rows_list = []
        if include_rows and self.rows:
            for p in sorted(self.rows.keys()):
                b, a = self.rows[p]
                rows_list.append({
                    "p": p,
                    "b": round(b, 4),
                    "a": round(a, 4),
                    "bi": p in self.buy_imbalances,
                    "si": p in self.sell_imbalances,
                    "poc": p == self.poc_price,
                })
        return {
            "t": self.open_time,
            "ct": self.close_time,
            "o": self.open,
            "h": self.high,
            "l": self.low,
            "c": self.close,
            "v": round(self.volume, 4),
            "bv": round(self.bybit_volume, 4),
            "rs": self.row_size,
            "closed": self.is_closed,
            "fp": self.fp_source == "LIVE",
            "fp_mode": self.fp_source,
            "delta": round(self.delta, 4),
            "bn_delta": round(self.binance_delta, 4),
            "bb_delta": round(self.bybit_delta, 4),
            "cvd": round(self.cvd, 4),
            "poc": self.poc_price,
            "val": self.val_price,
            "vah": self.vah_price,
            "stacked": self.stacked_zones,
            "abs": self.absorption,
            "abs_p": self.absorption_price,
            "trap": self.trapped,
            "trap_z": list(self.trapped_zone) if self.trapped_zone else None,
            "uh": self.unfinished_high,
            "ul": self.unfinished_low,
            "exh": self.exhaustion,
            "div": self.divergence,
            "sdiv": self.swing_div,
            "liq_b": round(self.liq_buy_usd, 2),
            "liq_s": round(self.liq_sell_usd, 2),
            "rows": rows_list,
        }


class SymbolOrderflowState:
    TIMEFRAMES = ("5m", "15m", "1h", "4h", "1d")

    def __init__(self, symbol: str, max_candles: int = 300):
        self.symbol = symbol.upper()
        self.max_candles = max_candles
        self.lock = threading.RLock()

        self.candles: Dict[str, List[FootprintCandle]] = {tf: [] for tf in self.TIMEFRAMES}
        self.last_price: float = 0.0
        self.bybit_last_price: float = 0.0
        self.price_change_24h: float = 0.0
        self.bn_open_interest: float = 0.0
        self.bb_open_interest: float = 0.0
        self.oi_history: Deque[Tuple[int, float]] = deque(maxlen=160)

        self.bn_bids: Dict[float, float] = {}
        self.bn_asks: Dict[float, float] = {}
        self.bb_bids: Dict[float, float] = {}
        self.bb_asks: Dict[float, float] = {}

        self.bids: List[Tuple[float, float]] = []
        self.asks: List[Tuple[float, float]] = []
        self.bid_walls: List[Dict[str, float]] = []
        self.ask_walls: List[Dict[str, float]] = []
        self.ob_imbalance_pct: float = 0.0
        self.has_deep_walls: bool = False

        self.recent_liqs: Deque[Dict[str, Any]] = deque(maxlen=80)
        self.seen_bybit_trade_ids: Deque[str] = deque(maxlen=4000)
        self._seen_bb_set: Set[str] = set()

        self.scanner_state: str = "WATCHING"
        self.scanner_note: str = "Monitoring 1D/4H/1H liquidity levels"
        self.active_bias: str = "RANGE"
        self.tier2_active: bool = False

        self.min_bn_agg_id: Optional[int] = None
        self.backfilled_pages: int = 0
        self._cached_lmap: Dict[str, Any] = {}
        self._cached_lmap_ts: float = 0.0

    @property
    def combined_oi(self) -> float:
        return self.bn_open_interest + self.bb_open_interest

    def load_historical_klines(self, tf: str, raw_klines: List[List[Any]]) -> None:
        with self.lock:
            existing_map = {c.open_time: c for c in self.candles[tf]}
            new_list: List[FootprintCandle] = []
            cvd = 0.0
            now_ms = int(time.time() * 1000)

            for k in raw_klines:
                open_t = int(k[0])
                o, h, l, c_px = float(k[1]), float(k[2]), float(k[3]), float(k[4])
                vol = float(k[5])
                close_t = int(k[6])
                q_vol = float(k[7])
                taker_buy = float(k[9])

                if open_t in existing_map and existing_map[open_t].fp_source == "LIVE":
                    candle = existing_map[open_t]
                    candle.open, candle.high, candle.low, candle.close = o, h, l, c_px
                    candle.volume = max(candle.volume, vol)
                else:
                    rs = compute_row_size(self.symbol, c_px, tf)
                    candle = FootprintCandle(
                        symbol=self.symbol, tf=tf, open_time=open_t, close_time=close_t,
                        open=o, high=h, low=l, close=c_px, volume=vol, quote_volume=q_vol,
                        taker_buy_vol=taker_buy, row_size=rs, is_closed=(now_ms > close_t),
                    )
                    if tf in ("5m", "15m", "1h"):
                        candle.build_estimated_rows()
                    candle.compute_metrics()

                cvd += candle.delta
                candle.cvd = round(cvd, 6)
                new_list.append(candle)

            self.candles[tf] = new_list[-self.max_candles:]
            if tf in ("5m", "15m"):
                self._update_swing_divergence(self.candles[tf])
            if new_list and self.last_price <= 0:
                self.last_price = new_list[-1].close

    def process_trade(
        self,
        price: float,
        qty: float,
        is_sell_taker: bool,
        ts_ms: int,
        exchange: str = "BINANCE",
        agg_id: Optional[int] = None,
        bybit_id: Optional[str] = None,
    ) -> None:
        with self.lock:
            if exchange == "BYBIT":
                if bybit_id:
                    if bybit_id in self._seen_bb_set:
                        return
                    if len(self.seen_bybit_trade_ids) == self.seen_bybit_trade_ids.maxlen:
                        old = self.seen_bybit_trade_ids.popleft()
                        self._seen_bb_set.discard(old)
                    self.seen_bybit_trade_ids.append(bybit_id)
                    self._seen_bb_set.add(bybit_id)
                self.bybit_last_price = price
            else:
                self.last_price = price
                if agg_id is not None:
                    if self.min_bn_agg_id is None or agg_id < self.min_bn_agg_id:
                        self.min_bn_agg_id = agg_id

            for tf in ("5m", "15m", "1h"):
                bars = self.candles[tf]
                tf_ms = TF_MS_MAP[tf]
                bar_open = (ts_ms // tf_ms) * tf_ms
                bar_close = bar_open + tf_ms - 1

                if not bars:
                    rs = compute_row_size(self.symbol, price, tf)
                    bars.append(FootprintCandle(
                        symbol=self.symbol, tf=tf, open_time=bar_open, close_time=bar_close,
                        open=price, high=price, low=price, close=price, row_size=rs,
                    ))

                last = bars[-1]
                if bar_open > last.open_time:
                    last.is_closed = True
                    last.compute_metrics()
                    prev_cvd = last.cvd
                    rs = compute_row_size(self.symbol, price, tf)
                    c = FootprintCandle(
                        symbol=self.symbol, tf=tf, open_time=bar_open, close_time=bar_close,
                        open=price, high=price, low=price, close=price, row_size=rs, cvd=prev_cvd,
                    )
                    bars.append(c)
                    if len(bars) > self.max_candles:
                        bars.pop(0)
                    last = c

                if bar_open == last.open_time:
                    last.add_trade(price, qty, is_sell_taker, exchange=exchange, agg_id=agg_id)
                    prev_bar_cvd = bars[-2].cvd if len(bars) >= 2 else 0.0
                    last.cvd = round(prev_bar_cvd + last.delta, 6)

    def apply_backfill_trades(
        self, trades: List[Dict[str, Any]], imbalance_ratio: float = 3.0, stacked_min: int = 3
    ) -> None:
        if not trades:
            return
        with self.lock:
            touched = set()
            for tr in trades:
                agg_id = int(tr["a"])
                px = float(tr["p"])
                qty = float(tr["q"])
                is_sell = bool(tr["m"])
                ts_ms = int(tr["T"])

                if self.min_bn_agg_id is None or agg_id < self.min_bn_agg_id:
                    self.min_bn_agg_id = agg_id

                for tf in ("5m", "15m", "1h"):
                    bars = self.candles[tf]
                    if not bars:
                        continue
                    tf_ms = TF_MS_MAP[tf]
                    bar_open = (ts_ms // tf_ms) * tf_ms
                    for b in reversed(bars):
                        if b.open_time == bar_open:
                            b.add_backfill_trade(px, qty, is_sell, agg_id)
                            touched.add((tf, b.open_time))
                            break
                        if b.open_time < bar_open:
                            break

            for tf in ("5m", "15m", "1h"):
                bars = self.candles[tf]
                cvd = 0.0
                for b in bars:
                    if (tf, b.open_time) in touched or b._dirty:
                        b.compute_metrics(imbalance_ratio, stacked_min)
                    cvd += b.delta
                    b.cvd = round(cvd, 6)
                if tf in ("5m", "15m"):
                    self._update_swing_divergence(bars)

    def ensure_metrics_fresh(self, tf: str, imbalance_ratio: float = 3.0, stacked_min: int = 3) -> None:
        with self.lock:
            bars = self.candles.get(tf, [])
            if not bars:
                return
            last = bars[-1]
            if last._dirty:
                last.compute_metrics(imbalance_ratio, stacked_min)
                prev_cvd = bars[-2].cvd if len(bars) >= 2 else 0.0
                last.cvd = round(prev_cvd + last.delta, 6)
                if tf in ("5m", "15m"):
                    self._update_swing_divergence(bars)

    def process_kline_ws(
        self, tf: str, k: Dict[str, Any], imbalance_ratio: float = 3.0, stacked_min: int = 3
    ) -> Optional[FootprintCandle]:
        with self.lock:
            bars = self.candles.get(tf)
            if bars is None:
                return None
            open_t = int(k["t"])
            close_t = int(k["T"])
            o, h, l, c_px = float(k["o"]), float(k["h"]), float(k["l"]), float(k["c"])
            vol = float(k["v"])
            q_vol = float(k["q"])
            taker_buy = float(k["V"])
            is_closed = bool(k["x"])

            self.last_price = c_px
            target: Optional[FootprintCandle] = None
            if bars and bars[-1].open_time == open_t:
                target = bars[-1]
            elif not bars or open_t > bars[-1].open_time:
                if bars:
                    bars[-1].is_closed = True
                    bars[-1].compute_metrics(imbalance_ratio, stacked_min)
                rs = compute_row_size(self.symbol, c_px, tf)
                target = FootprintCandle(
                    symbol=self.symbol, tf=tf, open_time=open_t, close_time=close_t,
                    open=o, high=h, low=l, close=c_px, row_size=rs,
                )
                bars.append(target)
                if len(bars) > self.max_candles:
                    bars.pop(0)

            if target is None:
                return None

            target.open, target.high, target.low, target.close = o, h, l, c_px
            target.is_closed = is_closed
            if target.fp_source != "LIVE":
                target.volume = vol
                target.quote_volume = q_vol
                target.taker_buy_vol = taker_buy
                if tf in ("5m", "15m", "1h"):
                    target.build_estimated_rows()
            if is_closed or target._dirty or target.fp_source != "LIVE":
                target.compute_metrics(imbalance_ratio, stacked_min)

            prev_cvd = bars[-2].cvd if len(bars) >= 2 else 0.0
            target.cvd = round(prev_cvd + target.delta, 6)
            if is_closed and tf in ("5m", "15m"):
                self._update_swing_divergence(bars)
            return target if is_closed else None

    @staticmethod
    def _update_swing_divergence(bars: List[FootprintCandle]) -> None:
        if len(bars) < 10:
            return
        last = bars[-1]
        last.swing_div = None
        window = bars[-10:-1]
        max_bar = max(window, key=lambda b: b.high)
        min_bar = min(window, key=lambda b: b.low)
        if last.high > max_bar.high and last.cvd < max_bar.cvd:
            last.swing_div = "SWING_BEAR_DIV"
        elif last.low < min_bar.low and last.cvd > min_bar.cvd:
            last.swing_div = "SWING_BULL_DIV"

    def process_binance_depth(self, bids_raw: List[List[str]], asks_raw: List[List[str]], is_deep: bool = False) -> None:
        with self.lock:
            self.bn_bids = {float(p): float(q) for p, q in bids_raw if float(q) > 0}
            self.bn_asks = {float(p): float(q) for p, q in asks_raw if float(q) > 0}
            if is_deep:
                self.has_deep_walls = True
            self._rebuild_merged_orderbook()

    def process_bybit_depth(self, msg_type: str, data: Dict[str, Any]) -> None:
        with self.lock:
            u = int(data.get("u", 0))
            if msg_type == "snapshot" or u == 1:
                self.bb_bids.clear()
                self.bb_asks.clear()
            for p_s, q_s in data.get("b", []):
                p, q = float(p_s), float(q_s)
                if q <= 0:
                    self.bb_bids.pop(p, None)
                else:
                    self.bb_bids[p] = q
            for p_s, q_s in data.get("a", []):
                p, q = float(p_s), float(q_s)
                if q <= 0:
                    self.bb_asks.pop(p, None)
                else:
                    self.bb_asks[p] = q
            self._rebuild_merged_orderbook()

    def _rebuild_merged_orderbook(self) -> None:
        ref_px = self.last_price
        if ref_px <= 0 and self.bn_bids:
            ref_px = max(self.bn_bids.keys())
        if ref_px <= 0:
            return

        rs_ui = compute_row_size(self.symbol, ref_px, "5m")
        rs_wall = compute_row_size(self.symbol, ref_px, "15m")

        b_ui: Dict[float, float] = {}
        a_ui: Dict[float, float] = {}
        for src in (self.bn_bids, self.bb_bids):
            for p, q in src.items():
                bp = snap_bucket(p, rs_ui)
                b_ui[bp] = b_ui.get(bp, 0.0) + q
        for src in (self.bn_asks, self.bb_asks):
            for p, q in src.items():
                bp = snap_bucket(p, rs_ui)
                a_ui[bp] = a_ui.get(bp, 0.0) + q

        self.bids = sorted(b_ui.items(), key=lambda x: x[0], reverse=True)
        self.asks = sorted(a_ui.items(), key=lambda x: x[0])

        lo_band, hi_band = ref_px * 0.985, ref_px * 1.015
        bid_usd = sum(p * q for p, q in self.bids if lo_band <= p <= hi_band)
        ask_usd = sum(p * q for p, q in self.asks if lo_band <= p <= hi_band)
        tot = bid_usd + ask_usd
        self.ob_imbalance_pct = round(((bid_usd - ask_usd) / tot) * 100.0, 2) if tot > 0 else 0.0

        b_wall_map: Dict[float, float] = {}
        a_wall_map: Dict[float, float] = {}
        for src in (self.bn_bids, self.bb_bids):
            for p, q in src.items():
                bp = snap_bucket(p, rs_wall)
                b_wall_map[bp] = b_wall_map.get(bp, 0.0) + q
        for src in (self.bn_asks, self.bb_asks):
            for p, q in src.items():
                bp = snap_bucket(p, rs_wall)
                a_wall_map[bp] = a_wall_map.get(bp, 0.0) + q

        b_usd = [(p, q, p * q) for p, q in b_wall_map.items()]
        a_usd = [(p, q, p * q) for p, q in a_wall_map.items()]
        med_b = statistics.median([x[2] for x in b_usd]) if b_usd else 0.0
        med_a = statistics.median([x[2] for x in a_usd]) if a_usd else 0.0

        min_wall_usd = 800_000 if self.symbol == "BTCUSDT" else (350_000 if self.symbol == "ETHUSDT" else 100_000)
        self.bid_walls = [
            {"price": p, "qty": round(q, 3), "usd": round(u, 2)}
            for p, q, u in sorted(b_usd, key=lambda x: x[2], reverse=True)
            if u >= max(med_b * 2.5, min_wall_usd)
        ][:5]
        self.ask_walls = [
            {"price": p, "qty": round(q, 3), "usd": round(u, 2)}
            for p, q, u in sorted(a_usd, key=lambda x: x[2], reverse=True)
            if u >= max(med_a * 2.5, min_wall_usd)
        ][:5]

    def process_liquidation(self, exchange: str, price: float, qty: float, ts_ms: int, side_hint: str = "") -> None:
        with self.lock:
            usd = price * qty
            ref_px = self.last_price or price
            is_upside_squeeze = (side_hint == "BUY" and exchange == "BINANCE") or (price >= ref_px)
            self.recent_liqs.append({
                "ex": exchange,
                "price": price,
                "qty": qty,
                "usd": round(usd, 2),
                "ts": ts_ms,
                "squeeze": is_upside_squeeze,
            })
            for tf in ("5m", "15m", "1h"):
                bars = self.candles[tf]
                if bars and bars[-1].open_time <= ts_ms <= bars[-1].close_time:
                    if is_upside_squeeze:
                        bars[-1].liq_buy_usd += usd
                    else:
                        bars[-1].liq_sell_usd += usd

    def update_oi(self, oi: float, ts_ms: int, exchange: str = "BINANCE") -> None:
        with self.lock:
            if exchange == "BYBIT":
                self.bb_open_interest = oi
            else:
                self.bn_open_interest = oi
            self.oi_history.append((ts_ms, self.combined_oi))

    def get_atr(self, tf: str = "15m", period: int = 14) -> float:
        with self.lock:
            bars = [b for b in self.candles.get(tf, []) if b.is_closed]
            if len(bars) < period + 2:
                if bars:
                    return sum(b.high - b.low for b in bars) / len(bars)
                return 0.0
            trs: List[float] = []
            for i in range(1, len(bars)):
                tr = max(
                    bars[i].high - bars[i].low,
                    abs(bars[i].high - bars[i - 1].close),
                    abs(bars[i].low - bars[i - 1].close),
                )
                trs.append(tr)
            return sum(trs[-period:]) / period

    def get_naked_pocs(self, tf: str, max_count: int = 8) -> List[Dict[str, Any]]:
        with self.lock:
            bars = self.candles.get(tf, [])
            if len(bars) < 3:
                return []
            npocs: List[Dict[str, Any]] = []
            for i in range(len(bars) - 2):
                c = bars[i]
                if not c.is_closed or c.fp_source != "LIVE" or c.poc_price <= 0:
                    continue
                touched = False
                for j in range(i + 1, len(bars)):
                    if bars[j].low <= c.poc_price <= bars[j].high:
                        touched = True
                        break
                if not touched:
                    npocs.append({
                        "price": c.poc_price,
                        "time": c.open_time,
                        "vol": c.poc_vol,
                        "side": "ABOVE" if c.poc_price > bars[-1].close else "BELOW",
                    })
            return npocs[-max_count:]

    def get_session_vwap(self, tf: str = "15m") -> float:
        with self.lock:
            bars = self.candles.get(tf, [])
            if not bars:
                return 0.0
            now_sec = time.time()
            utc_midnight_ms = int((now_sec - (now_sec % 86400)) * 1000)
            pv_sum = 0.0
            v_sum = 0.0
            for c in bars:
                if c.open_time >= utc_midnight_ms and c.volume > 0:
                    tp = (c.high + c.low + c.close) / 3.0
                    pv_sum += tp * c.volume
                    v_sum += c.volume
            return round(pv_sum / v_sum, 8) if v_sum > 0 else bars[-1].close

    def get_visible_volume_profile(self, tf: str = "15m", lookback: int = 250) -> Dict[str, Any]:
        with self.lock:
            bars = self.candles.get(tf, [])[-lookback:]
            if not bars:
                return {"poc": 0.0, "vah": 0.0, "val": 0.0, "bins": []}
            rs = compute_row_size(self.symbol, bars[-1].close, tf)
            profile: Dict[float, List[float]] = {}

            for c in bars:
                if c.rows:
                    for p, (b, a) in c.rows.items():
                        bp = snap_bucket(p, rs)
                        if bp not in profile:
                            profile[bp] = [0.0, 0.0]
                        profile[bp][0] += b
                        profile[bp][1] += a
                else:
                    lo_b = snap_bucket(c.low, rs)
                    hi_b = snap_bucket(c.high, rs)
                    n_steps = max(1, min(32, int(round((hi_b - lo_b) / rs)) + 1))
                    ask_share = max(0.0, min(c.volume, c.taker_buy_vol))
                    bid_share = max(0.0, c.volume - ask_share)
                    for s in range(n_steps):
                        bp = round(lo_b + s * rs, 8)
                        if bp not in profile:
                            profile[bp] = [0.0, 0.0]
                        profile[bp][0] += bid_share / n_steps
                        profile[bp][1] += ask_share / n_steps

            if not profile:
                return {"poc": 0.0, "vah": 0.0, "val": 0.0, "bins": []}

            sorted_p = sorted(profile.keys())
            totals = [(p, profile[p][0], profile[p][1], profile[p][0] + profile[p][1]) for p in sorted_p]
            tot_vol = sum(x[3] for x in totals)
            poc_idx = max(range(len(totals)), key=lambda i: totals[i][3])
            poc_price = totals[poc_idx][0]

            target_va = tot_vol * 0.70
            va_vol = totals[poc_idx][3]
            lo, hi = poc_idx, poc_idx
            while va_vol < target_va and (lo > 0 or hi < len(totals) - 1):
                dv = totals[lo - 1][3] if lo > 0 else -1.0
                uv = totals[hi + 1][3] if hi < len(totals) - 1 else -1.0
                if uv >= dv and hi < len(totals) - 1:
                    hi += 1
                    va_vol += totals[hi][3]
                elif lo > 0:
                    lo -= 1
                    va_vol += totals[lo][3]
                else:
                    break

            bins = [
                {"p": p, "b": round(b, 3), "a": round(a, 3), "t": round(t, 3)}
                for p, b, a, t in totals
            ]
            return {
                "poc": poc_price,
                "val": totals[lo][0],
                "vah": round(totals[hi][0] + rs, 8),
                "rs": rs,
                "bins": bins,
            }