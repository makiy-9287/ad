import math
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Deque, Dict, List, Optional, Tuple

from config import compute_row_size


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
    quote_volume: float = 0.0
    taker_buy_vol: float = 0.0
    row_size: float = 1.0
    is_closed: bool = False
    has_footprint: bool = False

    # bucket_price -> [bid_vol (sell aggressor), ask_vol (buy aggressor)]
    rows: Dict[float, List[float]] = field(default_factory=dict)

    # Computed orderflow metrics
    delta: float = 0.0
    cvd: float = 0.0
    poc_price: float = 0.0
    poc_vol: float = 0.0
    val_price: float = 0.0
    vah_price: float = 0.0
    buy_imbalances: List[float] = field(default_factory=list)
    sell_imbalances: List[float] = field(default_factory=list)
    stacked_zones: List[Dict[str, Any]] = field(default_factory=list)
    absorption: Optional[str] = None         # "BULL" | "BEAR" | None
    absorption_price: Optional[float] = None
    trapped: Optional[str] = None            # "TRAPPED_SHORTS" | "TRAPPED_LONGS" | None
    trapped_zone: Optional[Tuple[float, float]] = None
    unfinished_high: bool = False
    unfinished_low: bool = False
    divergence: Optional[str] = None         # "BULL_DIV" | "BEAR_DIV" | None
    liq_buy_usd: float = 0.0                 # Shorts liquidated (buy forceOrder)
    liq_sell_usd: float = 0.0                # Longs liquidated (sell forceOrder)

    def add_trade(self, price: float, qty: float, is_buyer_maker: bool) -> None:
        if self.row_size <= 0:
            self.row_size = compute_row_size(self.symbol, price, self.tf)
        bucket = snap_bucket(price, self.row_size)
        if bucket not in self.rows:
            self.rows[bucket] = [0.0, 0.0]
        if is_buyer_maker:
            # Buyer is maker -> Market Sell hit the Bid
            self.rows[bucket][0] += qty
        else:
            # Seller is maker -> Market Buy lifted the Ask
            self.rows[bucket][1] += qty
            self.taker_buy_vol += qty

        self.has_footprint = True
        if self.open <= 0:
            self.open = price
        if self.high <= 0 or price > self.high:
            self.high = price
        if self.low <= 0 or price < self.low:
            self.low = price
        self.close = price
        self.volume += qty
        self.quote_volume += price * qty

    def compute_metrics(self, imbalance_ratio: float = 3.0, stacked_min: int = 3) -> None:
        if not self.has_footprint or not self.rows:
            # Fallback from Binance kline taker_buy_vol for historical candles
            self.delta = round(2.0 * self.taker_buy_vol - self.volume, 6)
            self.poc_price = round((self.high + self.low + self.close) / 3.0, 8)
            return

        sorted_prices = sorted(self.rows.keys())
        total_bid = 0.0
        total_ask = 0.0
        row_totals: List[Tuple[float, float, float, float]] = []  # (p, bid, ask, tot)

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

        self.delta = round(total_ask - total_bid, 6)
        self.poc_price = row_totals[poc_idx][0]
        self.poc_vol = round(max_vol, 6)

        total_row_vol = total_bid + total_ask
        avg_row_vol = (total_row_vol / len(row_totals)) if row_totals else 0.0

        # 70% Value Area around POC
        if total_row_vol > 0 and len(row_totals) > 0:
            target_va = total_row_vol * 0.70
            va_vol = row_totals[poc_idx][3]
            lo = poc_idx
            hi = poc_idx
            while va_vol < target_va and (lo > 0 or hi < len(row_totals) - 1):
                down_vol = row_totals[lo - 1][3] if lo > 0 else -1.0
                up_vol = row_totals[hi + 1][3] if hi < len(row_totals) - 1 else -1.0
                if up_vol >= down_vol and hi < len(row_totals) - 1:
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

        # Unfinished Auction check at extreme rows (both bid and ask traded significantly)
        if row_totals:
            lo_bid, lo_ask = row_totals[0][1], row_totals[0][2]
            hi_bid, hi_ask = row_totals[-1][1], row_totals[-1][2]
            min_edge_vol = avg_row_vol * 0.10
            self.unfinished_low = lo_bid > min_edge_vol and lo_ask > min_edge_vol
            self.unfinished_high = hi_bid > min_edge_vol and hi_ask > min_edge_vol

        # Diagonal Imbalances (Ask[i] vs Bid[i-1] for Buy; Bid[i] vs Ask[i+1] for Sell)
        buy_imbs: List[float] = []
        sell_imbs: List[float] = []
        min_imb_vol = avg_row_vol * 0.35

        for i in range(len(row_totals)):
            p, bid_i, ask_i, _ = row_totals[i]
            # Buy imbalance: Ask at row i vs Bid at row i-1 (contiguous row check)
            if i > 0 and abs((p - row_totals[i - 1][0]) - self.row_size) <= self.row_size * 0.15:
                bid_below = row_totals[i - 1][1]
                if ask_i >= min_imb_vol:
                    if (bid_below == 0 and ask_i >= avg_row_vol * 0.8) or (
                        bid_below > 0 and ask_i >= imbalance_ratio * bid_below
                    ):
                        buy_imbs.append(p)

            # Sell imbalance: Bid at row i vs Ask at row i+1 (contiguous row check)
            if i < len(row_totals) - 1 and abs((row_totals[i + 1][0] - p) - self.row_size) <= self.row_size * 0.15:
                ask_above = row_totals[i + 1][2]
                if bid_i >= min_imb_vol:
                    if (ask_above == 0 and bid_i >= avg_row_vol * 0.8) or (
                        ask_above > 0 and bid_i >= imbalance_ratio * ask_above
                    ):
                        sell_imbs.append(p)

        self.buy_imbalances = buy_imbs
        self.sell_imbalances = sell_imbs

        # Stacked Imbalances (3+ contiguous imbalanced rows)
        stacked: List[Dict[str, Any]] = []
        stacked.extend(self._find_stacked(buy_imbs, "BULL", stacked_min))
        stacked.extend(self._find_stacked(sell_imbs, "BEAR", stacked_min))
        self.stacked_zones = stacked

        # Absorption & Trapped Traders detection
        self.absorption = None
        self.absorption_price = None
        self.trapped = None
        self.trapped_zone = None

        candle_range = max(self.high - self.low, self.row_size)
        upper_wick = self.high - max(self.open, self.close)
        lower_wick = min(self.open, self.close) - self.low
        upper_wick_ratio = upper_wick / candle_range
        lower_wick_ratio = lower_wick / candle_range

        top_threshold = self.high - candle_range * 0.35
        bot_threshold = self.low + candle_range * 0.35

        # Bearish Absorption at Top Wick (Heavy Ask buying absorbed -> rejected lower)
        if upper_wick_ratio >= 0.30 and total_row_vol > 0:
            top_ask_vol = sum(r[2] for r in row_totals if r[0] >= top_threshold)
            top_bid_vol = sum(r[1] for r in row_totals if r[0] >= top_threshold)
            top_tot = top_ask_vol + top_bid_vol
            if top_tot >= total_row_vol * 0.25 and (top_ask_vol > top_bid_vol * 1.15 or self.close < self.open):
                self.absorption = "BEAR"
                max_top_row = max((r for r in row_totals if r[0] >= top_threshold), key=lambda x: x[3], default=None)
                self.absorption_price = max_top_row[0] if max_top_row else self.high

                # Trapped Longs: Buy imbalances OR POC trapped inside the upper wick above close
                trapped_asks = [p for p in buy_imbs if p > max(self.open, self.close)]
                if trapped_asks or (self.poc_price > max(self.open, self.close) and self.close <= self.open):
                    self.trapped = "TRAPPED_LONGS"
                    z_low = min(trapped_asks) if trapped_asks else max(self.open, self.close)
                    self.trapped_zone = (round(z_low, 8), round(self.high, 8))

        # Bullish Absorption at Bottom Wick (Heavy Bid selling absorbed -> rejected higher)
        if lower_wick_ratio >= 0.30 and total_row_vol > 0 and self.absorption is None:
            bot_bid_vol = sum(r[1] for r in row_totals if r[0] <= bot_threshold)
            bot_ask_vol = sum(r[2] for r in row_totals if r[0] <= bot_threshold)
            bot_tot = bot_bid_vol + bot_ask_vol
            if bot_tot >= total_row_vol * 0.25 and (bot_bid_vol > bot_ask_vol * 1.15 or self.close > self.open):
                self.absorption = "BULL"
                max_bot_row = max((r for r in row_totals if r[0] <= bot_threshold), key=lambda x: x[3], default=None)
                self.absorption_price = max_bot_row[0] if max_bot_row else self.low

                # Trapped Shorts: Sell imbalances OR POC trapped inside the lower wick below close
                trapped_bids = [p for p in sell_imbs if p < min(self.open, self.close)]
                if trapped_bids or (self.poc_price < min(self.open, self.close) and self.close >= self.open):
                    self.trapped = "TRAPPED_SHORTS"
                    z_high = max(trapped_bids) + self.row_size if trapped_bids else min(self.open, self.close)
                    self.trapped_zone = (round(self.low, 8), round(z_high, 8))

        # Single-bar Delta Divergence
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
        if include_rows and self.has_footprint:
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
            "rs": self.row_size,
            "closed": self.is_closed,
            "fp": self.has_footprint,
            "delta": round(self.delta, 4),
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
            "div": self.divergence,
            "liq_b": round(self.liq_buy_usd, 2),
            "liq_s": round(self.liq_sell_usd, 2),
            "rows": rows_list,
        }


