import json
import sqlite3
import threading
import time
from typing import Any, Dict, List, Optional
from config import DEFAULT_WATCHLIST

DEFAULT_SETTINGS: Dict[str, Any] = {
    "telegram_bot_token": "",
    "telegram_chat_id": "",
    "telegram_enabled": True,
    "watchlist": DEFAULT_WATCHLIST,
    "scalping_enabled": True,       # 1H Bias -> 15m Sweep -> 5m Orderflow MSS
    "day_enabled": True,            # 4H/1H Bias -> 1H Sweep -> 15m Orderflow MSS
    "min_rr": 2.0,                  # Strict Minimum Risk:Reward >= 2.0
    "imbalance_ratio": 3.0,         # 300% diagonal imbalance ratio
    "stacked_min_rows": 3,          # 3+ consecutive imbalanced rows
    "level_proximity_pct": 0.35,    # Max distance (%) from key liquidity level to enter WATCHING state
    "sweep_expiry_bars": 6,         # Max confirmation bars to wait after sweep before resetting
    "signal_cooldown_min": 45,      # Per-coin per-mode cooldown after signal fires
    "sl_buffer_pct": 0.08,          # Safe buffer beyond sweep wick (%)
}


class Database:
    def __init__(self, db_path: str = "orderflow_bot.db"):
        self.db_path = db_path
        self._lock = threading.RLock()
        self.conn = sqlite3.connect(self.db_path, check_same_thread=False, timeout=15.0)
        self.conn.row_factory = sqlite3.Row
        with self._lock:
            self.conn.execute("PRAGMA journal_mode=WAL;")
            self.conn.execute("PRAGMA synchronous=NORMAL;")
            self._create_tables()
            self._seed_defaults()

    def _create_tables(self) -> None:
        self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                updated_at INTEGER NOT NULL
            );

            CREATE TABLE IF NOT EXISTS signals (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                symbol TEXT NOT NULL,
                mode TEXT NOT NULL,
                timeframe TEXT NOT NULL,
                direction TEXT NOT NULL,
                entry_low REAL NOT NULL,
                entry_high REAL NOT NULL,
                entry_mid REAL NOT NULL,
                stop_loss REAL NOT NULL,
                tp1 REAL NOT NULL,
                tp2 REAL NOT NULL,
                rr REAL NOT NULL,
                confidence INTEGER NOT NULL,
                liquidity_level TEXT NOT NULL,
                reason TEXT NOT NULL,
                status TEXT NOT NULL,
                created_at INTEGER NOT NULL,
                updated_at INTEGER NOT NULL,
                filled_at INTEGER,
                closed_at INTEGER,
                pnl_r REAL DEFAULT 0.0
            );

            CREATE INDEX IF NOT EXISTS idx_signals_symbol_status ON signals(symbol, status);
            CREATE INDEX IF NOT EXISTS idx_signals_created ON signals(created_at DESC);
            """
        )
        self.conn.commit()

    def _seed_defaults(self) -> None:
        now = int(time.time() * 1000)
        for k, v in DEFAULT_SETTINGS.items():
            cur = self.conn.execute("SELECT key FROM settings WHERE key = ?", (k,))
            if cur.fetchone() is None:
                self.conn.execute(
                    "INSERT INTO settings (key, value, updated_at) VALUES (?, ?, ?)",
                    (k, json.dumps(v), now),
                )
        self.conn.commit()

    def get_all_settings(self) -> Dict[str, Any]:
        with self._lock:
            rows = self.conn.execute("SELECT key, value FROM settings").fetchall()
            out = dict(DEFAULT_SETTINGS)
            for r in rows:
                try:
                    out[r["key"]] = json.loads(r["value"])
                except json.JSONDecodeError:
                    out[r["key"]] = r["value"]
            return out

    def update_settings(self, updates: Dict[str, Any]) -> Dict[str, Any]:
        now = int(time.time() * 1000)
        with self._lock:
            for k, v in updates.items():
                if k not in DEFAULT_SETTINGS:
                    continue
                self.conn.execute(
                    """
                    INSERT INTO settings (key, value, updated_at)
                    VALUES (?, ?, ?)
                    ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at
                    """,
                    (k, json.dumps(v), now),
                )
            self.conn.commit()
        return self.get_all_settings()

    def insert_signal(self, sig: Dict[str, Any]) -> int:
        now = int(time.time() * 1000)
        with self._lock:
            cur = self.conn.execute(
                """
                INSERT INTO signals (
                    symbol, mode, timeframe, direction,
                    entry_low, entry_high, entry_mid, stop_loss, tp1, tp2,
                    rr, confidence, liquidity_level, reason, status,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    sig["symbol"],
                    sig["mode"],
                    sig["timeframe"],
                    sig["direction"],
                    float(sig["entry_low"]),
                    float(sig["entry_high"]),
                    float(sig["entry_mid"]),
                    float(sig["stop_loss"]),
                    float(sig["tp1"]),
                    float(sig["tp2"]),
                    float(sig["rr"]),
                    int(sig["confidence"]),
                    sig["liquidity_level"],
                    sig["reason"],
                    sig.get("status", "PENDING"),
                    int(sig.get("created_at", now)),
                    now,
                ),
            )
            self.conn.commit()
            return int(cur.lastrowid)

    def update_signal_status(
        self,
        signal_id: int,
        status: str,
        pnl_r: Optional[float] = None,
        filled_at: Optional[int] = None,
        closed_at: Optional[int] = None,
    ) -> None:
        now = int(time.time() * 1000)
        with self._lock:
            fields = ["status = ?", "updated_at = ?"]
            params: List[Any] = [status, now]
            if pnl_r is not None:
                fields.append("pnl_r = ?")
                params.append(float(pnl_r))
            if filled_at is not None:
                fields.append("filled_at = ?")
                params.append(int(filled_at))
            if closed_at is not None:
                fields.append("closed_at = ?")
                params.append(int(closed_at))
            params.append(signal_id)
            self.conn.execute(
                f"UPDATE signals SET {', '.join(fields)} WHERE id = ?",
                tuple(params),
            )
            self.conn.commit()

    def get_active_signals(self, symbol: Optional[str] = None) -> List[Dict[str, Any]]:
        with self._lock:
            if symbol:
                rows = self.conn.execute(
                    "SELECT * FROM signals WHERE symbol = ? AND status IN ('PENDING', 'ACTIVE', 'TP1_BE') ORDER BY created_at DESC",
                    (symbol,),
                ).fetchall()
            else:
                rows = self.conn.execute(
                    "SELECT * FROM signals WHERE status IN ('PENDING', 'ACTIVE', 'TP1_BE') ORDER BY created_at DESC"
                ).fetchall()
            return [dict(r) for r in rows]

    def get_recent_signals(self, limit: int = 50, symbol: Optional[str] = None) -> List[Dict[str, Any]]:
        with self._lock:
            if symbol:
                rows = self.conn.execute(
                    "SELECT * FROM signals WHERE symbol = ? ORDER BY created_at DESC LIMIT ?",
                    (symbol, limit),
                ).fetchall()
            else:
                rows = self.conn.execute(
                    "SELECT * FROM signals ORDER BY created_at DESC LIMIT ?",
                    (limit,),
                ).fetchall()
            return [dict(r) for r in rows]

    def get_stats(self) -> Dict[str, Any]:
        with self._lock:
            rows = self.conn.execute(
                "SELECT status, pnl_r FROM signals WHERE status IN ('TP1_BE', 'TP2_HIT', 'SL_HIT', 'EXPIRED')"
            ).fetchall()
            total_closed = 0
            wins = 0
            losses = 0
            be_count = 0
            total_r = 0.0
            for r in rows:
                st = r["status"]
                if st == "EXPIRED":
                    continue
                total_closed += 1
                total_r += float(r["pnl_r"] or 0.0)
                if st == "TP2_HIT":
                    wins += 1
                elif st == "SL_HIT":
                    losses += 1
                elif st == "TP1_BE":
                    be_count += 1
            win_rate = round((wins + be_count * 0.5) / total_closed * 100, 1) if total_closed else 0.0
            return {
                "total_closed": total_closed,
                "wins": wins,
                "be_partials": be_count,
                "losses": losses,
                "win_rate": win_rate,
                "net_r": round(total_r, 2),
            }