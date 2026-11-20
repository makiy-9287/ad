import asyncio
import logging
import os
import time
from contextlib import asynccontextmanager
from typing import Any, Dict, List, Optional

import uvicorn
from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from config import EngineConfig
from database import Database
from footprint_engine import FootprintCandle, SymbolOrderflowState
from liquidity_engine import LiquidityEngine
from strategy_engine import StrategyEngine
from telegram_bot import TelegramNotifier
from trade_tracker import TradeTracker
from ws_collector import BinanceFuturesCollector

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
)
logger = logging.getLogger("main")

cfg = EngineConfig(
    host=os.getenv("HOST", "0.0.0.0"),
    port=int(os.getenv("PORT", "8000")),
)
db = Database(cfg.db_path)
notifier = TelegramNotifier(db)
strategy = StrategyEngine(db, notifier)
tracker = TradeTracker(db, notifier)


class ConnectionManager:
    def __init__(self) -> None:
        self.clients: Dict[WebSocket, Dict[str, str]] = {}

    async def connect(self, ws: WebSocket) -> None:
        await ws.accept()
        watchlist = db.get_all_settings().get("watchlist", ["BTCUSDT"])
        default_sym = watchlist[0] if watchlist else "BTCUSDT"
        self.clients[ws] = {"symbol": default_sym, "tf": "5m"}

    def disconnect(self, ws: WebSocket) -> None:
        self.clients.pop(ws, None)

    def set_sub(self, ws: WebSocket, symbol: str, tf: str) -> None:
        if ws in self.clients:
            self.clients[ws]["symbol"] = symbol.upper()
            self.clients[ws]["tf"] = tf

    async def broadcast_event(self, payload: Dict[str, Any]) -> None:
        dead: List[WebSocket] = []
        for ws in list(self.clients.keys()):
            try:
                await ws.send_json(payload)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.disconnect(ws)


ws_manager = ConnectionManager()
collector: Optional[BinanceFuturesCollector] = None
_broadcast_task: Optional[asyncio.Task] = None


async def handle_candle_closed(symbol: str, tf: str, closed_candle: FootprintCandle) -> None:
    if collector is None:
        return
    state = collector.get_state(symbol)
    if state is None:
        return
    state._cached_lmap_ts = 0.0  # force refresh on bar close
    sig = await strategy.on_candle_closed(symbol, tf, closed_candle, state)
    if sig is not None:
        await ws_manager.broadcast_event({"type": "signal_alert", "signal": sig, "stats": db.get_stats()})


async def handle_live_tick(symbol: str) -> None:
    if collector is None:
        return
    state = collector.get_state(symbol)
    if state is None:
        return
    # Lightweight status check every 3s per symbol to avoid CPU load
    now = time.monotonic()
    if now - state._cached_lmap_ts >= 3.0:
        await strategy.on_live_tick(symbol, state)
        state._cached_lmap_ts = now
    updates = await tracker.process_price_tick(symbol, state.last_price)
    if updates:
        await ws_manager.broadcast_event({
            "type": "signals_updated",
            "updates": updates,
            "stats": db.get_stats(),
        })


async def handle_backfill_done(symbol: str) -> None:
    if collector is None:
        return
    for ws, sub in list(ws_manager.clients.items()):
        if sub["symbol"] == symbol:
            try:
                await ws.send_json({
                    "type": "chart_full",
                    "chart": build_chart_payload(symbol, sub["tf"], full=True),
                })
            except Exception:
                pass


def get_lmap_cached(state: SymbolOrderflowState, valid_tf: str, force: bool = False) -> Dict[str, Any]:
    now = time.monotonic()
    if force or not state._cached_lmap or (now - state._cached_lmap_ts >= 3.0):
        state._cached_lmap = LiquidityEngine.build_liquidity_map(state, exec_tf=valid_tf)
        state._cached_lmap_ts = now
    return state._cached_lmap


