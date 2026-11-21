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

from config import EngineConfig, sanitize_watchlist
from database import Database
from exchange_hub import DualExchangeHub
from footprint_engine import FootprintCandle, SymbolOrderflowState
from liquidity_engine import LiquidityEngine
from strategy_engine import StrategyEngine
from telegram_bot import TelegramNotifier
from trade_tracker import TradeTracker

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
        self.clients[ws] = {"symbol": default_sym, "tf": "15m"}

    def disconnect(self, ws: WebSocket) -> None:
        self.clients.pop(ws, None)

    def set_sub(self, ws: WebSocket, symbol: str, tf: str) -> None:
        if ws in self.clients:
            self.clients[ws]["symbol"] = symbol.upper()
            self.clients[ws]["tf"] = tf

    def pinned_symbols(self) -> set:
        return {v["symbol"] for v in self.clients.values()}

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
hub: Optional[DualExchangeHub] = None
_broadcast_task: Optional[asyncio.Task] = None
_scanner_task: Optional[asyncio.Task] = None


def get_lmap_cached(state: SymbolOrderflowState, valid_tf: str = "15m", max_age_sec: float = 4.0) -> Dict[str, Any]:
    now = time.monotonic()
    if not state._cached_lmap or (now - state._cached_lmap_ts >= max_age_sec):
        state._cached_lmap = LiquidityEngine.build_liquidity_map(state, exec_tf=valid_tf)
        state._cached_lmap_ts = now
    return state._cached_lmap


def refresh_and_save_daily_levels(utc_day: Optional[str] = None) -> int:
    if hub is None:
        return 0
    day_str = utc_day or time.strftime("%Y-%m-%d", time.gmtime())
    watchlist = db.get_all_settings().get("watchlist", [])
    saved_cnt = 0
    for sym in watchlist:
        st = hub.get_state(sym)
        if st is None:
            continue
        lmap = get_lmap_cached(st, "15m", max_age_sec=0.0)
        db.save_daily_levels(sym, day_str, lmap)
        saved_cnt += 1
    logger.info("Saved daily 1D/4H/1H Liquidity Maps for %d coins (%s)", saved_cnt, day_str)
    return saved_cnt


async def handle_daily_rollover(new_utc_day: str) -> None:
    cnt = refresh_and_save_daily_levels(new_utc_day)
    stats = db.get_stats()
    await notifier.send_daily_summary(new_utc_day, stats, cnt)
    await ws_manager.broadcast_event({
        "type": "daily_rollover",
        "utc_day": new_utc_day,
        "daily_levels": db.get_all_daily_levels(),
    })


async def handle_candle_closed(symbol: str, tf: str, closed_candle: FootprintCandle) -> None:
    if hub is None:
        return
    state = hub.get_state(symbol)
    if state is None:
        return
    state._cached_lmap_ts = 0.0
    btc_state = hub.get_state("BTCUSDT")
    sig = await strategy.on_candle_closed(symbol, tf, closed_candle, state, btc_state=btc_state)
    if sig is not None:
        await hub.sync_tier2_subscriptions()
        await ws_manager.broadcast_event({"type": "signal_alert", "signal": sig, "stats": db.get_stats()})
    elif tf == "1h":
        lmap = get_lmap_cached(state, "15m", max_age_sec=0.0)
        db.save_daily_levels(symbol, time.strftime("%Y-%m-%d", time.gmtime()), lmap)


async def handle_live_tick(symbol: str) -> None:
    if hub is None:
        return
    state = hub.get_state(symbol)
    if state is None:
        return
    updates = await tracker.process_price_tick(symbol, state.last_price, state=state, strategy=strategy)
    if updates:
        await hub.sync_tier2_subscriptions()
        await ws_manager.broadcast_event({
            "type": "signals_updated",
            "updates": updates,
            "stats": db.get_stats(),
        })


async def handle_backfill_done(symbol: str) -> None:
    if hub is None:
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


