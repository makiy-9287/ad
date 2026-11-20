import logging
import time
from typing import Any, Dict, List

from database import Database
from telegram_bot import TelegramNotifier

logger = logging.getLogger("trade_tracker")


class TradeTracker:
    def __init__(self, db: Database, notifier: TelegramNotifier):
        self.db = db
        self.notifier = notifier

    async def process_price_tick(self, symbol: str, price: float) -> List[Dict[str, Any]]:
        if price <= 0:
            return []
        active_list = self.db.get_active_signals(symbol)
        if not active_list:
            return []

        now_ms = int(time.time() * 1000)
        updates: List[Dict[str, Any]] = []

        for sig in active_list:
            if sig.get("closed_at") is not None:
                continue

            sid = int(sig["id"])
            status = sig["status"]
            direction = sig["direction"]
            is_long = direction == "LONG"
            e_lo, e_hi, e_mid = float(sig["entry_low"]), float(sig["entry_high"]), float(sig["entry_mid"])
            sl = float(sig["stop_loss"])
            tp1 = float(sig["tp1"])
            tp2 = float(sig["tp2"])
            rr = float(sig["rr"])
            created_at = int(sig["created_at"])

            # Max pending life: 12 execution bars (60 min for 5m SCALPING, 180 min for 15m DAY)
            max_pending_ms = (60 if sig["timeframe"] == "5m" else 180) * 60_000

            if status == "PENDING":
                # Missed retrace -> hit TP1 directly without filling, or invalidated past SL, or expired
                if (is_long and price >= tp1) or (not is_long and price <= tp1):
                    self.db.update_signal_status(sid, "EXPIRED", pnl_r=0.0, closed_at=now_ms)
                    sig["status"] = "EXPIRED"
                    updates.append(sig)
                    continue
                if (is_long and price <= sl) or (not is_long and price >= sl):
                    self.db.update_signal_status(sid, "EXPIRED", pnl_r=0.0, closed_at=now_ms)
                    sig["status"] = "EXPIRED"
                    updates.append(sig)
                    continue
                if now_ms - created_at > max_pending_ms:
                    self.db.update_signal_status(sid, "EXPIRED", pnl_r=0.0, closed_at=now_ms)
                    sig["status"] = "EXPIRED"
                    updates.append(sig)
                    continue

                # Check Entry Zone Fill
                filled = (e_lo <= price <= e_hi) or (is_long and price <= e_hi) or (not is_long and price >= e_lo)
                if filled:
                    self.db.update_signal_status(sid, "ACTIVE", filled_at=now_ms)
                    sig["status"] = "ACTIVE"
                    sig["filled_at"] = now_ms
                    updates.append(sig)

            elif status == "ACTIVE":
                if (is_long and price <= sl) or (not is_long and price >= sl):
                    self.db.update_signal_status(sid, "SL_HIT", pnl_r=-1.0, closed_at=now_ms)
                    sig["status"] = "SL_HIT"
                    sig["pnl_r"] = -1.0
                    sig["closed_at"] = now_ms
                    updates.append(sig)
                    await self.notifier.send_trade_update(sig, "SL_HIT", price)
                elif (is_long and price >= tp1) or (not is_long and price <= tp1):
                    self.db.update_signal_status(sid, "TP1_BE", pnl_r=0.75)
                    sig["status"] = "TP1_BE"
                    sig["pnl_r"] = 0.75
                    updates.append(sig)
                    await self.notifier.send_trade_update(sig, "TP1_BE", price)

            elif status == "TP1_BE":
                if (is_long and price >= tp2) or (not is_long and price <= tp2):
                    total_r = round(0.75 + 0.5 * rr, 2)
                    self.db.update_signal_status(sid, "TP2_HIT", pnl_r=total_r, closed_at=now_ms)
                    sig["status"] = "TP2_HIT"
                    sig["pnl_r"] = total_r
                    sig["closed_at"] = now_ms
                    updates.append(sig)
                    await self.notifier.send_trade_update(sig, "TP2_HIT", price)
                elif (is_long and price <= e_mid) or (not is_long and price >= e_mid):
                    # Remaining 50% stopped at Breakeven after TP1 partial
                    self.db.update_signal_status(sid, "TP1_BE", pnl_r=0.75, closed_at=now_ms)
                    sig["pnl_r"] = 0.75
                    sig["closed_at"] = now_ms
                    updates.append(sig)
                    await self.notifier.send_trade_update(sig, "BE_EXIT", price)

        return updates