def build_chart_payload(symbol: str, tf: str, full: bool = True) -> Dict[str, Any]:
    if collector is None:
        return {"symbol": symbol, "tf": tf, "candles": []}
    state = collector.get_state(symbol)
    if state is None:
        return {"symbol": symbol, "tf": tf, "candles": []}

    valid_tf = tf if tf in ("1m", "5m", "15m", "1h", "4h") else "5m"
    settings = db.get_all_settings()
    state.ensure_metrics_fresh(
        valid_tf,
        imbalance_ratio=float(settings.get("imbalance_ratio", 3.0)),
        stacked_min=int(settings.get("stacked_min_rows", 3)),
    )

    with state.lock:
        bars = list(state.candles.get(valid_tf, []))
        slice_bars = bars[-110:] if full else bars[-2:]
        candles_out = [b.to_dict(include_rows=True) for b in slice_bars]
        bids = [[p, q] for p, q in state.bids[:12]]
        asks = [[p, q] for p, q in state.asks[:12]]
        bid_walls = list(state.bid_walls)
        ask_walls = list(state.ask_walls)
        ob_imb = state.ob_imbalance_pct
        last_px = state.last_price
        chg_24h = state.price_change_24h
        oi = state.open_interest
        scanner_state = state.scanner_state
        scanner_note = state.scanner_note

    lmap = get_lmap_cached(state, valid_tf, force=full)
    npocs = state.get_naked_pocs(valid_tf, max_count=6) if full else []
    vwap = state.get_session_vwap(valid_tf if valid_tf in ("1m", "5m", "15m") else "15m")
    vp = state.get_visible_volume_profile(valid_tf, lookback=100) if full else None
    active_sigs = db.get_active_signals(symbol)

    out: Dict[str, Any] = {
        "symbol": symbol,
        "tf": valid_tf,
        "full": full,
        "last_price": last_px,
        "change_24h": round(chg_24h, 2),
        "open_interest": oi,
        "scanner_state": scanner_state,
        "scanner_note": scanner_note,
        "bias_1h": lmap["bias_1h"],
        "bias_4h": lmap["bias_4h"],
        "vwap": vwap,
        "bsl": lmap["bsl"],
        "ssl": lmap["ssl"],
        "bids": bids,
        "asks": asks,
        "bid_walls": bid_walls,
        "ask_walls": ask_walls,
        "ob_imbalance_pct": ob_imb,
        "candles": candles_out,
        "active_signals": active_sigs,
    }
    if full:
        out["vp"] = vp
        out["npocs"] = npocs
    return out


def build_watchlist_summary() -> List[Dict[str, Any]]:
    settings = db.get_all_settings()
    watchlist = settings.get("watchlist", [])
    out: List[Dict[str, Any]] = []
    if collector is None:
        return out
    for sym in watchlist:
        st = collector.get_state(sym)
        if st is None:
            continue
        out.append({
            "symbol": sym,
            "price": st.last_price,
            "change_24h": round(st.price_change_24h, 2),
            "bias": st.active_bias,
            "state": st.scanner_state,
            "note": st.scanner_note,
            "ob_imb": st.ob_imbalance_pct,
        })
    return out


async def ui_broadcast_loop() -> None:
    while True:
        try:
            await asyncio.sleep(1.0)
            if not ws_manager.clients:
                continue

            watchlist_data = build_watchlist_summary()
            stats_data = db.get_stats()
            payload_cache: Dict[tuple, Dict[str, Any]] = {}
            dead: List[WebSocket] = []

            for ws, sub in list(ws_manager.clients.items()):
                key = (sub["symbol"], sub["tf"])
                if key not in payload_cache:
                    payload_cache[key] = build_chart_payload(sub["symbol"], sub["tf"], full=False)
                msg: Dict[str, Any] = {
                    "type": "live_update",
                    "chart": payload_cache[key],
                    "watchlist": watchlist_data,
                    "stats": stats_data,
                }
                try:
                    await ws.send_json(msg)
                except Exception:
                    dead.append(ws)
            for ws in dead:
                ws_manager.disconnect(ws)
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.exception("UI broadcast loop error: %s", e)


@asynccontextmanager
async def lifespan(app: FastAPI):
    global collector, _broadcast_task
    os.makedirs("static", exist_ok=True)
    collector = BinanceFuturesCollector(
        cfg=cfg,
        get_settings_fn=db.get_all_settings,
        on_candle_closed=handle_candle_closed,
        on_live_tick=handle_live_tick,
        on_backfill_done=handle_backfill_done,
    )
    settings = db.get_all_settings()
    await collector.start(settings.get("watchlist", []))
    _broadcast_task = asyncio.create_task(ui_broadcast_loop(), name="ui_broadcast_loop")
    logger.info("Server ready at http://%s:%d", cfg.host, cfg.port)
    try:
        yield
    finally:
        if _broadcast_task and not _broadcast_task.done():
            _broadcast_task.cancel()
        if collector:
            await collector.stop()