def build_chart_payload(symbol: str, tf: str, full: bool = True) -> Dict[str, Any]:
    if hub is None:
        return {"symbol": symbol, "tf": tf, "candles": []}
    state = hub.get_state(symbol)
    if state is None:
        return {"symbol": symbol, "tf": tf, "candles": []}

    valid_tf = tf if tf in ("5m", "15m", "1h", "4h", "1d") else "15m"
    settings = db.get_all_settings()
    state.ensure_metrics_fresh(
        valid_tf,
        imbalance_ratio=float(settings.get("imbalance_ratio", 3.0)),
        stacked_min=int(settings.get("stacked_min_rows", 3)),
    )

    with state.lock:
        bars = list(state.candles.get(valid_tf, []))
        slice_bars = bars[-250:] if full else bars[-2:]
        candles_out = [b.to_dict(include_rows=True) for b in slice_bars]
        bids = [[p, q] for p, q in state.bids[:12]]
        asks = [[p, q] for p, q in state.asks[:12]]
        bid_walls = list(state.bid_walls)
        ask_walls = list(state.ask_walls)
        ob_imb = state.ob_imbalance_pct
        last_px = state.last_price
        bb_px = state.bybit_last_price
        chg_24h = state.price_change_24h
        bn_oi = state.bn_open_interest
        bb_oi = state.bb_open_interest
        comb_oi = state.combined_oi
        scanner_state = state.scanner_state
        scanner_note = state.scanner_note
        tier2_active = state.tier2_active
        recent_liqs = list(state.recent_liqs)[-12:]

    lmap = get_lmap_cached(state, valid_tf if valid_tf in ("5m", "15m") else "15m", max_age_sec=0.0 if full else 4.0)
    vwap = state.get_session_vwap(valid_tf if valid_tf in ("5m", "15m") else "15m")
    atr_15m = state.get_atr("15m", 14)
    active_sigs = db.get_active_signals(symbol)

    out: Dict[str, Any] = {
        "symbol": symbol,
        "tf": valid_tf,
        "full": full,
        "last_price": last_px,
        "bybit_price": bb_px,
        "change_24h": round(chg_24h, 2),
        "bn_oi": bn_oi,
        "bb_oi": bb_oi,
        "combined_oi": comb_oi,
        "atr_15m": round(atr_15m, 6),
        "tier2_active": tier2_active,
        "scanner_state": scanner_state,
        "scanner_note": scanner_note,
        "bias_1d": lmap["bias_1d"],
        "bias_4h": lmap["bias_4h"],
        "bias_1h": lmap["bias_1h"],
        "aligned_bias": lmap["aligned_bias"],
        "htf_bias_str": lmap["htf_bias_str"],
        "pdh": lmap["pdh"],
        "pdl": lmap["pdl"],
        "pwh": lmap["pwh"],
        "pwl": lmap["pwl"],
        "vwap": vwap,
        "bsl": lmap["bsl"],
        "ssl": lmap["ssl"],
        "bids": bids,
        "asks": asks,
        "bid_walls": bid_walls,
        "ask_walls": ask_walls,
        "ob_imbalance_pct": ob_imb,
        "recent_liqs": recent_liqs,
        "candles": candles_out,
        "active_signals": active_sigs,
    }
    if full:
        out["vp"] = state.get_visible_volume_profile(valid_tf, lookback=250)
        out["npocs"] = state.get_naked_pocs(valid_tf, max_count=8)
        out["fvgs"] = lmap["fvgs"]
        out["obs"] = lmap["obs"]
        out["liq_heatmap"] = lmap["liq_heatmap"]
    return out


def build_watchlist_summary() -> List[Dict[str, Any]]:
    settings = db.get_all_settings()
    watchlist = settings.get("watchlist", [])
    out: List[Dict[str, Any]] = []
    if hub is None:
        return out
    for sym in watchlist:
        st = hub.get_state(sym)
        if st is None:
            continue
        out.append({
            "symbol": sym,
            "price": st.last_price,
            "change_24h": round(st.price_change_24h, 2),
            "bias": st.active_bias,
            "state": st.scanner_state,
            "note": st.scanner_note,
            "tier2": st.tier2_active,
            "ob_imb": st.ob_imbalance_pct,
        })
    return out


async def tier1_scanner_loop() -> None:
    while True:
        try:
            await asyncio.sleep(2.0)
            if hub is None:
                continue
            watchlist = db.get_all_settings().get("watchlist", [])
            need_t2_sync = False

            for sym in watchlist:
                st = hub.get_state(sym)
                if st is None or st.last_price <= 0:
                    continue
                max_age = 4.0 if st.scanner_state in ("AT_LEVEL", "SWEEP_DETECTED") else 20.0
                get_lmap_cached(st, "15m", max_age_sec=max_age)

                updates = await tracker.process_price_tick(sym, st.last_price, state=st, strategy=strategy)
                if updates:
                    need_t2_sync = True
                    await ws_manager.broadcast_event({
                        "type": "signals_updated",
                        "updates": updates,
                        "stats": db.get_stats(),
                    })

                entered_level = await strategy.on_live_tick(sym, st)
                if entered_level:
                    need_t2_sync = True

            if need_t2_sync:
                await hub.sync_tier2_subscriptions()
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.exception("Tier-1 scanner loop error: %s", e)


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
    global hub, _broadcast_task, _scanner_task
    os.makedirs("static", exist_ok=True)
    hub = DualExchangeHub(
        cfg=cfg,
        get_settings_fn=db.get_all_settings,
        on_candle_closed=handle_candle_closed,
        on_live_tick=handle_live_tick,
        on_backfill_done=handle_backfill_done,
        on_daily_rollover=handle_daily_rollover,
    )
    settings = db.get_all_settings()
    await hub.start(settings.get("watchlist", []))
    refresh_and_save_daily_levels()
    await hub.sync_tier2_subscriptions()

    _scanner_task = asyncio.create_task(tier1_scanner_loop(), name="tier1_scanner_loop")
    _broadcast_task = asyncio.create_task(ui_broadcast_loop(), name="ui_broadcast_loop")
    logger.info("OrderFlowX V3.1 Ultra READY at http://%s:%d", cfg.host, cfg.port)
    try:
        yield
    finally:
        for t in (_broadcast_task, _scanner_task):
            if t and not t.done():
                t.cancel()
        if hub:
            await hub.stop()


