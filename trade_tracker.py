import logging
import time
from typing import Any, Dict, List, Optional

from database import Database
from footprint_engine import SymbolOrderflowState
from telegram_bot import TelegramNotifier

logger = logging.getLogger("trade_tracker")


class TradeTracker:
    """
    Monitors active Day-Mode signals (PENDING LIMIT, ACTIVE, TP1_BE) on live ticks.
    Automatically resets coin state back to WATCHING as soon as a signal closes
    (SL_HIT before TP1, BE_EXIT after TP1, TP2_HIT full target, or EXPIRED).
    """

    MAX_PENDING_MS = 6 * 3_600_000  # 6 hours max wait for a Day-Mode LIMIT fill

    def __init__(self, db: Database, notifier: TelegramNotifier):
        self.db = db
        self.notifier = notifier

    async def process_price_tick(
        self,
        symbol: str,
        price: float,
        state: Optional[SymbolOrderflowState] = None,
        strategy: Optional[Any] = None,
    ) -> List[Dict[str, Any]]:
        if price <= 0:
            return []
        sym = symbol.upper()
        active_list = self.db.get_active_signals(sym)
        if not active_list:
            return []

        now_ms = int(time.time() * 1000)
        updates: List[Dict[str, Any]] = []
        any_closed = False
        was_expired_only = True

        for sig in active_list:
            sid = int(sig["id"])
            status = sig["status"]
            direction = sig["direction"]
            is_long = direction == "LONG"
            e_lo = float(sig["entry_low"])
            e_hi = float(sig["entry_high"])
            e_mid = float(sig["entry_mid"])
            sl = float(sig["stop_loss"])
            tp1 = float(sig["tp1"])
            tp2 = float(sig["tp2"])
            rr = float(sig["rr"])
            created_at = int(sig["created_at"])

            if status == "PENDING":
                if (is_long and price >= tp1) or (not is_long and price <= tp1):
                    self.db.update_signal_status(sid, "EXPIRED", pnl_r=0.0, closed_at=now_ms)
                    sig["status"], sig["pnl_r"], sig["closed_at"] = "EXPIRED", 0.0, now_ms
                    updates.append(sig)
                    any_closed = True
                    logger.info("[%s] LIMIT #%d EXPIRED (ran to TP1 without fill)", sym, sid)
                    continue
                if (is_long and price <= sl) or (not is_long and price >= sl):
                    self.db.update_signal_status(sid, "EXPIRED", pnl_r=0.0, closed_at=now_ms)
                    sig["status"], sig["pnl_r"], sig["closed_at"] = "EXPIRED", 0.0, now_ms
                    updates.append(sig)
                    any_closed = True
                    logger.info("[%s] LIMIT #%d EXPIRED (invalidated past SL before fill)", sym, sid)
                    continue
                if now_ms - created_at > self.MAX_PENDING_MS:
                    self.db.update_signal_status(sid, "EXPIRED", pnl_r=0.0, closed_at=now_ms)
                    sig["status"], sig["pnl_r"], sig["closed_at"] = "EXPIRED", 0.0, now_ms
                    updates.append(sig)
                    any_closed = True
                    logger.info("[%s] LIMIT #%d EXPIRED (6h timeout)", sym, sid)
                    continue

                filled = (e_lo <= price <= e_hi) or (is_long and price <= e_hi) or (not is_long and price >= e_lo)
                if filled:
                    self.db.update_signal_status(sid, "ACTIVE", filled_at=now_ms)
                    sig["status"], sig["filled_at"] = "ACTIVE", now_ms
                    updates.append(sig)
                    logger.info("[%s] LIMIT #%d FILLED @ %.5g -> ACTIVE", sym, sid, price)
                    await self.notifier.send_trade_update(sig, "FILLED", price)

            elif status == "ACTIVE":
                was_expired_only = False
                if (is_long and price <= sl) or (not is_long and price >= sl):
                    self.db.update_signal_status(sid, "SL_HIT", pnl_r=-1.0, closed_at=now_ms)
                    sig["status"], sig["pnl_r"], sig["closed_at"] = "SL_HIT", -1.0, now_ms
                    updates.append(sig)
                    any_closed = True
                    logger.info("[%s] Signal #%d SL_HIT (-1.00R) @ %.5g", sym, sid, price)
                    await self.notifier.send_trade_update(sig, "SL_HIT", price)
                elif (is_long and price >= tp1) or (not is_long and price <= tp1):
                    self.db.update_signal_status(sid, "TP1_BE", pnl_r=0.75)
                    sig["status"], sig["pnl_r"] = "TP1_BE", 0.75
                    updates.append(sig)
                    logger.info("[%s] Signal #%d TP1 HIT (+0.75R partial, SL -> Breakeven)", sym, sid)
                    await self.notifier.send_trade_update(sig, "TP1_BE", price)

            elif status == "TP1_BE":
                was_expired_only = False
                if (is_long and price >= tp2) or (not is_long and price <= tp2):
                    total_r = round(0.75 + 0.5 * rr, 2)
                    self.db.update_signal_status(sid, "TP2_HIT", pnl_r=total_r, closed_at=now_ms)
                    sig["status"], sig["pnl_r"], sig["closed_at"] = "TP2_HIT", total_r, now_ms
                    updates.append(sig)
                    any_closed = True
                    logger.info("[%s] Signal #%d FULL TP2_HIT (+%.2fR) @ %.5g", sym, sid, total_r, price)
                    await self.notifier.send_trade_update(sig, "TP2_HIT", price)
                elif (is_long and price <= e_mid) or (not is_long and price >= e_mid):
                    self.db.update_signal_status(sid, "BE_EXIT", pnl_r=0.75, closed_at=now_ms)
                    sig["status"], sig["pnl_r"], sig["closed_at"] = "BE_EXIT", 0.75, now_ms
                    updates.append(sig)
                    any_closed = True
                    logger.info("[%s] Signal #%d BE_EXIT after TP1 (+0.75R) @ %.5g", sym, sid, price)
                    await self.notifier.send_trade_update(sig, "BE_EXIT", price)

        if any_closed and not self.db.get_active_signals(sym):
            if strategy is not None:
                strategy.sweep_watches.pop(sym, None)
                strategy.level_watches.pop(sym, None)
                if was_expired_only:
                    strategy.last_signal_ts.pop(sym, None)
                if state is not None:
                    st, note = strategy.get_symbol_status(sym, state)
                    state.scanner_state = st
                    state.scanner_note = note
                    logger.info("[%s] Trade closed -> Auto-Reset to %s (%s)", sym, st, note)
            elif state is not None:
                state.scanner_state = "WATCHING"
                state.scanner_note = "Reset after trade close — scanning liquidity levels"

        return updates