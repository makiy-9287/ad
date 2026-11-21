import logging
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

from database import Database
from footprint_engine import FootprintCandle, SymbolOrderflowState
from liquidity_engine import LiquidityEngine, LiquidityPool
from telegram_bot import TelegramNotifier

logger = logging.getLogger("strategy_engine")


@dataclass
class LevelWatch:
    pool: LiquidityPool
    entered_at: int
    oi_at_entry: float


@dataclass
class SweepWatch:
    symbol: str
    sweep_tf: str             # "15m" | "5m"
    direction: str            # "LONG" | "SHORT"
    pool: LiquidityPool
    sweep_time: int
    sweep_extreme: float
    sweep_poc: float
    sweep_open: float
    sweep_close: float
    structure_break_15m: float
    structure_break_5m: float
    trapped_zone: Optional[Tuple[float, float]]
    has_absorption: bool
    has_trapped: bool
    has_div: bool
    has_exhaustion: bool
    has_liq_burst: bool
    has_oi_trap: bool
    aligned_bias: str
    htf_bias_str: str
    bars_waited_15m: int = 0
    bars_waited_5m: int = 0
    sweep_score: int = 0


class StrategyEngine:
    """
    V3.1 Ultra Day-Mode-Only Institutional Strategy Engine:
    - Bias & Structure: 1D + 4H + 1H
    - Execution: 15m Sweep + 5m Sniper Orderflow Confirmation (and 15m MSS fallback)
    - Wide Day-Mode SL/TP enforced (>= min_sl_pct AND >= 1.25 * 15m ATR, RR >= 2.0)
    - Entry Mode: CMP (if already in/at Entry Zone) or LIMIT (if displaced away from Orderflow Zone)
    """

    def __init__(self, db: Database, notifier: TelegramNotifier):
        self.db = db
        self.notifier = notifier
        self.level_watches: Dict[str, LevelWatch] = {}
        self.sweep_watches: Dict[str, SweepWatch] = {}
        self.last_signal_ts: Dict[str, int] = {}

    def get_symbol_status(self, symbol: str, state: SymbolOrderflowState) -> Tuple[str, str]:
        sym = symbol.upper()
        active = self.db.get_active_signals(sym)
        if active:
            s = active[0]
            return "ACTIVE_SIGNAL", f"{s['direction']} {s['entry_type']} ({s['status']})"

        sw = self.sweep_watches.get(sym)
        if sw is not None:
            return (
                "SWEEP_DETECTED",
                f"{sw.direction} Sweep @ {sw.pool.name} ({sw.bars_waited_5m}x5m / {sw.bars_waited_15m}x15m — awaiting MSS)",
            )

        settings = self.db.get_all_settings()
        prox_pct = float(settings.get("level_proximity_pct", 0.65))
        lmap = state._cached_lmap if state._cached_lmap else LiquidityEngine.build_liquidity_map(state, "15m")
        px = state.last_price
        if px > 0 and lmap:
            near_pools = [
                p for p in (lmap.get("bsl_objs", []) + lmap.get("ssl_objs", []))
                if p.strength >= 3 and abs(p.price - px) / px * 100.0 <= prox_pct
            ]
            if near_pools:
                best = max(near_pools, key=lambda p: p.strength)
                if sym not in self.level_watches:
                    self.level_watches[sym] = LevelWatch(
                        pool=best,
                        entered_at=int(time.time() * 1000),
                        oi_at_entry=state.combined_oi,
                    )
                return "AT_LEVEL", f"At {best.name} ({best.price:,.5g})"
            else:
                self.level_watches.pop(sym, None)

        bias_str = lmap.get("htf_bias_str", "1D:RANG | 4H:RANG | 1H:RANG") if lmap else "DAY SCAN"
        return "WATCHING", bias_str

    async def on_live_tick(self, symbol: str, state: SymbolOrderflowState) -> bool:
        prev_st = state.scanner_state
        st, note = self.get_symbol_status(symbol, state)
        state.scanner_state = st
        state.scanner_note = note
        if st != prev_st and st in ("AT_LEVEL", "SWEEP_DETECTED"):
            logger.info("[%s] State: %s -> %s (%s)", symbol, prev_st, st, note)
            return True
        return False

    async def on_candle_closed(
        self,
        symbol: str,
        closed_tf: str,
        closed_candle: FootprintCandle,
        state: SymbolOrderflowState,
        btc_state: Optional[SymbolOrderflowState] = None,
    ) -> Optional[Dict[str, Any]]:
        if closed_tf not in ("5m", "15m"):
            return None

        sym = symbol.upper()
        settings = self.db.get_all_settings()

        if self.db.get_active_signals(sym):
            self.sweep_watches.pop(sym, None)
            st, note = self.get_symbol_status(sym, state)
            state.scanner_state, state.scanner_note = st, note
            return None

        cooldown_ms = int(settings.get("signal_cooldown_min", 90)) * 60_000
        if time.time() * 1000 - self.last_signal_ts.get(sym, 0) < cooldown_ms:
            st, note = self.get_symbol_status(sym, state)
            state.scanner_state, state.scanner_note = st, note
            return None

        with state.lock:
            bars_15m = [b for b in state.candles.get("15m", []) if b.is_closed]
            bars_5m = [b for b in state.candles.get("5m", []) if b.is_closed]

        if len(bars_15m) < 20 or len(bars_5m) < 20:
            return None

        lmap = LiquidityEngine.build_liquidity_map(state, exec_tf="15m")
        state._cached_lmap = lmap
        state._cached_lmap_ts = time.monotonic()

        sig: Optional[Dict[str, Any]] = None
        sw = self.sweep_watches.get(sym)

        if sw is not None:
            eval_bars = bars_5m if closed_tf == "5m" else bars_15m
            bar = eval_bars[-1]

            if sw.direction == "SHORT" and bar.close > sw.sweep_extreme:
                logger.info("[%s] Sweep INVALIDATED (%s closed above %.4f — Liquidity Run)", sym, closed_tf, sw.sweep_extreme)
                self.sweep_watches.pop(sym, None)
            elif sw.direction == "LONG" and bar.close < sw.sweep_extreme:
                logger.info("[%s] Sweep INVALIDATED (%s closed below %.4f — Liquidity Run)", sym, closed_tf, sw.sweep_extreme)
                self.sweep_watches.pop(sym, None)
            else:
                if sw.direction == "SHORT" and bar.high > sw.sweep_extreme and bar.close < sw.pool.price:
                    sw.sweep_extreme = bar.high
                elif sw.direction == "LONG" and bar.low < sw.sweep_extreme and bar.close > sw.pool.price:
                    sw.sweep_extreme = bar.low

                if closed_tf == "5m":
                    sw.bars_waited_5m += 1
                else:
                    sw.bars_waited_15m += 1

                max_5m = int(settings.get("sweep_expiry_bars_5m", 8))
                max_15m = int(settings.get("sweep_expiry_bars_15m", 6))
                if sw.bars_waited_5m > max_5m or sw.bars_waited_15m > max_15m:
                    logger.info("[%s] Sweep watch expired (no Day MSS confirmed in time)", sym)
                    self.sweep_watches.pop(sym, None)
                else:
                    sig = await self._check_confirmation(
                        sw, eval_bars, bars_15m, state, lmap, settings,
                        exec_tf=closed_tf, same_bar=False, btc_state=btc_state,
                    )
                    if sig is not None:
                        self.sweep_watches.pop(sym, None)
                        self.level_watches.pop(sym, None)
        else:
            if closed_tf == "15m":
                new_sw = self._detect_sweep(sym, "15m", bars_15m, bars_5m, state, lmap, min_strength=3)
                eval_bars = bars_15m
            else:
                if not state.tier2_active:
                    new_sw = None
                else:
                    new_sw = self._detect_sweep(sym, "5m", bars_5m, bars_5m, state, lmap, min_strength=4)
                eval_bars = bars_5m

            if new_sw is not None:
                self.sweep_watches[sym] = new_sw
                logger.info(
                    "[%s] %s SWEEP DETECTED: %s @ %s (%.4f), score=%d",
                    sym, closed_tf, new_sw.direction, new_sw.pool.name, new_sw.pool.price, new_sw.sweep_score,
                )
                sig = await self._check_confirmation(
                    new_sw, eval_bars, bars_15m, state, lmap, settings,
                    exec_tf=closed_tf, same_bar=True, btc_state=btc_state,
                )
                if sig is not None:
                    self.sweep_watches.pop(sym, None)
                    self.level_watches.pop(sym, None)

        st, note = self.get_symbol_status(sym, state)
        state.scanner_state, state.scanner_note = st, note
        return sig

    def _has_oi_trap(self, state: SymbolOrderflowState, sym: str) -> bool:
        lw = self.level_watches.get(sym)
        if lw and lw.oi_at_entry > 0 and state.combined_oi > 0:
            if (state.combined_oi - lw.oi_at_entry) / lw.oi_at_entry * 100.0 >= 0.12:
                return True
        if len(state.oi_history) >= 6:
            old_oi = state.oi_history[-6][1]
            cur_oi = state.oi_history[-1][1]
            if old_oi > 0 and (cur_oi - old_oi) / old_oi * 100.0 >= 0.15:
                return True
        return False

    def _detect_sweep(
        self,
        symbol: str,
        tf: str,
        bars: List[FootprintCandle],
        bars_5m: List[FootprintCandle],
        state: SymbolOrderflowState,
        lmap: Dict[str, Any],
        min_strength: int = 3,
    ) -> Optional[SweepWatch]:
        c = bars[-1]
        prev = bars[-2]
        c_range = c.high - c.low
        if c_range <= 0:
            return None

        aligned_bias = lmap.get("aligned_bias", "RANGE")
        htf_bias_str = lmap.get("htf_bias_str", "1D:RANG | 4H:RANG | 1H:RANG")
        dyn_liq_thresh = max(8_000.0, c.quote_volume * 0.004)
        oi_trap = self._has_oi_trap(state, symbol)
        is_live_fp = (c.fp_source == "LIVE")

        bsl_cands: List[Tuple[LiquidityPool, float]] = []
        for p in lmap.get("all_bsl_objs", []):
            if p.strength < min_strength:
                continue
            one_bar = (c.high > p.price) and (c.close < p.price) and (min(c.open, prev.open) < p.price)
            two_bar = (
                (prev.high > p.price)
                and (prev.close >= p.price)
                and ((prev.close - p.price) / p.price <= 0.0020)
                and (c.close < p.price)
                and (c.close < prev.open)
            )
            if one_bar or two_bar:
                bsl_cands.append((p, max(c.high, prev.high) if two_bar else c.high))

        if bsl_cands:
            pool, sweep_ext = max(bsl_cands, key=lambda x: x[0].strength)
            upper_wick = c.high - max(c.open, c.close)
            wick_ratio = upper_wick / c_range
            two_bar_engulf = (prev.high > pool.price and c.close < prev.open and c.close < pool.price)

            if wick_ratio >= 0.25 or two_bar_engulf:
                has_abs = is_live_fp and ((c.absorption == "BEAR") or (prev.absorption == "BEAR" and two_bar_engulf))
                has_trap = is_live_fp and ((c.trapped == "TRAPPED_LONGS") or (prev.trapped == "TRAPPED_LONGS" and two_bar_engulf))
                has_div = (c.divergence == "BEAR_DIV") or (c.swing_div == "SWING_BEAR_DIV") or (prev.swing_div == "SWING_BEAR_DIV")
                has_exh = is_live_fp and (c.exhaustion == "HIGH_EXHAUSTION")
                has_liq = (c.liq_buy_usd >= dyn_liq_thresh) or (prev.liq_buy_usd >= dyn_liq_thresh)
                has_ob_wall = (state.ob_imbalance_pct <= -4.0)

                hits = sum([has_abs, has_trap, has_div, has_exh, has_liq, c.delta < 0, has_ob_wall, oi_trap])
                allow_bias = (aligned_bias != "BULLISH") or (pool.strength >= 5 and is_live_fp and (has_abs or has_trap))

                if allow_bias and ((is_live_fp and (has_abs or has_trap)) or hits >= 2):
                    prior_win = bars[-6:-1]
                    struct_15m = min(b.low for b in prior_win[-3:]) if len(prior_win) >= 3 else c.low
                    if c.close < c.open:
                        struct_15m = min(struct_15m, c.low)
                    win_5m = [b for b in bars_5m if b.open_time <= c.close_time][-5:]
                    struct_5m = min(b.low for b in win_5m) if win_5m else c.low

                    score = 50 + (15 if aligned_bias == "BEARISH" else (6 if aligned_bias == "RANGE" else 0))
                    score += 10 if pool.strength >= 4 else 5
                    score += 10 if (has_abs or has_trap) else 0
                    score += 5 if (has_div or has_exh or has_liq or oi_trap or has_ob_wall) else 0

                    return SweepWatch(
                        symbol=symbol, sweep_tf=tf, direction="SHORT", pool=pool,
                        sweep_time=c.open_time, sweep_extreme=sweep_ext, sweep_poc=c.poc_price,
                        sweep_open=c.open, sweep_close=c.close,
                        structure_break_15m=struct_15m, structure_break_5m=struct_5m,
                        trapped_zone=c.trapped_zone or prev.trapped_zone,
                        has_absorption=has_abs, has_trapped=has_trap, has_div=has_div,
                        has_exhaustion=has_exh, has_liq_burst=has_liq, has_oi_trap=oi_trap,
                        aligned_bias=aligned_bias, htf_bias_str=htf_bias_str, sweep_score=score,
                    )

        ssl_cands: List[Tuple[LiquidityPool, float]] = []
        for p in lmap.get("all_ssl_objs", []):
            if p.strength < min_strength:
                continue
            one_bar = (c.low < p.price) and (c.close > p.price) and (max(c.open, prev.open) > p.price)
            two_bar = (
                (prev.low < p.price)
                and (prev.close <= p.price)
                and ((p.price - prev.close) / p.price <= 0.0020)
                and (c.close > p.price)
                and (c.close > prev.open)
            )
            if one_bar or two_bar:
                ssl_cands.append((p, min(c.low, prev.low) if two_bar else c.low))

        if ssl_cands:
            pool, sweep_ext = max(ssl_cands, key=lambda x: x[0].strength)
            lower_wick = min(c.open, c.close) - c.low
            wick_ratio = lower_wick / c_range
            two_bar_engulf = (prev.low < pool.price and c.close > prev.open and c.close > pool.price)

            if wick_ratio >= 0.25 or two_bar_engulf:
                has_abs = is_live_fp and ((c.absorption == "BULL") or (prev.absorption == "BULL" and two_bar_engulf))
                has_trap = is_live_fp and ((c.trapped == "TRAPPED_SHORTS") or (prev.trapped == "TRAPPED_SHORTS" and two_bar_engulf))
                has_div = (c.divergence == "BULL_DIV") or (c.swing_div == "SWING_BULL_DIV") or (prev.swing_div == "SWING_BULL_DIV")
                has_exh = is_live_fp and (c.exhaustion == "LOW_EXHAUSTION")
                has_liq = (c.liq_sell_usd >= dyn_liq_thresh) or (prev.liq_sell_usd >= dyn_liq_thresh)
                has_ob_wall = (state.ob_imbalance_pct >= 4.0)

                hits = sum([has_abs, has_trap, has_div, has_exh, has_liq, c.delta > 0, has_ob_wall, oi_trap])
                allow_bias = (aligned_bias != "BEARISH") or (pool.strength >= 5 and is_live_fp and (has_abs or has_trap))

                if allow_bias and ((is_live_fp and (has_abs or has_trap)) or hits >= 2):
                    prior_win = bars[-6:-1]
                    struct_15m = max(b.high for b in prior_win[-3:]) if len(prior_win) >= 3 else c.high
                    if c.close > c.open:
                        struct_15m = max(struct_15m, c.high)
                    win_5m = [b for b in bars_5m if b.open_time <= c.close_time][-5:]
                    struct_5m = max(b.high for b in win_5m) if win_5m else c.high

                    score = 50 + (15 if aligned_bias == "BULLISH" else (6 if aligned_bias == "RANGE" else 0))
                    score += 10 if pool.strength >= 4 else 5
                    score += 10 if (has_abs or has_trap) else 0
                    score += 5 if (has_div or has_exh or has_liq or oi_trap or has_ob_wall) else 0

                    return SweepWatch(
                        symbol=symbol, sweep_tf=tf, direction="LONG", pool=pool,
                        sweep_time=c.open_time, sweep_extreme=sweep_ext, sweep_poc=c.poc_price,
                        sweep_open=c.open, sweep_close=c.close,
                        structure_break_15m=struct_15m, structure_break_5m=struct_5m,
                        trapped_zone=c.trapped_zone or prev.trapped_zone,
                        has_absorption=has_abs, has_trapped=has_trap, has_div=has_div,
                        has_exhaustion=has_exh, has_liq_burst=has_liq, has_oi_trap=oi_trap,
                        aligned_bias=aligned_bias, htf_bias_str=htf_bias_str, sweep_score=score,
                    )
        return None

    @staticmethod
    def _btc_correlation_penalty(symbol: str, direction: str, btc_state: Optional[SymbolOrderflowState]) -> int:
        if symbol == "BTCUSDT" or btc_state is None:
            return 0
        with btc_state.lock:
            b15 = [b for b in btc_state.candles.get("15m", []) if b.is_closed]
        if len(b15) < 3:
            return 0
        move_pct = (b15[-1].close - b15[-3].open) / max(b15[-3].open, 1e-9) * 100.0
        delta_sum = b15[-1].delta + b15[-2].delta
        if direction == "LONG" and move_pct <= -0.65 and delta_sum < 0:
            return -15
        if direction == "SHORT" and move_pct >= 0.65 and delta_sum > 0:
            return -15
        return 0

    @staticmethod
    def _dual_exchange_delta_bonus(bar: FootprintCandle, direction: str) -> int:
        if bar.bybit_volume <= 0 or bar.fp_source != "LIVE":
            return 0
        if direction == "SHORT":
            if bar.binance_delta < 0 and bar.bybit_delta < 0:
                return 8
            if bar.binance_delta < 0 and bar.bybit_delta > abs(bar.binance_delta):
                return -6
        else:
            if bar.binance_delta > 0 and bar.bybit_delta > 0:
                return 8
            if bar.binance_delta > 0 and bar.bybit_delta < -abs(bar.binance_delta):
                return -6
        return 0

    async def _check_confirmation(
        self,
        watch: SweepWatch,
        eval_bars: List[FootprintCandle],
        bars_15m: List[FootprintCandle],
        state: SymbolOrderflowState,
        lmap: Dict[str, Any],
        settings: Dict[str, Any],
        exec_tf: str,
        same_bar: bool = False,
        btc_state: Optional[SymbolOrderflowState] = None,
    ) -> Optional[Dict[str, Any]]:
        bar = eval_bars[-1]
        prev = eval_bars[-2]
        prev2 = eval_bars[-3]

        min_rr = float(settings.get("min_rr", 2.0))
        min_sl_pct = float(settings.get("min_sl_pct", 0.55)) / 100.0
        max_sl_pct = float(settings.get("max_sl_pct", 4.50)) / 100.0
        sl_atr_mult = float(settings.get("sl_atr_mult", 1.25))
        sl_buf_pct = float(settings.get("sl_buffer_pct", 0.12)) / 100.0
        cmp_thresh_pct = float(settings.get("cmp_threshold_pct", 0.22)) / 100.0
        min_conf = int(settings.get("min_confidence", 72))

        atr_15m = state.get_atr("15m", 14)
        avg_vol = sum(b.volume for b in eval_bars[-13:-1]) / max(1, len(eval_bars[-13:-1]))
        bar_range = max(bar.high - bar.low, 1e-9)
        body_ratio = abs(bar.close - bar.open) / bar_range
        delta_pct = abs(bar.delta) / max(bar.volume + bar.bybit_volume, 1e-9)

        if watch.direction == "SHORT":
            if same_bar:
                mss_ok = (
                    (bar.close < bar.open)
                    and (bar.close < watch.sweep_open)
                    and (bar.close < watch.pool.price)
                    and ((bar.high - bar.close) / bar_range >= 0.58)
                )
            else:
                ref_struct = watch.structure_break_5m if exec_tf == "5m" else watch.structure_break_15m
                break_target = min(ref_struct, watch.sweep_poc) if watch.sweep_poc > 0 else ref_struct
                mss_ok = (
                    (bar.close < bar.open)
                    and (bar.close < break_target)
                    and (body_ratio >= 0.40)
                    and (bar.volume >= avg_vol * 0.70)
                )

            delta_ok = (bar.delta < 0) and (delta_pct >= 0.03)
            if not (mss_ok and delta_ok):
                return None

            bear_stacked = [z for z in bar.stacked_zones if z["direction"] == "BEAR"]
            if not bear_stacked and not same_bar:
                bear_stacked = [z for z in prev.stacked_zones if z["direction"] == "BEAR"]

            fvg_zone: Optional[Tuple[float, float]] = None
            if len(eval_bars) >= 3 and bar.high < prev2.low:
                fvg_zone = (bar.high, prev2.low)

            if bear_stacked:
                z = bear_stacked[0]
                e_lo, e_hi = float(z["low"]), float(z["high"])
                entry_source = "Stacked Sell Imbalance"
            elif fvg_zone is not None:
                e_lo, e_hi = fvg_zone
                entry_source = "Bearish FVG"
            elif watch.trapped_zone is not None:
                e_lo, e_hi = watch.trapped_zone
                entry_source = "Trapped Longs Zone"
            else:
                poc = bar.poc_price if bar.poc_price > bar.close else watch.sweep_poc
                e_lo = min(poc, (bar.open + bar.close) * 0.5)
                e_hi = max(poc, bar.open)
                entry_source = "Displacement OB / POC"

            e_lo = max(e_lo, bar.close)
            e_hi = min(max(e_hi, e_lo + bar.row_size), watch.sweep_extreme * 0.9995)
            if e_hi <= e_lo:
                e_lo = bar.close
                e_hi = min(bar.close + (watch.sweep_extreme - bar.close) * 0.45, watch.sweep_extreme * 0.9995)

            zone_mid = (e_lo + e_hi) * 0.5
            if (e_lo <= bar.close <= e_hi) or (abs(zone_mid - bar.close) / max(bar.close, 1e-9) <= cmp_thresh_pct):
                entry_type = "CMP"
                entry_mid = round(bar.close, 8)
            else:
                entry_type = "LIMIT"
                entry_mid = round(zone_mid, 8)

            struct_high_15m = max(b.high for b in bars_15m[-3:])
            raw_sl = max(watch.sweep_extreme, struct_high_15m) * (1.0 + sl_buf_pct)
            min_sl_required = entry_mid + max(entry_mid * min_sl_pct, atr_15m * sl_atr_mult)
            stop_loss = round(max(raw_sl, min_sl_required), 8)
            risk = stop_loss - entry_mid
            risk_pct = risk / max(entry_mid, 1e-9)

            if risk <= 0 or risk_pct < min_sl_pct or risk_pct > max_sl_pct:
                logger.info("[%s] SHORT rejected by Day-Mode SL width guard (risk=%.2f%%)", watch.symbol, risk_pct * 100)
                return None

            blocking_ssl = [
                p for p in lmap.get("all_ssl_objs", [])
                if p.strength >= 4 and (entry_mid - risk * 1.70) < p.price < bar.close
            ]
            if blocking_ssl:
                logger.info("[%s] SHORT rejected: Opposing %s blocks RR (<1.7R)", watch.symbol, blocking_ssl[0].name)
                return None

            ssl_targets: List[LiquidityPool] = [
                p for p in lmap.get("all_ssl_objs", [])
                if (entry_mid - risk * 5.0) <= p.price <= (entry_mid - risk * min_rr)
            ]
            tp1 = round(entry_mid - 1.5 * risk, 8)
            if ssl_targets:
                tp2_pool = max(ssl_targets, key=lambda p: (p.strength, 1 if p.tf in ("HEAT", "OB", "1h", "4h") else 0, p.price))
                tp2 = round(tp2_pool.price, 8)
                tp2_name = tp2_pool.name
            else:
                tp2 = round(entry_mid - 2.6 * risk, 8)
                tp2_name = "2.6R Day Extension"

            rr = round((entry_mid - tp2) / risk, 2)
            req_rr = max(min_rr, 2.3) if watch.aligned_bias == "BULLISH" else min_rr
            if rr < req_rr:
                return None

            dual_bonus = self._dual_exchange_delta_bonus(bar, "SHORT")
            btc_pen = self._btc_correlation_penalty(watch.symbol, "SHORT", btc_state)
            confidence = watch.sweep_score + (8 if bear_stacked else (5 if fvg_zone else 0)) + (5 if rr >= 2.5 else 0) + dual_bonus + btc_pen
            confidence = max(0, min(98, confidence))
            if confidence < min_conf:
                logger.info("[%s] SHORT rejected: Confidence %d < %d", watch.symbol, confidence, min_conf)
                return None

        else:  # LONG
            if same_bar:
                mss_ok = (
                    (bar.close > bar.open)
                    and (bar.close > watch.sweep_open)
                    and (bar.close > watch.pool.price)
                    and ((bar.close - bar.low) / bar_range >= 0.58)
                )
            else:
                ref_struct = watch.structure_break_5m if exec_tf == "5m" else watch.structure_break_15m
                break_target = max(ref_struct, watch.sweep_poc) if watch.sweep_poc > 0 else ref_struct
                mss_ok = (
                    (bar.close > bar.open)
                    and (bar.close > break_target)
                    and (body_ratio >= 0.40)
                    and (bar.volume >= avg_vol * 0.70)
                )

            delta_ok = (bar.delta > 0) and (delta_pct >= 0.03)
            if not (mss_ok and delta_ok):
                return None

            bull_stacked = [z for z in bar.stacked_zones if z["direction"] == "BULL"]
            if not bull_stacked and not same_bar:
                bull_stacked = [z for z in prev.stacked_zones if z["direction"] == "BULL"]

            fvg_zone = None
            if len(eval_bars) >= 3 and bar.low > prev2.high:
                fvg_zone = (prev2.high, bar.low)

            if bull_stacked:
                z = bull_stacked[0]
                e_lo, e_hi = float(z["low"]), float(z["high"])
                entry_source = "Stacked Buy Imbalance"
            elif fvg_zone is not None:
                e_lo, e_hi = fvg_zone
                entry_source = "Bullish FVG"
            elif watch.trapped_zone is not None:
                e_lo, e_hi = watch.trapped_zone
                entry_source = "Trapped Shorts Zone"
            else:
                poc = bar.poc_price if bar.poc_price < bar.close else watch.sweep_poc
                e_hi = max(poc, (bar.open + bar.close) * 0.5)
                e_lo = min(poc, bar.open)
                entry_source = "Displacement OB / POC"

            e_hi = min(e_hi, bar.close)
            e_lo = max(min(e_lo, e_hi - bar.row_size), watch.sweep_extreme * 1.0005)
            if e_lo >= e_hi:
                e_hi = bar.close
                e_lo = max(bar.close - (bar.close - watch.sweep_extreme) * 0.45, watch.sweep_extreme * 1.0005)

            zone_mid = (e_lo + e_hi) * 0.5
            if (e_lo <= bar.close <= e_hi) or (abs(bar.close - zone_mid) / max(bar.close, 1e-9) <= cmp_thresh_pct):
                entry_type = "CMP"
                entry_mid = round(bar.close, 8)
            else:
                entry_type = "LIMIT"
                entry_mid = round(zone_mid, 8)

            struct_low_15m = min(b.low for b in bars_15m[-3:])
            raw_sl = min(watch.sweep_extreme, struct_low_15m) * (1.0 - sl_buf_pct)
            min_sl_required = entry_mid - max(entry_mid * min_sl_pct, atr_15m * sl_atr_mult)
            stop_loss = round(min(raw_sl, min_sl_required), 8)
            risk = entry_mid - stop_loss
            risk_pct = risk / max(entry_mid, 1e-9)

            if risk <= 0 or risk_pct < min_sl_pct or risk_pct > max_sl_pct:
                logger.info("[%s] LONG rejected by Day-Mode SL width guard (risk=%.2f%%)", watch.symbol, risk_pct * 100)
                return None

            blocking_bsl = [
                p for p in lmap.get("all_bsl_objs", [])
                if p.strength >= 4 and bar.close < p.price < (entry_mid + risk * 1.70)
            ]
            if blocking_bsl:
                logger.info("[%s] LONG rejected: Opposing %s blocks RR (<1.7R)", watch.symbol, blocking_bsl[0].name)
                return None

            bsl_targets: List[LiquidityPool] = [
                p for p in lmap.get("all_bsl_objs", [])
                if (entry_mid + risk * min_rr) <= p.price <= (entry_mid + risk * 5.0)
            ]
            tp1 = round(entry_mid + 1.5 * risk, 8)
            if bsl_targets:
                tp2_pool = max(bsl_targets, key=lambda p: (p.strength, 1 if p.tf in ("HEAT", "OB", "1h", "4h") else 0, -p.price))
                tp2 = round(tp2_pool.price, 8)
                tp2_name = tp2_pool.name
            else:
                tp2 = round(entry_mid + 2.6 * risk, 8)
                tp2_name = "2.6R Day Extension"

            rr = round((tp2 - entry_mid) / risk, 2)
            req_rr = max(min_rr, 2.3) if watch.aligned_bias == "BEARISH" else min_rr
            if rr < req_rr:
                return None

            dual_bonus = self._dual_exchange_delta_bonus(bar, "LONG")
            btc_pen = self._btc_correlation_penalty(watch.symbol, "LONG", btc_state)
            confidence = watch.sweep_score + (8 if bull_stacked else (5 if fvg_zone else 0)) + (5 if rr >= 2.5 else 0) + dual_bonus + btc_pen
            confidence = max(0, min(98, confidence))
            if confidence < min_conf:
                logger.info("[%s] LONG rejected: Confidence %d < %d", watch.symbol, confidence, min_conf)
                return None

        tags = []
        if watch.has_absorption:
            tags.append("Absorption")
        if watch.has_trapped:
            tags.append("Trapped Longs" if watch.direction == "SHORT" else "Trapped Shorts")
        if watch.has_oi_trap:
            tags.append("OI Trap")
        if watch.has_exhaustion:
            tags.append("Exhaustion")
        if watch.has_div:
            tags.append("Delta Div")
        if watch.has_liq_burst:
            tags.append("Liq Flush")
        if dual_bonus > 0:
            tags.append("Binance+Bybit Aligned")
        tags.append(f"Delta Flip ({bar.delta:+.1f})")
        tags.append(entry_source)

        now_ms = int(time.time() * 1000)
        sig_data = {
            "symbol": watch.symbol,
            "mode": "DAY",
            "timeframe": f"{watch.sweep_tf}->{exec_tf}",
            "entry_type": entry_type,
            "direction": watch.direction,
            "entry_low": round(e_lo, 8),
            "entry_high": round(e_hi, 8),
            "entry_mid": entry_mid,
            "stop_loss": stop_loss,
            "tp1": tp1,
            "tp2": tp2,
            "rr": rr,
            "confidence": confidence,
            "htf_bias": watch.htf_bias_str,
            "liquidity_level": f"{watch.pool.name} ({watch.pool.price:,.5g})",
            "tp_target_name": tp2_name,
            "reason": " + ".join(tags),
            "status": "ACTIVE" if entry_type == "CMP" else "PENDING",
            "created_at": now_ms,
        }
        sig_id = self.db.insert_signal(sig_data)
        sig_data["id"] = sig_id
        self.last_signal_ts[watch.symbol] = now_ms

        logger.info(
            "CONFIRMED DAY SIGNAL #%d: %s %s [%s] Entry=%.4f SL=%.4f (%.2f%%) TP2=%.4f (%s) RR=1:%.2f Conf=%d",
            sig_id, watch.symbol, watch.direction, entry_type, entry_mid, stop_loss,
            risk_pct * 100.0, tp2, tp2_name, rr, confidence,
        )
        await self.notifier.send_signal_alert(sig_data)
        return sig_data