app = FastAPI(title="Orderflow & Liquidity Terminal", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)
os.makedirs("static", exist_ok=True)
app.mount("/static", StaticFiles(directory="static"), name="static")


@app.get("/")
async def index():
    return FileResponse("static/index.html")


@app.get("/api/watchlist")
async def api_watchlist():
    return {"watchlist": build_watchlist_summary(), "stats": db.get_stats()}


@app.get("/api/chart/{symbol}")
async def api_chart(symbol: str, tf: str = "5m", full: bool = True):
    sym = symbol.upper()
    if collector:
        collector.prioritize_backfill(sym)
    return build_chart_payload(sym, tf, full=full)


@app.get("/api/live/{symbol}")
async def api_live(symbol: str, tf: str = "5m"):
    sym = symbol.upper()
    return {
        "chart": build_chart_payload(sym, tf, full=False),
        "watchlist": build_watchlist_summary(),
        "stats": db.get_stats(),
    }


@app.get("/api/signals")
async def api_signals(limit: int = 50, symbol: Optional[str] = None):
    return {
        "signals": db.get_recent_signals(limit=limit, symbol=symbol.upper() if symbol else None),
        "stats": db.get_stats(),
    }


@app.get("/api/settings")
async def api_get_settings():
    return db.get_all_settings()


class SettingsPayload(BaseModel):
    telegram_bot_token: Optional[str] = None
    telegram_chat_id: Optional[str] = None
    telegram_enabled: Optional[bool] = None
    watchlist: Optional[List[str]] = None
    scalping_enabled: Optional[bool] = None
    day_enabled: Optional[bool] = None
    min_rr: Optional[float] = None
    imbalance_ratio: Optional[float] = None
    stacked_min_rows: Optional[int] = None
    level_proximity_pct: Optional[float] = None
    sweep_expiry_bars: Optional[int] = None
    signal_cooldown_min: Optional[int] = None
    sl_buffer_pct: Optional[float] = None


@app.post("/api/settings")
async def api_save_settings(payload: SettingsPayload):
    updates = {k: v for k, v in payload.model_dump().items() if v is not None}
    if "watchlist" in updates:
        cleaned = []
        for s in updates["watchlist"]:
            sym = s.strip().upper()
            if sym and sym.isalnum() and sym.endswith("USDT"):
                if sym not in cleaned:
                    cleaned.append(sym)
        if not cleaned:
            raise HTTPException(status_code=400, detail="Watchlist needs at least 1 valid USDT Futures symbol")
        updates["watchlist"] = cleaned[:15]

    if "min_rr" in updates:
        updates["min_rr"] = max(1.5, min(5.0, float(updates["min_rr"])))
    if "imbalance_ratio" in updates:
        updates["imbalance_ratio"] = max(1.5, min(10.0, float(updates["imbalance_ratio"])))

    saved = db.update_settings(updates)
    if "watchlist" in updates and collector is not None:
        await collector.reload_watchlist(saved["watchlist"])
    return {"ok": True, "settings": saved}


class TelegramTestPayload(BaseModel):
    telegram_bot_token: Optional[str] = None
    telegram_chat_id: Optional[str] = None


@app.post("/api/telegram/test")
async def api_test_telegram(payload: TelegramTestPayload):
    ok, msg = await notifier.send_test_alert(
        token=payload.telegram_bot_token,
        chat_id=payload.telegram_chat_id,
    )
    return {"ok": ok, "message": msg}


@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    await ws_manager.connect(ws)
    try:
        sub = ws_manager.clients[ws]
        if collector:
            collector.prioritize_backfill(sub["symbol"])
        await ws.send_json({
            "type": "init",
            "chart": build_chart_payload(sub["symbol"], sub["tf"], full=True),
            "watchlist": build_watchlist_summary(),
            "stats": db.get_stats(),
        })
        while True:
            msg = await ws.receive_json()
            if msg.get("action") == "subscribe":
                sym = str(msg.get("symbol", "BTCUSDT")).upper()
                tf = str(msg.get("tf", "5m"))
                ws_manager.set_sub(ws, sym, tf)
                if collector:
                    collector.prioritize_backfill(sym)
                await ws.send_json({
                    "type": "chart_full",
                    "chart": build_chart_payload(sym, tf, full=True),
                })
    except WebSocketDisconnect:
        ws_manager.disconnect(ws)
    except Exception:
        ws_manager.disconnect(ws)


if __name__ == "__main__":
    uvicorn.run("main:app", host=cfg.host, port=cfg.port, reload=False, log_level="info")
