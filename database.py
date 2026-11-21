import json
import sqlite3
import threading
import time
from typing import Any, Dict, List, Optional

from config import DEFAULT_SETTINGS, sanitize_watchlist


class Database:
    ACTIVE_STATUSES = ("PENDING", "ACTIVE", "TP1_BE")
    CLOSED_STATUSES = ("TP2_HIT", "BE_EXIT", "SL_HIT", "EXPIRED")

    def __init__(self, db_path: str = "orderflow_v3.db"):
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
                entry_type TEXT NOT NULL,
                direction TEXT NOT NULL,
                entry_low REAL NOT NULL,
                entry_high REAL NOT NULL,
                entry_mid REAL NOT NULL,
                stop_loss REAL NOT NULL,
                tp1 REAL NOT NULL,
                tp2 REAL NOT NULL,
                rr REAL NOT NULL,
                confidence INTEGER NOT NULL,
                htf_bias TEXT NOT NULL,
                liquidity_level TEXT NOT NULL,
                tp_target_name TEXT NOT NULL,
                reason TEXT NOT NULL,
                status TEXT NOT NULL,
                created_at INTEGER NOT NULL,
                updated_at INTEGER NOT NULL,
                filled_at INTEGER,
                closed_at INTEGER,
                pnl_r REAL DEFAULT 0.0
            );

            CREATE TABLE IF NOT EXISTS liquidity_levels (
                symbol TEXT PRIMARY KEY,
                utc_day TEXT NOT NULL,
                bias_1d TEXT NOT NULL,
                bias_4h TEXT NOT NULL,
                bias_1h TEXT NOT NULL,
                pdh REAL,
                pdl REAL,
                pwh REAL,
                pwl REAL,
                vwap REAL,
                levels_json TEXT NOT NULL,
                updated_at INTEGER NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_signals_sym_status ON signals(symbol, status);
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
                val = v
                if k == "watchlist" and isinstance(v, list):
                    cleaned = sanitize_watchlist(v, max_items=20)
                    if not cleaned:
                        continue
                    val = cleaned
                elif k == "min_rr":
                    val = round(max(1.8, min(6.0, float(v))), 2)
                elif k == "min_sl_pct":
                    val = round(max(0.35, min(2.5, float(v))), 2)
                elif k == "max_sl_pct":
                    val = round(max(1.5, min(8.0, float(v))), 2)
                elif k == "imbalance_ratio":
                    val = round(max(1.5, min(10.0, float(v))), 2)
                elif k == "max_tier2_streams":
                    val = max(2, min(20, int(v)))
                elif k == "min_confidence":
                    val = max(55, min(95, int(v)))

                self.conn.execute(
                    """
                    INSERT INTO settings (key, value, updated_at)
                    VALUES (?, ?, ?)
                    ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at
                    """,
                    (k, json.dumps(val), now),
                )
            self.conn.commit()
        return self.get_all_settings()

    def save_daily_levels(self, symbol: str, utc_day: str, lmap: Dict[str, Any]) -> None:
        now = int(time.time() * 1000)
        payload = {
            "bsl": lmap.get("bsl", []),
            "ssl": lmap.get("ssl", []),
            "liq_heatmap": lmap.get("liq_heatmap", []),
            "fvgs": lmap.get("fvgs", []),
            "obs": lmap.get("obs", []),
        }
        with self._lock:
            self.conn.execute(
                """
                INSERT INTO liquidity_levels (
                    symbol, utc_day, bias_1d, bias_4h, bias_1h,
                    pdh, pdl, pwh, pwl, vwap, levels_json, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(symbol) DO UPDATE SET
                    utc_day=excluded.utc_day,
                    bias_1d=excluded.bias_1d,
                    bias_4h=excluded.bias_4h,
                    bias_1h=excluded.bias_1h,
                    pdh=excluded.pdh,
                    pdl=excluded.pdl,
                    pwh=excluded.pwh,
                    pwl=excluded.pwl,
                    vwap=excluded.vwap,
                    levels_json=excluded.levels_json,
                    updated_at=excluded.updated_at
                """,
                (
                    symbol.upper(),
                    utc_day,
                    str(lmap.get("bias_1d", "RANGE")),
                    str(lmap.get("bias_4h", "RANGE")),
                    str(lmap.get("bias_1h", "RANGE")),
                    lmap.get("pdh"),
                    lmap.get("pdl"),
                    lmap.get("pwh"),
                    lmap.get("pwl"),
                    lmap.get("vwap"),
                    json.dumps(payload),
                    now,
                ),
            )
            self.conn.commit()

    def get_all_daily_levels(self) -> List[Dict[str, Any]]:
        with self._lock:
            rows = self.conn.execute("SELECT * FROM liquidity_levels ORDER BY symbol ASC").fetchall()
            out: List[Dict[str, Any]] = []
            for r in rows:
                d = dict(r)
                try:
                    d["levels"] = json.loads(d.pop("levels_json", "{}"))
                except json.JSONDecodeError:
                    d["levels"] = {}
                out.append(d)
            return out

    def insert_signal(self, sig: Dict[str, Any]) -> int:
        now = int(time.time() * 1000)
        entry_type = str(sig.get("entry_type", "LIMIT")).upper()
        initial_status = sig.get("status") or ("ACTIVE" if entry_type == "CMP" else "PENDING")
        filled_at = int(sig.get("created_at", now)) if initial_status == "ACTIVE" else None

        with self._lock:
            cur = self.conn.execute(
                """
                INSERT INTO signals (
                    symbol, mode, timeframe, entry_type, direction,
                    entry_low, entry_high, entry_mid, stop_loss, tp1, tp2,
                    rr, confidence, htf_bias, liquidity_level, tp_target_name,
                    reason, status, created_at, updated_at, filled_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    sig["symbol"].upper(),
                    sig.get("mode", "DAY"),
                    sig["timeframe"],
                    entry_type,
                    sig["direction"].upper(),
                    float(sig["entry_low"]),
                    float(sig["entry_high"]),
                    float(sig["entry_mid"]),
                    float(sig["stop_loss"]),
                    float(sig["tp1"]),
                    float(sig["tp2"]),
                    float(sig["rr"]),
                    int(sig["confidence"]),
                    str(sig.get("htf_bias", "1D:RANGE | 4H:RANGE")),
                    str(sig["liquidity_level"]),
                    str(sig.get("tp_target_name", "Opposing Liquidity")),
                    str(sig["reason"]),
                    initial_status,
                    int(sig.get("created_at", now)),
                    now,
                    filled_at,
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
                params.append(round(float(pnl_r), 2))
            if filled_at is not None:
                fields.append("filled_at = ?")
                params.append(int(filled_at))
            if closed_at is not None:
                fields.append("closed_at = ?")
                params.append(int(closed_at))
            params.append(int(signal_id))
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
                    (symbol.upper(),),
                ).fetchall()
            else:
                rows = self.conn.execute(
                    "SELECT * FROM signals WHERE status IN ('PENDING', 'ACTIVE', 'TP1_BE') ORDER BY created_at DESC"
                ).fetchall()
            return [dict(r) for r in rows]

    def get_recent_signals(self, limit: int = 80, symbol: Optional[str] = None) -> List[Dict[str, Any]]:
        with self._lock:
            if symbol:
                rows = self.conn.execute(
                    "SELECT * FROM signals WHERE symbol = ? ORDER BY created_at DESC LIMIT ?",
                    (symbol.upper(), int(limit)),
                ).fetchall()
            else:
                rows = self.conn.execute(
                    "SELECT * FROM signals ORDER BY created_at DESC LIMIT ?",
                    (int(limit),),
                ).fetchall()
            return [dict(r) for r in rows]

    def get_stats(self) -> Dict[str, Any]:
        with self._lock:
            active_cnt = self.conn.execute(
                "SELECT COUNT(*) AS c FROM signals WHERE status IN ('PENDING', 'ACTIVE', 'TP1_BE')"
            ).fetchone()["c"]
            rows = self.conn.execute(
                "SELECT status, pnl_r, closed_at FROM signals WHERE status IN ('TP2_HIT', 'BE_EXIT', 'SL_HIT', 'EXPIRED')"
            ).fetchall()

            now_sec = time.time()
            today_utc_ms = int((now_sec - (now_sec % 86400)) * 1000)

            total_closed = 0
            tp2_wins = 0
            be_exits = 0
            sl_losses = 0
            expired = 0
            net_r = 0.0
            today_r = 0.0

            for r in rows:
                st = r["status"]
                if st == "EXPIRED":
                    expired += 1
                    continue
                total_closed += 1
                pr = float(r["pnl_r"] or 0.0)
                net_r += pr
                if r["closed_at"] and int(r["closed_at"]) >= today_utc_ms:
                    today_r += pr
                if st == "TP2_HIT":
                    tp2_wins += 1
                elif st == "BE_EXIT":
                    be_exits += 1
                elif st == "SL_HIT":
                    sl_losses += 1

            win_rate = round(((tp2_wins + be_exits) / total_closed) * 100.0, 1) if total_closed > 0 else 0.0
            return {
                "active_count": int(active_cnt),
                "total_closed": total_closed,
                "wins": tp2_wins,
                "be_exits": be_exits,
                "losses": sl_losses,
                "expired": expired,
                "win_rate": win_rate,
                "net_r": round(net_r, 2),
                "today_r": round(today_r, 2),
            }

    def close(self) -> None:
        with self._lock:
            self.conn.close()