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
class SweepWatch:
    symbol: str
    mode: str                 # "SCALPING" | "DAY"
    tf: str                   # "5m" | "15m"
    direction: str            # "LONG" | "SHORT"
    pool: LiquidityPool
    sweep_time: int
    sweep_extreme: float      # high for SHORT, low for LONG
    sweep_poc: float
    sweep_open: float
    sweep_close: float
    structure_break_lvl: float
    trapped_zone: Optional[Tuple[float, float]]
    has_absorption: bool
    has_trapped: bool
    has_div: bool
    has_liq_burst: bool
    htf_bias: str
    bars_waited: int = 0
    sweep_score: int = 0


class StrategyEngine:
    def __init__(self, db: Database, notifier: TelegramNotifier):
        self.db = db
        self.notifier = notifier
        # (symbol, mode) -> SweepWatch
        self.watches: Dict[Tuple[str, str], SweepWatch] = {}
        # (symbol, mode) -> last signal timestamp ms
        self.last_signal_ts: Dict[Tuple[str, str], int] = {}

    def get_symbol_status(self, symbol: str, state: SymbolOrderflowState) -> Tuple[str, str]:
        active = self.db.get_active_signals(symbol)
        if active:
            s = active[0]
            return "ACTIVE_SIGNAL", f"{s['direction']} {s['mode']} ({s['status']})"

        for mode in ("SCALPING", "DAY"):
            w = self.watches.get((symbol, mode))
            if w is not None:
                return (
                    "SWEEP_DETECTED",
                    f"{w.direction} Sweep @ {w.pool.name} ({w.bars_waited} bars — waiting MSS)",
                )

        # Check proximity to top liquidity pools
        settings = self.db.get_all_settings()
        prox_pct = float(settings.get("level_proximity_pct", 0.35))
        lmap = LiquidityEngine.build_liquidity_map(state, "5m")
        px = state.last_price
        if px > 0:
            for pool in (lmap.get("bsl_objs", []) + lmap.get("ssl_objs", [])):
                if pool.strength >= 3 and abs(pool.price - px) / px * 100.0 <= prox_pct:
                    return "AT_LEVEL", f"Near {pool.name} ({pool.price:,.4g})"

        return "SCANNING", f"1H Bias: {lmap.get('bias_1h', 'RANGE')}"

    async def on_live_tick(self, symbol: str, state: SymbolOrderflowState) -> None:
        st, note = self.get_symbol_status(symbol, state)
        state.scanner_state = st
        state.scanner_note = note

    async def on_candle_closed(
        self, symbol: str, closed_tf: str, closed_candle: FootprintCandle, state: SymbolOrderflowState
    ) -> Optional[Dict[str, Any]]:
        settings = self.db.get_all_settings()

        if closed_tf == "5m" and settings.get("scalping_enabled", True):
            sig = await self._evaluate_mode(symbol, "SCALPING", "5m", closed_candle, state, settings)
            if sig:
                return sig

        if closed_tf == "15m" and settings.get("day_enabled", True):
            sig = await self._evaluate_mode(symbol, "DAY", "15m", closed_candle, state, settings)
            if sig:
                return sig

        st, note = self.get_symbol_status(symbol, state)
        state.scanner_state = st
        state.scanner_note = note
        return None

    async def _evaluate_mode(
        self,
        symbol: str,
        mode: str,
        exec_tf: str,
        bar: FootprintCandle,
        state: SymbolOrderflowState,
        settings: Dict[str, Any],
    ) -> Optional[Dict[str, Any]]:
        key = (symbol, mode)

        # Do not open duplicate signal on same symbol+mode while active
        active_for_sym = [s for s in self.db.get_active_signals(symbol) if s["mode"] == mode]
        if active_for_sym:
            self.watches.pop(key, None)
            return None

        # Cooldown check
        cooldown_ms = int(settings.get("signal_cooldown_min", 45)) * 60_000
        if time.time() * 1000 - self.last_signal_ts.get(key, 0) < cooldown_ms:
            return None

        with state.lock:
            bars = list(state.candles.get(exec_tf, []))
        if len(bars) < 25:
            return None

        # Make sure `bar` is the last closed bar in `bars`
        closed_bars = [b for b in bars if b.is_closed]
        if len(closed_bars) < 15:
            return None
        bar = closed_bars[-1]

        lmap = LiquidityEngine.build_liquidity_map(state, exec_tf=exec_tf)

        # If currently watching a sweep, test Invalidation OR Gate 3/4 MSS Confirmation
        watch = self.watches.get(key)
        if watch is not None:
            watch.bars_waited += 1
            # Invalidation checks (Liquidity Run instead of Sweep!)
            if watch.direction == "SHORT" and bar.close > watch.sweep_extreme:
                logger.info("[%s %s] Sweep invalidated (Breakout Run above %.4f)", symbol, mode, watch.sweep_extreme)
                self.watches.pop(key, None)
                return None
            if watch.direction == "LONG" and bar.close < watch.sweep_extreme:
                logger.info("[%s %s] Sweep invalidated (Breakdown Run below %.4f)", symbol, mode, watch.sweep_extreme)
                self.watches.pop(key, None)
                return None

            # Update extreme wick if re-swept slightly without closing beyond
            if watch.direction == "SHORT" and bar.high > watch.sweep_extreme and bar.close < watch.pool.price:
                watch.sweep_extreme = bar.high
            elif watch.direction == "LONG" and bar.low < watch.sweep_extreme and bar.close > watch.pool.price:
                watch.sweep_extreme = bar.low

            # Expiry check
            max_wait = int(settings.get("sweep_expiry_bars", 6))
            if watch.bars_waited > max_wait:
                self.watches.pop(key, None)
                return None

            # Check Gate 3 + Gate 4 Confirmation
            sig = await self._check_confirmation(watch, closed_bars, state, lmap, settings)
            if sig is not None:
                self.watches.pop(key, None)
                return sig
            return None

        # Otherwise (State: SCANNING / AT_LEVEL) -> Check Gate 1 + Gate 2 (Fresh Sweep + Absorption!)
        new_watch = self._detect_sweep_and_absorption(symbol, mode, exec_tf, closed_bars, state, lmap)
        if new_watch is not None:
            self.watches[key] = new_watch
            logger.info(
                "[%s %s] SWEEP DETECTED: %s @ %s (%.4f), score=%d",
                symbol, mode, new_watch.direction, new_watch.pool.name, new_watch.pool.price, new_watch.sweep_score,
            )
            # Check if the sweep candle itself already printed an aggressive engulfing MSS + Stacked Imbalance
            sig = await self._check_confirmation(new_watch, closed_bars, state, lmap, settings, same_bar=True)
            if sig is not None:
                self.watches.pop(key, None)
                return sig

        return None

    def _detect_sweep_and_absorption(
        self,
        symbol: str,
        mode: str,
        exec_tf: str,
        closed_bars: List[FootprintCandle],
        state: SymbolOrderflowState,
        lmap: Dict[str, Any],
    ) -> Optional[SweepWatch]:
        c = closed_bars[-1]
        c_range = c.high - c.low
        if c_range <= 0:
            return None

        htf_bias = lmap["bias_4h"] if mode == "DAY" else lmap["bias_1h"]
        min_pool_strength = 4 if mode == "DAY" else 3

        # 1. Check Bearish Sweep (BSL Pool above/near high) -> Potential SHORT
        bsl_candidates: List[LiquidityPool] = [
            p for p in lmap.get("bsl_objs", [])
            if p.strength >= min_pool_strength and c.high > p.price and c.close < p.price
        ]
        # Also include pools slightly below c.high that got swept (compare against c.open/prev close)
        if not bsl_candidates:
            # Check recent 2-bar high pool from map
            for p in lmap.get("bsl_objs", []):
                if p.strength >= min_pool_strength and c.high > p.price and c.close < p.price:
                    bsl_candidates.append(p)

        if bsl_candidates:
            pool = max(bsl_candidates, key=lambda p: p.strength)
            upper_wick = c.high - max(c.open, c.close)
            wick_ratio = upper_wick / c_range
            if wick_ratio >= 0.28:
                # Counter-trend strictness: if HTF is BULLISH, only allow strength >= 4 pools + clear absorption/trapped
                has_abs = (c.absorption == "BEAR")
                has_trap = (c.trapped == "TRAPPED_LONGS")
                has_div = (c.divergence == "BEAR_DIV")
                has_liq = (c.liq_buy_usd >= 25_000)
                has_ob_wall = (state.ob_imbalance_pct <= -4.0)

                orderflow_hits = sum([has_abs, has_trap, has_div, has_liq, c.delta < 0, has_ob_wall])
                allow_bias = (
                    htf_bias != "BULLISH"
                    or (pool.strength >= 4 and (has_abs or has_trap))
                )

                if allow_bias and (has_abs or has_trap or orderflow_hits >= 2):
                    # Determine nearest micro swing low before sweep for MSS break level
                    prior_lows = [b.low for b in closed_bars[-6:-1]]
                    micro_low = min(prior_lows[-2:]) if len(prior_lows) >= 2 else c.low
                    struct_lvl = max(micro_low, c.low) if c.close > c.open else c.low

                    score = 50 + (15 if htf_bias == "BEARISH" else (5 if htf_bias == "RANGE" else 0))
                    score += 10 if pool.strength >= 4 else 5
                    score += 10 if (has_abs or has_trap) else 0
                    score += 5 if (has_div or has_liq or has_ob_wall) else 0

                    return SweepWatch(
                        symbol=symbol,
                        mode=mode,
                        tf=exec_tf,
                        direction="SHORT",
                        pool=pool,
                        sweep_time=c.open_time,
                        sweep_extreme=c.high,
                        sweep_poc=c.poc_price,
                        sweep_open=c.open,
                        sweep_close=c.close,
                        structure_break_lvl=struct_lvl,
                        trapped_zone=c.trapped_zone,
                        has_absorption=has_abs,
                        has_trapped=has_trap,
                        has_div=has_div,
                        has_liq_burst=has_liq,
                        htf_bias=htf_bias,
                        sweep_score=score,
                    )

        # 2. Check Bullish Sweep (SSL Pool below/near low) -> Potential LONG
        ssl_candidates: List[LiquidityPool] = [
            p for p in lmap.get("ssl_objs", [])
            if p.strength >= min_pool_strength and c.low < p.price and c.close > p.price
        ]
        if ssl_candidates:
            pool = max(ssl_candidates, key=lambda p: p.strength)
            lower_wick = min(c.open, c.close) - c.low
            wick_ratio = lower_wick / c_range
            if wick_ratio >= 0.28:
                has_abs = (c.absorption == "BULL")
                has_trap = (c.trapped == "TRAPPED_SHORTS")
                has_div = (c.divergence == "BULL_DIV")
                has_liq = (c.liq_sell_usd >= 25_000)
                has_ob_wall = (state.ob_imbalance_pct >= 4.0)

                orderflow_hits = sum([has_abs, has_trap, has_div, has_liq, c.delta > 0, has_ob_wall])
                allow_bias = (
                    htf_bias != "BEARISH"
                    or (pool.strength >= 4 and (has_abs or has_trap))
                )

                if allow_bias and (has_abs or has_trap or orderflow_hits >= 2):
                    prior_highs = [b.high for b in closed_bars[-6:-1]]
                    micro_high = max(prior_highs[-2:]) if len(prior_highs) >= 2 else c.high
                    struct_lvl = min(micro_high, c.high) if c.close < c.open else c.high

                    score = 50 + (15 if htf_bias == "BULLISH" else (5 if htf_bias == "RANGE" else 0))
                    score += 10 if pool.strength >= 4 else 5
                    score += 10 if (has_abs or has_trap) else 0
                    score += 5 if (has_div or has_liq or has_ob_wall) else 0

                    return SweepWatch(
                        symbol=symbol,
                        mode=mode,
                        tf=exec_tf,
                        direction="LONG",
                        pool=pool,
                        sweep_time=c.open_time,
                        sweep_extreme=c.low,
                        sweep_poc=c.poc_price,
                        sweep_open=c.open,
                        sweep_close=c.close,
                        structure_break_lvl=struct_lvl,
                        trapped_zone=c.trapped_zone,
                        has_absorption=has_abs,
                        has_trapped=has_trap,
                        has_div=has_div,
                        has_liq_burst=has_liq,
                        htf_bias=htf_bias,
                        sweep_score=score,
                    )

        return None

    async def _check_confirmation(
        self,
        watch: SweepWatch,
        closed_bars: List[FootprintCandle],
        state: SymbolOrderflowState,
        lmap: Dict[str, Any],
        settings: Dict[str, Any],
        same_bar: bool = False,
    ) -> Optional[Dict[str, Any]]:
        bar = closed_bars[-1]
        prev = closed_bars[-2]
        prev2 = closed_bars[-3]

        min_rr = float(settings.get("min_rr", 2.0))
        sl_buf_pct = float(settings.get("sl_buffer_pct", 0.08))

        # Gate 3: Structure Break (MSS) + Delta Shift + Stacked Imbalance / FVG
        if watch.direction == "SHORT":
            # Same-bar engulfing requires closing strongly bearish below sweep_open & strong negative delta
            if same_bar:
                mss_ok = (bar.close < bar.open) and (bar.close < watch.sweep_open) and ((bar.high - bar.close) / max(bar.high - bar.low, 1e-9) >= 0.60)
            else:
                # Subsequent bar closes below micro structure break level OR below sweep bar low / POC
                break_ref = min(watch.structure_break_lvl, watch.sweep_poc) if watch.sweep_poc > 0 else watch.structure_break_lvl
                mss_ok = (bar.close < bar.open) and (bar.close < break_ref or bar.close < prev.low)

            delta_ok = bar.delta < 0 and (bar.delta + prev.delta < 0 or bar.delta < 0)
            if not (mss_ok and delta_ok):
                return None

            # Find Entry Zone: Priority 1 = Stacked Bear Imbalance, 2 = Bearish FVG, 3 = Trapped Longs / Sweep POC
            bear_stacked = [z for z in bar.stacked_zones if z["direction"] == "BEAR"]
            if not bear_stacked and not same_bar:
                bear_stacked = [z for z in prev.stacked_zones if z["direction"] == "BEAR"]

            fvg_zone: Optional[Tuple[float, float]] = None
            if len(closed_bars) >= 3 and bar.high < prev2.low:
                fvg_zone = (bar.high, prev2.low)

            entry_source = ""
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
                # Candle POC / 50% retrace zone
                poc = bar.poc_price if bar.poc_price > bar.close else watch.sweep_poc
                e_lo = min(poc, (bar.open + bar.close) / 2.0)
                e_hi = max(poc, bar.open)
                entry_source = "Displacement OB / POC"

            # Ensure entry zone sits safely above current close & below sweep extreme
            e_lo = max(e_lo, bar.close)
            e_hi = min(max(e_hi, e_lo + bar.row_size), watch.sweep_extreme * 0.9997)
            if e_hi <= e_lo:
                e_lo = bar.close
                e_hi = min(bar.close + (watch.sweep_extreme - bar.close) * 0.45, watch.sweep_extreme * 0.9995)

            entry_mid = round((e_lo + e_hi) / 2.0, 8)
            stop_loss = round(watch.sweep_extreme * (1.0 + sl_buf_pct / 100.0), 8)
            risk = stop_loss - entry_mid
            if risk <= 0 or (risk / entry_mid) < 0.0012:
                return None

            # Gate 4: Opposing Liquidity Target (TP1 & TP2) & Strict RR >= min_rr
            ssl_targets: List[LiquidityPool] = [
                p for p in lmap.get("ssl_objs", [])
                if p.price < entry_mid - risk * min_rr
            ]
            # Check if a strong opposing SSL blocks the path (< 1.75R)
            blocking_ssl = [
                p for p in lmap.get("ssl_objs", [])
                if p.strength >= 4 and (entry_mid - risk * 1.75) < p.price < bar.close
            ]
            if blocking_ssl:
                return None

            tp1 = round(entry_mid - 1.5 * risk, 8)
            # Pick nearest solid opposing target up to 4.5R
            valid_targets = [p for p in ssl_targets if p.price >= entry_mid - 4.5 * risk]
            if valid_targets:
                # Highest-strength target in 2R-4.5R window
                tp2_pool = max(valid_targets, key=lambda p: (p.strength, p.price))
                tp2 = round(tp2_pool.price, 8)
                tp2_name = tp2_pool.name
            else:
                tp2 = round(entry_mid - 2.5 * risk, 8)
                tp2_name = "2.5R Extension"

            rr = round((entry_mid - tp2) / risk, 2)
            if rr < min_rr:
                return None

            confidence = watch.sweep_score + (10 if bear_stacked else (5 if fvg_zone else 0)) + (5 if rr >= 2.5 else 0)
            confidence = min(98, confidence)
            if confidence < 70:
                return None

        else:  # LONG
            if same_bar:
                mss_ok = (bar.close > bar.open) and (bar.close > watch.sweep_open) and ((bar.close - bar.low) / max(bar.high - bar.low, 1e-9) >= 0.60)
            else:
                break_ref = max(watch.structure_break_lvl, watch.sweep_poc) if watch.sweep_poc > 0 else watch.structure_break_lvl
                mss_ok = (bar.close > bar.open) and (bar.close > break_ref or bar.close > prev.high)

            delta_ok = bar.delta > 0 and (bar.delta + prev.delta > 0 or bar.delta > 0)
            if not (mss_ok and delta_ok):
                return None

            bull_stacked = [z for z in bar.stacked_zones if z["direction"] == "BULL"]
            if not bull_stacked and not same_bar:
                bull_stacked = [z for z in prev.stacked_zones if z["direction"] == "BULL"]

            fvg_zone = None
            if len(closed_bars) >= 3 and bar.low > prev2.high:
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
                e_hi = max(poc, (bar.open + bar.close) / 2.0)
                e_lo = min(poc, bar.open)
                entry_source = "Displacement OB / POC"

            e_hi = min(e_hi, bar.close)
            e_lo = max(min(e_lo, e_hi - bar.row_size), watch.sweep_extreme * 1.0003)
            if e_lo >= e_hi:
                e_hi = bar.close
                e_lo = max(bar.close - (bar.close - watch.sweep_extreme) * 0.45, watch.sweep_extreme * 1.0005)

            entry_mid = round((e_lo + e_hi) / 2.0, 8)
            stop_loss = round(watch.sweep_extreme * (1.0 - sl_buf_pct / 100.0), 8)
            risk = entry_mid - stop_loss
            if risk <= 0 or (risk / entry_mid) < 0.0012:
                return None

            bsl_targets: List[LiquidityPool] = [
                p for p in lmap.get("bsl_objs", [])
                if p.price > entry_mid + risk * min_rr
            ]
            blocking_bsl = [
                p for p in lmap.get("bsl_objs", [])
                if p.strength >= 4 and bar.close < p.price < (entry_mid + risk * 1.75)
            ]
            if blocking_bsl:
                return None

            tp1 = round(entry_mid + 1.5 * risk, 8)
            valid_targets = [p for p in bsl_targets if p.price <= entry_mid + 4.5 * risk]
            if valid_targets:
                tp2_pool = max(valid_targets, key=lambda p: (p.strength, -p.price))
                tp2 = round(tp2_pool.price, 8)
                tp2_name = tp2_pool.name
            else:
                tp2 = round(entry_mid + 2.5 * risk, 8)
                tp2_name = "2.5R Extension"

            rr = round((tp2 - entry_mid) / risk, 2)
            if rr < min_rr:
                return None

            confidence = watch.sweep_score + (10 if bull_stacked else (5 if fvg_zone else 0)) + (5 if rr >= 2.5 else 0)
            confidence = min(98, confidence)
            if confidence < 70:
                return None

        # Build human-readable confirmation reason
        tags = []
        if watch.has_absorption:
            tags.append("Absorption")
        if watch.has_trapped:
            tags.append("Trapped Longs" if watch.direction == "SHORT" else "Trapped Shorts")
        if watch.has_div:
            tags.append("Delta Div")
        if watch.has_liq_burst:
            tags.append("Liq Flush")
        tags.append(f"Delta Flip ({bar.delta:+.1f})")
        tags.append(entry_source)
        tags.append(f"Target: {tp2_name}")
        reason_str = " + ".join(tags)

        now_ms = int(time.time() * 1000)
        sig_data = {
            "symbol": watch.symbol,
            "mode": watch.mode,
            "timeframe": watch.tf,
            "direction": watch.direction,
            "entry_low": round(e_lo, 8),
            "entry_high": round(e_hi, 8),
            "entry_mid": entry_mid,
            "stop_loss": stop_loss,
            "tp1": tp1,
            "tp2": tp2,
            "rr": rr,
            "confidence": confidence,
            "liquidity_level": f"{watch.pool.name} ({watch.pool.price:,.5g})",
            "reason": reason_str,
            "status": "PENDING",
            "created_at": now_ms,
        }
        sig_id = self.db.insert_signal(sig_data)
        sig_data["id"] = sig_id
        self.last_signal_ts[(watch.symbol, watch.mode)] = now_ms

        logger.info("CONFIRMED SIGNAL #%d: %s %s %s Entry=%.4f SL=%.4f TP2=%.4f RR=%.2f",
                    sig_id, watch.symbol, watch.mode, watch.direction, entry_mid, stop_loss, tp2, rr)
        await self.notifier.send_signal_alert(sig_data)
        return sig_data