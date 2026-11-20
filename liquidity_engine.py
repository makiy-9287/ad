import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

from footprint_engine import FootprintCandle, SymbolOrderflowState


@dataclass
class LiquidityPool:
    name: str
    side: str         # "BSL" (above price) | "SSL" (below price)
    price: float
    strength: int     # 1 (minor) to 5 (major institutional)
    tf: str
    meta: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "side": self.side,
            "price": round(self.price, 8),
            "strength": self.strength,
            "tf": self.tf,
            "meta": self.meta,
        }


class LiquidityEngine:
    @staticmethod
    def find_fractal_pivots(
        bars: List[FootprintCandle], left: int = 3, right: int = 3
    ) -> Tuple[List[Tuple[int, float, int]], List[Tuple[int, float, int]]]:
        """
        Returns confirmed swing highs and swing lows: (index, price, open_time).
        Only evaluates closed bars up to len(bars) - 1 - right.
        """
        highs: List[Tuple[int, float, int]] = []
        lows: List[Tuple[int, float, int]] = []
        n = len(bars)
        if n < left + right + 3:
            return highs, lows

        for i in range(left, n - right):
            c = bars[i]
            if not c.is_closed:
                continue
            is_high = all(bars[j].high <= c.high for j in range(i - left, i + right + 1) if j != i)
            is_low = all(bars[j].low >= c.low for j in range(i - left, i + right + 1) if j != i)
            # Ensure strictly higher/lower on at least one neighbor to avoid flat plateaus
            if is_high and any(bars[j].high < c.high for j in range(i - left, i + right + 1) if j != i):
                highs.append((i, c.high, c.open_time))
            if is_low and any(bars[j].low > c.low for j in range(i - left, i + right + 1) if j != i):
                lows.append((i, c.low, c.open_time))
        return highs, lows

    @staticmethod
    def determine_bias(bars: List[FootprintCandle], current_price: float) -> str:
        """Determines HTF Trend & Order Flow Bias: BULLISH | BEARISH | RANGE."""
        if len(bars) < 20:
            return "RANGE"
        highs, lows = LiquidityEngine.find_fractal_pivots(bars, left=3, right=3)
        if len(highs) < 2 or len(lows) < 2:
            # Fallback simple momentum
            first_c = bars[-15].close if len(bars) >= 15 else bars[0].close
            pct = ((current_price - first_c) / first_c) * 100.0 if first_c > 0 else 0.0
            if pct > 0.6:
                return "BULLISH"
            if pct < -0.6:
                return "BEARISH"
            return "RANGE"

        sh1, sh2 = highs[-2][1], highs[-1][1]
        sl1, sl2 = lows[-2][1], lows[-1][1]
        last_closed = next((b for b in reversed(bars) if b.is_closed), bars[-1])

        if (sh2 > sh1 and sl2 > sl1) or (last_closed.close > sh2 and sl2 >= sl1 * 0.999):
            return "BULLISH"
        if (sh2 < sh1 and sl2 < sl1) or (last_closed.close < sl2 and sh2 <= sh1 * 1.001):
            return "BEARISH"
        return "RANGE"

    @staticmethod
    def compute_pdh_pdl(bars_1h: List[FootprintCandle]) -> Tuple[Optional[float], Optional[float]]:
        if not bars_1h:
            return None, None
        now_sec = time.time()
        today_midnight_ms = int((now_sec - (now_sec % 86400)) * 1000)
        yest_midnight_ms = today_midnight_ms - 86_400_000
        yest_bars = [b for b in bars_1h if yest_midnight_ms <= b.open_time < today_midnight_ms]
        if len(yest_bars) < 6:
            return None, None
        return max(b.high for b in yest_bars), min(b.low for b in yest_bars)

    @staticmethod
    def _is_unmitigated_high(bars: List[FootprintCandle], idx: int, level: float) -> bool:
        for j in range(idx + 1, len(bars)):
            # If a closed bar later closed decisively above it, it's broken (not a fresh resting BSL pool)
            if bars[j].is_closed and bars[j].close > level * 1.0005:
                return False
        return True

    @staticmethod
    def _is_unmitigated_low(bars: List[FootprintCandle], idx: int, level: float) -> bool:
        for j in range(idx + 1, len(bars)):
            if bars[j].is_closed and bars[j].close < level * 0.9995:
                return False
        return True

    @classmethod
    def build_liquidity_map(cls, state: SymbolOrderflowState, exec_tf: str = "5m") -> Dict[str, Any]:
        with state.lock:
            bars_4h = list(state.candles.get("4h", []))
            bars_1h = list(state.candles.get("1h", []))
            bars_15m = list(state.candles.get("15m", []))
            bars_5m = list(state.candles.get("5m", []))
            price = state.last_price or (bars_15m[-1].close if bars_15m else 0.0)
            bid_walls = list(state.bid_walls)
            ask_walls = list(state.ask_walls)

        if price <= 0:
            return {"bias_1h": "RANGE", "bias_4h": "RANGE", "pools": [], "bsl": [], "ssl": []}

        bias_1h = cls.determine_bias(bars_1h, price)
        bias_4h = cls.determine_bias(bars_4h, price)
        state.active_bias = bias_1h

        pools: List[LiquidityPool] = []

        # 1. PDH / PDL (Strength 5)
        pdh, pdl = cls.compute_pdh_pdl(bars_1h)
        if pdh and pdh > price * 0.998:
            pools.append(LiquidityPool("PDH", "BSL" if pdh >= price else "SSL", pdh, 5, "1D", "Prev Day High"))
        if pdl and pdl < price * 1.002:
            pools.append(LiquidityPool("PDL", "SSL" if pdl <= price else "BSL", pdl, 5, "1D", "Prev Day Low"))

        # 2. Unmitigated Swing Highs / Lows + EQH/EQL on 1h and 15m
        for tf, b_list, base_str in (("1h", bars_1h, 4), ("15m", bars_15m, 3)):
            sh_list, sl_list = cls.find_fractal_pivots(b_list, left=3, right=3)
            unmit_highs = [(i, p) for (i, p, _) in sh_list[-10:] if cls._is_unmitigated_high(b_list, i, p)]
            unmit_lows = [(i, p) for (i, p, _) in sl_list[-10:] if cls._is_unmitigated_low(b_list, i, p)]

            # Detect EQH / EQL (within 0.14%)
            eqh_prices = set()
            for a_idx in range(len(unmit_highs)):
                for b_idx in range(a_idx + 1, len(unmit_highs)):
                    p1, p2 = unmit_highs[a_idx][1], unmit_highs[b_idx][1]
                    if abs(p1 - p2) / p1 <= 0.0014:
                        eqh_prices.add(max(p1, p2))

            eql_prices = set()
            for a_idx in range(len(unmit_lows)):
                for b_idx in range(a_idx + 1, len(unmit_lows)):
                    p1, p2 = unmit_lows[a_idx][1], unmit_lows[b_idx][1]
                    if abs(p1 - p2) / p1 <= 0.0014:
                        eql_prices.add(min(p1, p2))

            for _, p in unmit_highs:
                if p >= price * 0.9985:
                    is_eq = p in eqh_prices
                    pools.append(
                        LiquidityPool(
                            name=f"{tf} {'EQH' if is_eq else 'Swing High'}",
                            side="BSL",
                            price=p,
                            strength=5 if is_eq else base_str,
                            tf=tf,
                            meta="Equal Highs Magnet" if is_eq else "Resting BSL",
                        )
                    )

            for _, p in unmit_lows:
                if p <= price * 1.0015:
                    is_eq = p in eql_prices
                    pools.append(
                        LiquidityPool(
                            name=f"{tf} {'EQL' if is_eq else 'Swing Low'}",
                            side="SSL",
                            price=p,
                            strength=5 if is_eq else base_str,
                            tf=tf,
                            meta="Equal Lows Magnet" if is_eq else "Resting SSL",
                        )
                    )

        # 3. 5m Minor Swings (only for Scalping TP/internal liquidity reference)
        if exec_tf == "5m" and len(bars_5m) >= 15:
            sh5, sl5 = cls.find_fractal_pivots(bars_5m[-50:], left=2, right=2)
            for idx5, p, _ in sh5[-3:]:
                if p > price * 1.0005:
                    pools.append(LiquidityPool("5m Swing High", "BSL", p, 2, "5m", "Internal BSL"))
            for idx5, p, _ in sl5[-3:]:
                if p < price * 0.9995:
                    pools.append(LiquidityPool("5m Swing Low", "SSL", p, 2, "5m", "Internal SSL"))

        # 4. Volume Profile VAH / VAL / VWAP (15m & 1h)
        vp15 = state.get_visible_volume_profile("15m", lookback=96)
        if vp15.get("vah", 0) > price * 0.999:
            pools.append(LiquidityPool("15m VAH", "BSL", vp15["vah"], 3, "15m", "Value Area High"))
        if vp15.get("val", 0) > 0 and vp15["val"] < price * 1.001:
            pools.append(LiquidityPool("15m VAL", "SSL", vp15["val"], 3, "15m", "Value Area Low"))

        vwap = state.get_session_vwap("15m")
        if vwap > 0:
            side = "BSL" if vwap > price else "SSL"
            pools.append(LiquidityPool("Daily VWAP", side, vwap, 2, "15m", "Session VWAP"))

        # 5. Naked POCs (5m & 15m)
        for tf_np in ("15m", "5m"):
            for npoc in state.get_naked_pocs(tf_np, max_count=5):
                p = npoc["price"]
                side = "BSL" if p > price else "SSL"
                pools.append(LiquidityPool(f"{tf_np} nPOC", side, p, 3 if tf_np == "15m" else 2, tf_np, "Naked POC Magnet"))

        # 6. Live Orderbook Walls (Top 2 Bid & Ask Walls)
        for w in sorted(ask_walls, key=lambda x: x["usd"], reverse=True)[:2]:
            pools.append(
                LiquidityPool(
                    name="Ask Wall",
                    side="BSL",
                    price=w["price"],
                    strength=4,
                    tf="OB",
                    meta=f"${w['usd'] / 1e6:.2f}M" if w["usd"] >= 1e6 else f"${w['usd'] / 1e3:.0f}K",
                )
            )
        for w in sorted(bid_walls, key=lambda x: x["usd"], reverse=True)[:2]:
            pools.append(
                LiquidityPool(
                    name="Bid Wall",
                    side="SSL",
                    price=w["price"],
                    strength=4,
                    tf="OB",
                    meta=f"${w['usd'] / 1e6:.2f}M" if w["usd"] >= 1e6 else f"${w['usd'] / 1e3:.0f}K",
                )
            )

        # Deduplicate very close pools (< 0.06% apart) keeping highest strength
        bsl = cls._dedupe_pools([p for p in pools if p.side == "BSL" and p.price >= price * 0.9985], ascending=True)
        ssl = cls._dedupe_pools([p for p in pools if p.side == "SSL" and p.price <= price * 1.0015], ascending=False)

        return {
            "bias_1h": bias_1h,
            "bias_4h": bias_4h,
            "pdh": pdh,
            "pdl": pdl,
            "vwap": vwap,
            "vp15": {"poc": vp15.get("poc", 0), "vah": vp15.get("vah", 0), "val": vp15.get("val", 0)},
            "bsl": [p.to_dict() for p in bsl[:10]],
            "ssl": [p.to_dict() for p in ssl[:10]],
            "bsl_objs": bsl[:10],
            "ssl_objs": ssl[:10],
        }

    @staticmethod
    def _dedupe_pools(items: List[LiquidityPool], ascending: bool) -> List[LiquidityPool]:
        if not items:
            return []
        items_sorted = sorted(items, key=lambda x: x.price, reverse=not ascending)
        kept: List[LiquidityPool] = []
        for item in items_sorted:
            merged = False
            for idx, k in enumerate(kept):
                if k.price > 0 and abs(item.price - k.price) / k.price <= 0.0006:
                    if item.strength > k.strength:
                        kept[idx] = item
                    merged = True
                    break
            if not merged:
                kept.append(item)
        return kept