app = FastAPI(title="OrderFlowX V3.1 Ultra — Binance + Bybit Day Terminal", lifespan=lifespan)
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


@app.get("/api/health")
async def api_health():
    return {
        "ok": True,
        "coins": len(hub.states) if hub else 0,
        "tier2_active": sorted(list(hub._tier2_desired)) if hub else [],
        "stats": db.get_stats(),
    }


@app.get("/api/watchlist")
async def api_watchlist():
    return {"watchlist": build_watchlist_summary(), "stats": db.get_stats()}


@app.get("/api/daily-levels")
async def api_daily_levels():
    return {"levels": db.get_all_daily_levels()}


@app.get("/api/chart/{symbol}")
async def api_chart(symbol: str, tf: str = "15m", full: bool = True):
    sym = symbol.upper()
    if hub:
        hub.set_ui_pinned(ws_manager.pinned_symbols() | {sym})
        hub.prioritize_backfill(sym)
        await hub.sync_tier2_subscriptions()
    return build_chart_payload(sym, tf, full=full)


@app.get("/api/live/{symbol}")
async def api_live(symbol: str, tf: str = "15m"):
    sym = symbol.upper()
    return {
        "chart": build_chart_payload(sym, tf, full=False),
        "watchlist": build_watchlist_summary(),
        "stats": db.get_stats(),
    }


@app.get("/api/signals")
async def api_signals(limit: int = 80, symbol: Optional[str] = None):
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
    min_rr: Optional[float] = None
    min_sl_pct: Optional[float] = None
    max_sl_pct: Optional[float] = None
    sl_atr_mult: Optional[float] = None
    sl_buffer_pct: Optional[float] = None
    imbalance_ratio: Optional[float] = None
    stacked_min_rows: Optional[int] = None
    level_proximity_pct: Optional[float] = None
    sweep_expiry_bars_15m: Optional[int] = None
    sweep_expiry_bars_5m: Optional[int] = None
    signal_cooldown_min: Optional[int] = None
    max_tier2_streams: Optional[int] = None
    cmp_threshold_pct: Optional[float] = None
    min_confidence: Optional[int] = None


@app.post("/api/settings")
async def api_save_settings(payload: SettingsPayload):
    raw_dict = payload.model_dump() if hasattr(payload, "model_dump") else payload.__dict__
    updates = {k: v for k, v in raw_dict.items() if v is not None}
    if "watchlist" in updates:
        cleaned = sanitize_watchlist(updates["watchlist"], max_items=20)
        if not cleaned:
            raise HTTPException(status_code=400, detail="Watchlist needs at least 1 valid USDT Futures symbol")
        updates["watchlist"] = cleaned

    saved = db.update_settings(updates)
    if "watchlist" in updates and hub is not None:
        await hub.reload_watchlist(saved["watchlist"])
        refresh_and_save_daily_levels()
        await hub.sync_tier2_subscriptions()
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
        if hub:
            hub.set_ui_pinned(ws_manager.pinned_symbols())
            hub.prioritize_backfill(sub["symbol"])
            await hub.sync_tier2_subscriptions()
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
                tf = str(msg.get("tf", "15m"))
                ws_manager.set_sub(ws, sym, tf)
                if hub:
                    hub.set_ui_pinned(ws_manager.pinned_symbols())
                    hub.prioritize_backfill(sym)
                    await hub.sync_tier2_subscriptions()
                await ws.send_json({
                    "type": "chart_full",
                    "chart": build_chart_payload(sym, tf, full=True),
                })
    except WebSocketDisconnect:
        ws_manager.disconnect(ws)
        if hub:
            hub.set_ui_pinned(ws_manager.pinned_symbols())
    except Exception:
        ws_manager.disconnect(ws)
        if hub:
            hub.set_ui_pinned(ws_manager.pinned_symbols())


if __name__ == "__main__":
    uvicorn.run("main:app", host=cfg.host, port=cfg.port, reload=False, log_level="info")