class SymbolOrderflowState:
    TIMEFRAMES = ("1m", "5m", "15m", "1h", "4h")

    def __init__(self, symbol: str, max_candles: int = 180):
        self.symbol = symbol.upper()
        self.max_candles = max_candles
        self.lock = threading.RLock()

        self.candles: Dict[str, List[FootprintCandle]] = {tf: [] for tf in self.TIMEFRAMES}
        self.last_price: float = 0.0
        self.price_change_24h: float = 0.0
        self.open_interest: float = 0.0
        self.oi_history: Deque[Tuple[int, float]] = deque(maxlen=120)

        # Live L2 Orderbook snapshot (top 20 bids/asks)
        self.bids: List[Tuple[float, float]] = []
        self.asks: List[Tuple[float, float]] = []
        self.bid_walls: List[Dict[str, float]] = []
        self.ask_walls: List[Dict[str, float]] = []
        self.ob_imbalance_pct: float = 0.0
        self.last_depth_ts: int = 0

        # Recent liquidations buffer
        self.recent_liqs: Deque[Dict[str, Any]] = deque(maxlen=60)

        # Engine status badge for dashboard
        self.scanner_state: str = "SCANNING"      # SCANNING | AT_LEVEL | SWEEP_DETECTED | ACTIVE_SIGNAL
        self.scanner_note: str = "Monitoring structure & liquidity pools"
        self.active_bias: str = "NEUTRAL"

    def load_historical_klines(self, tf: str, raw_klines: List[List[Any]]) -> None:
        with self.lock:
            existing_map = {c.open_time: c for c in self.candles[tf]}
            new_list: List[FootprintCandle] = []
            cvd = 0.0
            for k in raw_klines:
                open_t = int(k[0])
                o, h, l, c_px = float(k[1]), float(k[2]), float(k[3]), float(k[4])
                vol = float(k[5])
                close_t = int(k[6])
                q_vol = float(k[7])
                taker_buy = float(k[9])

                if open_t in existing_map and existing_map[open_t].has_footprint:
                    candle = existing_map[open_t]
                    candle.open, candle.high, candle.low, candle.close = o, h, l, c_px
                else:
                    rs = compute_row_size(self.symbol, c_px, tf)
                    candle = FootprintCandle(
                        symbol=self.symbol,
                        tf=tf,
                        open_time=open_t,
                        close_time=close_t,
                        open=o,
                        high=h,
                        low=l,
                        close=c_px,
                        volume=vol,
                        quote_volume=q_vol,
                        taker_buy_vol=taker_buy,
                        row_size=rs,
                        is_closed=(int(time.time() * 1000) > close_t),
                        has_footprint=False,
                    )
                    candle.compute_metrics()

                cvd += candle.delta
                candle.cvd = round(cvd, 6)
                new_list.append(candle)

            self.candles[tf] = new_list[-self.max_candles:]
            if new_list:
                self.last_price = new_list[-1].close

    def process_agg_trade(
        self,
        price: float,
        qty: float,
        is_buyer_maker: bool,
        ts_ms: int,
        imbalance_ratio: float = 3.0,
        stacked_min: int = 3,
    ) -> None:
        with self.lock:
            self.last_price = price
            for tf in ("1m", "5m", "15m", "1h"):
                bars = self.candles[tf]
                tf_ms = self._tf_to_ms(tf)
                bar_open = (ts_ms // tf_ms) * tf_ms
                bar_close = bar_open + tf_ms - 1

                if not bars:
                    rs = compute_row_size(self.symbol, price, tf)
                    c = FootprintCandle(
                        symbol=self.symbol,
                        tf=tf,
                        open_time=bar_open,
                        close_time=bar_close,
                        open=price,
                        high=price,
                        low=price,
                        close=price,
                        row_size=rs,
                    )
                    bars.append(c)

                last = bars[-1]
                if bar_open > last.open_time:
                    last.is_closed = True
                    last.compute_metrics(imbalance_ratio, stacked_min)
                    prev_cvd = last.cvd
                    rs = compute_row_size(self.symbol, price, tf)
                    c = FootprintCandle(
                        symbol=self.symbol,
                        tf=tf,
                        open_time=bar_open,
                        close_time=bar_close,
                        open=price,
                        high=price,
                        low=price,
                        close=price,
                        row_size=rs,
                        cvd=prev_cvd,
                    )
                    bars.append(c)
                    if len(bars) > self.max_candles:
                        bars.pop(0)
                    last = c

                if bar_open == last.open_time:
                    last.add_trade(price, qty, is_buyer_maker)
                    last.compute_metrics(imbalance_ratio, stacked_min)
                    prev_bar_cvd = bars[-2].cvd if len(bars) >= 2 else 0.0
                    last.cvd = round(prev_bar_cvd + last.delta, 6)

    def process_kline_ws(
        self,
        tf: str,
        k: Dict[str, Any],
        imbalance_ratio: float = 3.0,
        stacked_min: int = 3,
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

            target: Optional[FootprintCandle] = None
            if bars and bars[-1].open_time == open_t:
                target = bars[-1]
            elif not bars or open_t > bars[-1].open_time:
                if bars:
                    bars[-1].is_closed = True
                rs = compute_row_size(self.symbol, c_px, tf)
                target = FootprintCandle(
                    symbol=self.symbol,
                    tf=tf,
                    open_time=open_t,
                    close_time=close_t,
                    open=o,
                    high=h,
                    low=l,
                    close=c_px,
                    row_size=rs,
                )
                bars.append(target)
                if len(bars) > self.max_candles:
                    bars.pop(0)

            if target is None:
                return None

            target.open, target.high, target.low, target.close = o, h, l, c_px
            target.is_closed = is_closed
            if not target.has_footprint:
                target.volume = vol
                target.quote_volume = q_vol
                target.taker_buy_vol = taker_buy
            target.compute_metrics(imbalance_ratio, stacked_min)
            prev_cvd = bars[-2].cvd if len(bars) >= 2 else 0.0
            target.cvd = round(prev_cvd + target.delta, 6)
            return target if is_closed else None

    def process_depth(self, bids_raw: List[List[str]], asks_raw: List[List[str]], ts_ms: int) -> None:
        with self.lock:
            bids = [(float(p), float(q)) for p, q in bids_raw if float(q) > 0]
            asks = [(float(p), float(q)) for p, q in asks_raw if float(q) > 0]
            self.bids = bids
            self.asks = asks
            self.last_depth_ts = ts_ms

            bid_usd = sum(p * q for p, q in bids)
            ask_usd = sum(p * q for p, q in asks)
            tot = bid_usd + ask_usd
            self.ob_imbalance_pct = round(((bid_usd - ask_usd) / tot) * 100.0, 2) if tot > 0 else 0.0

            # Detect resting walls (>= 2.5x average level USD depth in top 20)
            avg_bid = (bid_usd / len(bids)) if bids else 0.0
            avg_ask = (ask_usd / len(asks)) if asks else 0.0

            self.bid_walls = [
                {"price": p, "qty": q, "usd": round(p * q, 2)}
                for p, q in bids
                if avg_bid > 0 and (p * q) >= avg_bid * 2.3
            ]
            self.ask_walls = [
                {"price": p, "qty": q, "usd": round(p * q, 2)}
                for p, q in asks
                if avg_ask > 0 and (p * q) >= avg_ask * 2.3
            ]

    def process_liquidation(self, side: str, price: float, qty: float, ts_ms: int) -> None:
        with self.lock:
            usd = price * qty
            self.recent_liqs.append({
                "side": side,  # "BUY" = short liquidated, "SELL" = long liquidated
                "price": price,
                "qty": qty,
                "usd": round(usd, 2),
                "ts": ts_ms,
            })
            for tf in ("1m", "5m", "15m", "1h"):
                bars = self.candles[tf]
                if bars and bars[-1].open_time <= ts_ms <= bars[-1].close_time:
                    if side == "BUY":
                        bars[-1].liq_buy_usd += usd
                    else:
                        bars[-1].liq_sell_usd += usd

    def update_oi(self, oi: float, ts_ms: int) -> None:
        with self.lock:
            self.open_interest = oi
            self.oi_history.append((ts_ms, oi))

    def get_naked_pocs(self, tf: str, max_count: int = 8) -> List[Dict[str, Any]]:
        with self.lock:
            bars = self.candles.get(tf, [])
            if len(bars) < 3:
                return []
            npocs: List[Dict[str, Any]] = []
            # Only closed bars with footprint data produce high-precision nPOCs
            for i in range(len(bars) - 2):
                c = bars[i]
                if not c.is_closed or not c.has_footprint or c.poc_price <= 0:
                    continue
                touched = False
                for j in range(i + 1, len(bars)):
                    later = bars[j]
                    if later.low <= c.poc_price <= later.high:
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

    def get_visible_volume_profile(self, tf: str = "15m", lookback: int = 90) -> Dict[str, Any]:
        with self.lock:
            bars = self.candles.get(tf, [])[-lookback:]
            if not bars:
                return {"poc": 0.0, "vah": 0.0, "val": 0.0, "bins": []}
            rs = compute_row_size(self.symbol, bars[-1].close, tf)
            profile: Dict[float, List[float]] = {}  # bucket -> [bid, ask]

            for c in bars:
                if c.has_footprint and c.rows:
                    for p, (b, a) in c.rows.items():
                        bp = snap_bucket(p, rs)
                        if bp not in profile:
                            profile[bp] = [0.0, 0.0]
                        profile[bp][0] += b
                        profile[bp][1] += a
                else:
                    # Distribute historical bar volume across [low, high] cleanly
                    lo_b = snap_bucket(c.low, rs)
                    hi_b = snap_bucket(c.high, rs)
                    n_steps = max(1, int(round((hi_b - lo_b) / rs)) + 1)
                    n_steps = min(n_steps, 40)
                    ask_share = max(0.0, min(c.volume, c.taker_buy_vol))
                    bid_share = max(0.0, c.volume - ask_share)
                    step_b = bid_share / n_steps
                    step_a = ask_share / n_steps
                    for s in range(n_steps):
                        bp = round(lo_b + s * rs, 8)
                        if bp not in profile:
                            profile[bp] = [0.0, 0.0]
                        profile[bp][0] += step_b
                        profile[bp][1] += step_a

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

    @staticmethod
    def _tf_to_ms(tf: str) -> int:
        return {
            "1m": 60_000,
            "5m": 300_000,
            "15m": 900_000,
            "1h": 3_600_000,
            "4h": 14_400_000,
        }[tf]