import math
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

from config import compute_row_size
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
            if is_high and any(bars[j].high < c.high for j in range(i - left, i + right + 1) if j != i):
                highs.append((i, c.high, c.open_time))
            if is_low and any(bars[j].low > c.low for j in range(i - left, i + right + 1) if j != i):
                lows.append((i, c.low, c.open_time))
        return highs, lows

    @staticmethod
    def determine_bias(bars: List[FootprintCandle], current_price: float, pivot_lr: int = 3) -> str:
        if len(bars) < max(12, pivot_lr * 3):
            return "RANGE"
        highs, lows = LiquidityEngine.find_fractal_pivots(bars, left=pivot_lr, right=pivot_lr)
        if len(highs) < 2 or len(lows) < 2:
            look = min(15, len(bars))
            first_c = bars[-look].close
            pct = ((current_price - first_c) / first_c) * 100.0 if first_c > 0 else 0.0
            if pct > 1.0:
                return "BULLISH"
            if pct < -1.0:
                return "BEARISH"
            return "RANGE"

        sh1, sh2 = highs[-2][1], highs[-1][1]
        sl1, sl2 = lows[-2][1], lows[-1][1]
        last_closed = next((b for b in reversed(bars) if b.is_closed), bars[-1])

        if (sh2 > sh1 and sl2 > sl1) or (last_closed.close > sh2 and sl2 >= sl1 * 0.998):
            return "BULLISH"
        if (sh2 < sh1 and sl2 < sl1) or (last_closed.close < sl2 and sh2 <= sh1 * 1.002):
            return "BEARISH"
        return "RANGE"

    @staticmethod
    def compute_pdh_pdl_pwh_pwl(
        bars_1d: List[FootprintCandle], bars_1h: List[FootprintCandle]
    ) -> Tuple[Optional[float], Optional[float], Optional[float], Optional[float], Optional[float]]:
        pdh = pdl = pwh = pwl = daily_open = None
        now_sec = time.time()
        today_midnight_ms = int((now_sec - (now_sec % 86400)) * 1000)

        if bars_1d:
            closed_d = [b for b in bars_1d if b.is_closed or b.open_time < today_midnight_ms]
            if closed_d:
                prev_day = closed_d[-1]
                pdh, pdl = prev_day.high, prev_day.low
            today_bars = [b for b in bars_1d if b.open_time >= today_midnight_ms]
            if today_bars:
                daily_open = today_bars[-1].open
            prev_week_bars = closed_d[-8:-1] if len(closed_d) >= 8 else closed_d
            if len(prev_week_bars) >= 3:
                pwh = max(b.high for b in prev_week_bars)
                pwl = min(b.low for b in prev_week_bars)

        if (pdh is None or pdl is None) and bars_1h:
            yest_midnight_ms = today_midnight_ms - 86_400_000
            yest_1h = [b for b in bars_1h if yest_midnight_ms <= b.open_time < today_midnight_ms]
            if len(yest_1h) >= 6:
                pdh = max(b.high for b in yest_1h)
                pdl = min(b.low for b in yest_1h)
        return pdh, pdl, pwh, pwl, daily_open

    @staticmethod
    def _is_unmitigated_high(bars: List[FootprintCandle], idx: int, level: float) -> bool:
        limit = max(idx + 1, len(bars) - 2)
        for j in range(idx + 1, limit):
            if bars[j].is_closed and bars[j].close > level * 1.0008:
                return False
        return True

    @staticmethod
    def _is_unmitigated_low(bars: List[FootprintCandle], idx: int, level: float) -> bool:
        limit = max(idx + 1, len(bars) - 2)
        for j in range(idx + 1, limit):
            if bars[j].is_closed and bars[j].close < level * 0.9992:
                return False
        return True

    @staticmethod
    def find_htf_fvgs(bars: List[FootprintCandle], tf: str, min_gap_pct: float = 0.0020) -> List[Dict[str, Any]]:
        closed = [b for b in bars if b.is_closed][-55:]
        if len(closed) < 4:
            return []
        fvgs: List[Dict[str, Any]] = []
        for i in range(2, len(closed)):
            b0, b1, b2 = closed[i - 2], closed[i - 1], closed[i]
            if b2.low > b0.high and b1.close > b1.open:
                gap_pct = (b2.low - b0.high) / max(b1.close, 1e-9)
                if gap_pct >= min_gap_pct:
                    mid = (b2.low + b0.high) * 0.5
                    mitigated = any(closed[k].low <= mid for k in range(i + 1, len(closed)))
                    if not mitigated:
                        fvgs.append({
                            "tf": tf, "side": "BULL", "low": round(b0.high, 8), "high": round(b2.low, 8),
                            "mid": round(mid, 8), "time": b1.open_time,
                        })
            elif b2.high < b0.low and b1.close < b1.open:
                gap_pct = (b0.low - b2.high) / max(b1.close, 1e-9)
                if gap_pct >= min_gap_pct:
                    mid = (b0.low + b2.high) * 0.5
                    mitigated = any(closed[k].high >= mid for k in range(i + 1, len(closed)))
                    if not mitigated:
                        fvgs.append({
                            "tf": tf, "side": "BEAR", "low": round(b2.high, 8), "high": round(b0.low, 8),
                            "mid": round(mid, 8), "time": b1.open_time,
                        })
        return fvgs[-6:]

    @staticmethod
    def find_htf_orderblocks(bars: List[FootprintCandle], tf: str) -> List[Dict[str, Any]]:
        closed = [b for b in bars if b.is_closed][-60:]
        if len(closed) < 6:
            return []
        obs: List[Dict[str, Any]] = []
        for i in range(1, len(closed) - 2):
            ob_bar = closed[i]
            disp1 = closed[i + 1]
            disp2 = closed[i + 2]
            rng1 = (disp1.close - disp1.open) / max(disp1.open, 1e-9)
            rng2 = (disp2.close - disp1.open) / max(disp1.open, 1e-9)

            if ob_bar.close < ob_bar.open and (rng1 >= 0.0055 or rng2 >= 0.0080) and disp1.close > ob_bar.high:
                mitigated = any(closed[k].low <= ob_bar.low for k in range(i + 2, len(closed)))
                if not mitigated:
                    obs.append({
                        "tf": tf, "side": "BULL", "low": round(ob_bar.low, 8), "high": round(ob_bar.high, 8),
                        "time": ob_bar.open_time,
                    })
            elif ob_bar.close > ob_bar.open and (rng1 <= -0.0055 or rng2 <= -0.0080) and disp1.close < ob_bar.low:
                mitigated = any(closed[k].high >= ob_bar.high for k in range(i + 2, len(closed)))
                if not mitigated:
                    obs.append({
                        "tf": tf, "side": "BEAR", "low": round(ob_bar.low, 8), "high": round(ob_bar.high, 8),
                        "time": ob_bar.open_time,
                    })
        return obs[-6:]

    @staticmethod
    def build_synthetic_liq_heatmap(
        symbol: str,
        price: float,
        bsl_pools: List[LiquidityPool],
        ssl_pools: List[LiquidityPool],
        ask_walls: List[Dict[str, float]],
        bid_walls: List[Dict[str, float]],
        combined_oi: float,
    ) -> List[Dict[str, Any]]:
        if price <= 0:
            return []
        row_step = compute_row_size(symbol, price, "15m") * 2.0
        heat_bins: Dict[Tuple[str, float], float] = {}

        def add_heat(side: str, target_px: float, weight: float) -> None:
            if target_px <= 0:
                return
            bp = round(math.floor((target_px + 1e-12) / row_step) * row_step, 8)
            k = (side, bp)
            heat_bins[k] = heat_bins.get(k, 0.0) + weight

        for p in bsl_pools:
            if p.strength < 3 or p.price < price * 0.995:
                continue
            base_w = float(p.strength ** 2)
            add_heat("SHORT_LIQ", p.price * 1.0035, base_w * 0.9)
            add_heat("SHORT_LIQ", p.price * 1.0095, base_w * 1.2)
            add_heat("SHORT_LIQ", p.price * 1.0190, base_w * 0.8)

        for p in ssl_pools:
            if p.strength < 3 or p.price > price * 1.005:
                continue
            base_w = float(p.strength ** 2)
            add_heat("LONG_LIQ", p.price * 0.9965, base_w * 0.9)
            add_heat("LONG_LIQ", p.price * 0.9905, base_w * 1.2)
            add_heat("LONG_LIQ", p.price * 0.9810, base_w * 0.8)

        for w in ask_walls:
            add_heat("SHORT_LIQ", w["price"], 14.0)
        for w in bid_walls:
            add_heat("LONG_LIQ", w["price"], 14.0)

        if not heat_bins:
            return []
        max_w = max(heat_bins.values()) or 1.0
        oi_usd = max(combined_oi * price, 10_000_000.0)

        bands: List[Dict[str, Any]] = []
        for (side, bp), w in heat_bins.items():
            norm = round(min(1.0, w / max_w), 3)
            if norm < 0.25:
                continue
            est_usd = round(oi_usd * 0.004 * norm, 0)
            bands.append({
                "side": side,
                "price": bp,
                "high": round(bp + row_step, 8),
                "intensity": norm,
                "est_usd": est_usd,
            })

        bands.sort(key=lambda x: x["intensity"], reverse=True)
        return bands[:24]

    @classmethod
    def build_liquidity_map(cls, state: SymbolOrderflowState, exec_tf: str = "15m") -> Dict[str, Any]:
        with state.lock:
            bars_1d = list(state.candles.get("1d", []))
            bars_4h = list(state.candles.get("4h", []))
            bars_1h = list(state.candles.get("1h", []))
            bars_15m = list(state.candles.get("15m", []))
            price = state.last_price or (bars_15m[-1].close if bars_15m else 0.0)
            bid_walls = list(state.bid_walls)
            ask_walls = list(state.ask_walls)
            comb_oi = state.combined_oi

        if price <= 0:
            return {
                "bias_1d": "RANGE", "bias_4h": "RANGE", "bias_1h": "RANGE",
                "aligned_bias": "RANGE", "htf_bias_str": "1D:RANGE | 4H:RANGE | 1H:RANGE",
                "pdh": None, "pdl": None, "pwh": None, "pwl": None, "daily_open": None, "vwap": 0.0,
                "bsl": [], "ssl": [], "bsl_objs": [], "ssl_objs": [],
                "all_bsl_objs": [], "all_ssl_objs": [],
                "fvgs": [], "obs": [], "liq_heatmap": [],
            }

        bias_1d = cls.determine_bias(bars_1d, price, pivot_lr=2)
        bias_4h = cls.determine_bias(bars_4h, price, pivot_lr=3)
        bias_1h = cls.determine_bias(bars_1h, price, pivot_lr=3)
        state.active_bias = bias_4h if bias_4h != "RANGE" else bias_1h

        if bias_1d == bias_4h and bias_1d != "RANGE":
            aligned_bias = bias_1d
        elif bias_4h != "RANGE" and (bias_1d == "RANGE" or bias_1h == bias_4h):
            aligned_bias = bias_4h
        elif bias_1d != "RANGE" and bias_4h == "RANGE":
            aligned_bias = bias_1d
        else:
            aligned_bias = "RANGE"

        htf_bias_str = f"1D:{bias_1d[:4]} | 4H:{bias_4h[:4]} | 1H:{bias_1h[:4]}"

        all_bsl: List[LiquidityPool] = []
        all_ssl: List[LiquidityPool] = []

        pdh, pdl, pwh, pwl, daily_open = cls.compute_pdh_pdl_pwh_pwl(bars_1d, bars_1h)
        if pwh:
            all_bsl.append(LiquidityPool("PWH", "BSL", pwh, 5, "1W", "Prev 7D High"))
        if pwl:
            all_ssl.append(LiquidityPool("PWL", "SSL", pwl, 5, "1W", "Prev 7D Low"))
        if pdh:
            all_bsl.append(LiquidityPool("PDH", "BSL", pdh, 5, "1D", "Prev Day High"))
        if pdl:
            all_ssl.append(LiquidityPool("PDL", "SSL", pdl, 5, "1D", "Prev Day Low"))
        if daily_open:
            side_do = "BSL" if daily_open >= price else "SSL"
            (all_bsl if side_do == "BSL" else all_ssl).append(
                LiquidityPool("UTC Open", side_do, daily_open, 3, "1D", "00:00 UTC Open")
            )

        tf_configs = (
            ("1d", bars_1d, 2, 5, 6),
            ("4h", bars_4h, 3, 5, 10),
            ("1h", bars_1h, 3, 4, 14),
            ("15m", bars_15m, 3, 3, 12),
        )
        for tf_name, b_list, lr, base_str, slice_n in tf_configs:
            sh_list, sl_list = cls.find_fractal_pivots(b_list, left=lr, right=lr)
            unmit_h = [(i, p) for (i, p, _) in sh_list[-slice_n:] if cls._is_unmitigated_high(b_list, i, p)]
            unmit_l = [(i, p) for (i, p, _) in sl_list[-slice_n:] if cls._is_unmitigated_low(b_list, i, p)]

            eqh_prices = set()
            for a_i in range(len(unmit_h)):
                for b_i in range(a_i + 1, len(unmit_h)):
                    p1, p2 = unmit_h[a_i][1], unmit_h[b_i][1]
                    if abs(p1 - p2) / p1 <= 0.0015:
                        eqh_prices.add(max(p1, p2))

            eql_prices = set()
            for a_i in range(len(unmit_l)):
                for b_i in range(a_i + 1, len(unmit_l)):
                    p1, p2 = unmit_l[a_i][1], unmit_l[b_i][1]
                    if abs(p1 - p2) / p1 <= 0.0015:
                        eql_prices.add(min(p1, p2))

            for _, p in unmit_h:
                is_eq = p in eqh_prices
                all_bsl.append(LiquidityPool(
                    name=f"{tf_name} {'EQH' if is_eq else 'Swing H'}",
                    side="BSL", price=p, strength=5 if is_eq else base_str, tf=tf_name,
                    meta="Equal Highs Magnet" if is_eq else "Resting BSL",
                ))
            for _, p in unmit_l:
                is_eq = p in eql_prices
                all_ssl.append(LiquidityPool(
                    name=f"{tf_name} {'EQL' if is_eq else 'Swing L'}",
                    side="SSL", price=p, strength=5 if is_eq else base_str, tf=tf_name,
                    meta="Equal Lows Magnet" if is_eq else "Resting SSL",
                ))

        fvgs = cls.find_htf_fvgs(bars_4h, "4h", min_gap_pct=0.0030) + cls.find_htf_fvgs(bars_1h, "1h", min_gap_pct=0.0018)
        obs = cls.find_htf_orderblocks(bars_4h, "4h") + cls.find_htf_orderblocks(bars_1h, "1h")

        for fvg in fvgs:
            if fvg["side"] == "BEAR" and fvg["low"] >= price * 0.995:
                all_bsl.append(LiquidityPool(f"{fvg['tf']} Bear FVG", "BSL", fvg["mid"], 4 if fvg["tf"] == "4h" else 3, fvg["tf"], "HTF Supply IRL"))
            elif fvg["side"] == "BULL" and fvg["high"] <= price * 1.005:
                all_ssl.append(LiquidityPool(f"{fvg['tf']} Bull FVG", "SSL", fvg["mid"], 4 if fvg["tf"] == "4h" else 3, fvg["tf"], "HTF Demand IRL"))

        for ob in obs:
            if ob["side"] == "BEAR" and ob["low"] >= price * 0.995:
                all_bsl.append(LiquidityPool(f"{ob['tf']} Bear OB", "BSL", ob["low"], 4 if ob["tf"] == "4h" else 3, ob["tf"], "HTF Orderblock"))
            elif ob["side"] == "BULL" and ob["high"] <= price * 1.005:
                all_ssl.append(LiquidityPool(f"{ob['tf']} Bull OB", "SSL", ob["high"], 4 if ob["tf"] == "4h" else 3, ob["tf"], "HTF Orderblock"))

        vp15 = state.get_visible_volume_profile("15m", lookback=250)
        if vp15.get("vah", 0) > 0:
            all_bsl.append(LiquidityPool("15m VAH", "BSL", vp15["vah"], 3, "15m", "250-bar VAH"))
        if vp15.get("val", 0) > 0:
            all_ssl.append(LiquidityPool("15m VAL", "SSL", vp15["val"], 3, "15m", "250-bar VAL"))

        vwap = state.get_session_vwap("15m")
        if vwap > 0:
            (all_bsl if vwap >= price else all_ssl).append(
                LiquidityPool("Daily VWAP", "BSL" if vwap >= price else "SSL", vwap, 3, "15m", "00:00 UTC VWAP")
            )

        for tf_np in ("1h", "15m"):
            for npoc in state.get_naked_pocs(tf_np, max_count=5):
                p = npoc["price"]
                st_val = 4 if tf_np == "1h" else 3
                (all_bsl if p >= price else all_ssl).append(
                    LiquidityPool(f"{tf_np} nPOC", "BSL" if p >= price else "SSL", p, st_val, tf_np, "Naked POC Magnet")
                )

        for w in sorted(ask_walls, key=lambda x: x["usd"], reverse=True)[:4]:
            all_bsl.append(LiquidityPool(
                name="Ask Wall", side="BSL", price=w["price"], strength=4, tf="OB",
                meta=f"${w['usd'] / 1e6:.2f}M" if w["usd"] >= 1e6 else f"${w['usd'] / 1e3:.0f}K",
            ))
        for w in sorted(bid_walls, key=lambda x: x["usd"], reverse=True)[:4]:
            all_ssl.append(LiquidityPool(
                name="Bid Wall", side="SSL", price=w["price"], strength=4, tf="OB",
                meta=f"${w['usd'] / 1e6:.2f}M" if w["usd"] >= 1e6 else f"${w['usd'] / 1e3:.0f}K",
            ))

        dedup_all_bsl = cls._dedupe_pools(all_bsl, ascending=True)
        dedup_all_ssl = cls._dedupe_pools(all_ssl, ascending=False)

        liq_heatmap = cls.build_synthetic_liq_heatmap(
            state.symbol, price, dedup_all_bsl, dedup_all_ssl, ask_walls, bid_walls, comb_oi
        )

        for band in liq_heatmap:
            if band["intensity"] >= 0.75:
                if band["side"] == "SHORT_LIQ" and band["price"] > price * 1.001:
                    dedup_all_bsl.append(LiquidityPool(
                        "Short Liq Cluster", "BSL", band["price"], 4, "HEAT",
                        f"~${band['est_usd'] / 1e6:.1f}M" if band["est_usd"] >= 1e6 else f"~${band['est_usd'] / 1e3:.0f}K",
                    ))
                elif band["side"] == "LONG_LIQ" and band["price"] < price * 0.999:
                    dedup_all_ssl.append(LiquidityPool(
                        "Long Liq Cluster", "SSL", band["price"], 4, "HEAT",
                        f"~${band['est_usd'] / 1e6:.1f}M" if band["est_usd"] >= 1e6 else f"~${band['est_usd'] / 1e3:.0f}K",
                    ))

        dedup_all_bsl = cls._dedupe_pools(dedup_all_bsl, ascending=True)
        dedup_all_ssl = cls._dedupe_pools(dedup_all_ssl, ascending=False)

        ui_bsl = [p for p in dedup_all_bsl if p.price >= price * 0.9985][:12]
        ui_ssl = [p for p in dedup_all_ssl if p.price <= price * 1.0015][:12]

        return {
            "bias_1d": bias_1d,
            "bias_4h": bias_4h,
            "bias_1h": bias_1h,
            "aligned_bias": aligned_bias,
            "htf_bias_str": htf_bias_str,
            "pdh": pdh,
            "pdl": pdl,
            "pwh": pwh,
            "pwl": pwl,
            "daily_open": daily_open,
            "vwap": vwap,
            "vp15": {"poc": vp15.get("poc", 0), "vah": vp15.get("vah", 0), "val": vp15.get("val", 0)},
            "bsl": [p.to_dict() for p in ui_bsl],
            "ssl": [p.to_dict() for p in ui_ssl],
            "bsl_objs": ui_bsl,
            "ssl_objs": ui_ssl,
            "all_bsl_objs": dedup_all_bsl,
            "all_ssl_objs": dedup_all_ssl,
            "fvgs": fvgs,
            "obs": obs,
            "liq_heatmap": liq_heatmap,
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
                if k.price > 0 and abs(item.price - k.price) / k.price <= 0.0008:
                    if item.strength > k.strength:
                        kept[idx] = item
                    merged = True
                    break
            if not merged:
                kept.append(item)
        return kept