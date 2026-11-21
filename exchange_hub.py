import asyncio
import json
import logging
import time
from typing import Any, Awaitable, Callable, Dict, List, Optional, Set

import aiohttp
import websockets

from config import (
    BINANCE_FAPI_REST,
    BINANCE_FAPI_WS,
    BYBIT_V5_REST,
    BYBIT_V5_WS_LINEAR,
    EngineConfig,
)
from footprint_engine import FootprintCandle, SymbolOrderflowState

logger = logging.getLogger("exchange_hub")


class DualExchangeHub:
    """
    2-Tier Dual-Exchange (Binance USDS-M + Bybit Linear V5) Data & Orderflow Collector.
    - Tier 1 (Always-On for all 20 coins): 1s miniTicker + 5m/15m/1h klines + 260-bar REST history + 1D/4H rollover.
    - Tier 2 (Dynamic Heavy Orderflow): Auto-subscribes Binance (aggTrade, depth20, forceOrder) +
      Bybit (publicTrade, orderbook.50, allLiquidation) whenever a coin enters AT_LEVEL or is viewed on Web UI!
    """

    def __init__(
        self,
        cfg: EngineConfig,
        get_settings_fn: Callable[[], Dict[str, Any]],
        on_candle_closed: Optional[Callable[[str, str, FootprintCandle], Awaitable[None]]] = None,
        on_live_tick: Optional[Callable[[str], Awaitable[None]]] = None,
        on_backfill_done: Optional[Callable[[str], Awaitable[None]]] = None,
        on_daily_rollover: Optional[Callable[[str], Awaitable[None]]] = None,
    ):
        self.cfg = cfg
        self.get_settings = get_settings_fn
        self.on_candle_closed = on_candle_closed
        self.on_live_tick = on_live_tick
        self.on_backfill_done = on_backfill_done
        self.on_daily_rollover = on_daily_rollover

        self.states: Dict[str, SymbolOrderflowState] = {}
        self._watchlist: List[str] = []
        self._running = False
        self._reload_bn_requested = False

        self._tier2_desired: Set[str] = {"BTCUSDT"}
        self._bn_tier2_active: Set[str] = set()
        self._bb_tier2_active: Set[str] = set()
        self._ui_pinned_symbols: Set[str] = set()
        self._sync_lock = asyncio.Lock()
        self._ctrl_id = 10

        self._bn_ws: Any = None
        self._bb_ws: Any = None

        self._bn_task: Optional[asyncio.Task] = None
        self._bb_task: Optional[asyncio.Task] = None
        self._poll_task: Optional[asyncio.Task] = None
        self._backfill_task: Optional[asyncio.Task] = None

        self._backfill_priority: List[str] = []
        self._last_tick_notify: Dict[str, float] = {}
        self._last_utc_day: str = time.strftime("%Y-%m-%d", time.gmtime())

    def get_state(self, symbol: str) -> Optional[SymbolOrderflowState]:
        return self.states.get(symbol.upper())

    def set_ui_pinned(self, symbols: Set[str]) -> None:
        self._ui_pinned_symbols = {s.upper() for s in symbols if s.upper() in self.states}

    def prioritize_backfill(self, symbol: str) -> None:
        sym = symbol.upper()
        if sym in self.states and sym not in self._backfill_priority:
            self._backfill_priority.insert(0, sym)

    async def start(self, watchlist: List[str]) -> None:
        self._running = True
        self._watchlist = [s.upper().strip() for s in watchlist if s.strip()]
        for sym in self._watchlist:
            if sym not in self.states:
                self.states[sym] = SymbolOrderflowState(sym, max_candles=self.cfg.max_candles_per_tf)

        await self.bootstrap_history(self._watchlist)
        initial_t2 = [s for s in (["BTCUSDT"] + self._watchlist[:2]) if s in self.states]
        self._tier2_desired = set(initial_t2)
        for s in self._tier2_desired:
            self.states[s].tier2_active = True

        self._bn_task = asyncio.create_task(self._bn_ws_loop(), name="binance_ws_loop")
        self._bb_task = asyncio.create_task(self._bb_ws_loop(), name="bybit_ws_loop")
        self._poll_task = asyncio.create_task(self._periodic_rest_loop(), name="dual_rest_poll")
        self._backfill_task = asyncio.create_task(self._backfill_loop(), name="aggtrade_backfill")

    async def stop(self) -> None:
        self._running = False
        for t in (self._bn_task, self._bb_task, self._poll_task, self._backfill_task):
            if t and not t.done():
                t.cancel()
                try:
                    await t
                except asyncio.CancelledError:
                    pass

    async def reload_watchlist(self, new_watchlist: List[str]) -> None:
        cleaned = [s.upper().strip() for s in new_watchlist if s.strip()]
        if cleaned == self._watchlist:
            return
        logger.info("Reloading watchlist (%d symbols)", len(cleaned))
        added = [s for s in cleaned if s not in self.states]
        for sym in added:
            self.states[sym] = SymbolOrderflowState(sym, max_candles=self.cfg.max_candles_per_tf)
        self._watchlist = cleaned
        if added:
            await self.bootstrap_history(added)
        self._reload_bn_requested = True

    async def sync_tier2_subscriptions(self) -> None:
        async with self._sync_lock:
            settings = self.get_settings()
            max_t2 = int(settings.get("max_tier2_streams", 8))

            desired: List[str] = []
            for s in list(self._ui_pinned_symbols) + ["BTCUSDT"]:
                if s in self.states and s not in desired:
                    desired.append(s)

            for target_st in ("ACTIVE_SIGNAL", "SWEEP_DETECTED", "AT_LEVEL"):
                for sym in self._watchlist:
                    st = self.states.get(sym)
                    if st and st.scanner_state == target_st and sym not in desired:
                        desired.append(sym)
                    if len(desired) >= max_t2:
                        break
                if len(desired) >= max_t2:
                    break

            new_set = set(desired[:max_t2])
            for sym, st in self.states.items():
                st.tier2_active = sym in new_set

            if new_set == self._tier2_desired:
                return
            self._tier2_desired = new_set

            await self._apply_binance_tier2_diff()
            await self._apply_bybit_tier2_diff()

    async def _apply_binance_tier2_diff(self) -> None:
        if self._bn_ws is None:
            return
        to_add = self._tier2_desired - self._bn_tier2_active
        to_rem = self._bn_tier2_active - self._tier2_desired
        if to_add:
            params: List[str] = []
            for sym in to_add:
                s = sym.lower()
                params.extend([f"{s}@aggTrade", f"{s}@depth20@100ms", f"{s}@forceOrder"])
            for i in range(0, len(params), 15):
                self._ctrl_id += 1
                try:
                    await self._bn_ws.send(json.dumps({"method": "SUBSCRIBE", "params": params[i:i + 15], "id": self._ctrl_id}))
                except Exception:
                    return
                await asyncio.sleep(0.15)
            self._bn_tier2_active |= to_add
            for sym in to_add:
                self.prioritize_backfill(sym)
            logger.info("Binance Tier-2 SUBSCRIBED: %s", sorted(to_add))

        if to_rem:
            params = []
            for sym in to_rem:
                s = sym.lower()
                params.extend([f"{s}@aggTrade", f"{s}@depth20@100ms", f"{s}@forceOrder"])
            for i in range(0, len(params), 15):
                self._ctrl_id += 1
                try:
                    await self._bn_ws.send(json.dumps({"method": "UNSUBSCRIBE", "params": params[i:i + 15], "id": self._ctrl_id}))
                except Exception:
                    return
                await asyncio.sleep(0.15)
            self._bn_tier2_active -= to_rem
            logger.info("Binance Tier-2 UNSUBSCRIBED: %s", sorted(to_rem))

    async def _apply_bybit_tier2_diff(self) -> None:
        if self._bb_ws is None:
            return
        to_add = self._tier2_desired - self._bb_tier2_active
        to_rem = self._bb_tier2_active - self._tier2_desired
        if to_add:
            args: List[str] = []
            for sym in to_add:
                args.extend([f"publicTrade.{sym}", f"orderbook.50.{sym}", f"allLiquidation.{sym}"])
            for i in range(0, len(args), 9):
                try:
                    await self._bb_ws.send(json.dumps({"op": "subscribe", "args": args[i:i + 9]}))
                except Exception:
                    return
                await asyncio.sleep(0.15)
            self._bb_tier2_active |= to_add
            logger.info("Bybit Tier-2 SUBSCRIBED: %s", sorted(to_add))

        if to_rem:
            args = []
            for sym in to_rem:
                args.extend([f"publicTrade.{sym}", f"orderbook.50.{sym}", f"allLiquidation.{sym}"])
            for i in range(0, len(args), 9):
                try:
                    await self._bb_ws.send(json.dumps({"op": "unsubscribe", "args": args[i:i + 9]}))
                except Exception:
                    return
                await asyncio.sleep(0.15)
            self._bb_tier2_active -= to_rem
            logger.info("Bybit Tier-2 UNSUBSCRIBED: %s", sorted(to_rem))

    async def bootstrap_history(self, symbols: List[str]) -> None:
        timeout = aiohttp.ClientTimeout(total=18)
        sem = asyncio.Semaphore(4)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            try:
                async with session.get(f"{BINANCE_FAPI_REST}/fapi/v1/ticker/24hr") as resp:
                    if resp.status == 451:
                        logger.error("HTTP 451 from Binance Futures API: Restricted IP/Region!")
                        return
                    if resp.status == 200:
                        tickers = await resp.json()
                        t_map = {t["symbol"]: t for t in tickers}
                        for sym in symbols:
                            if sym in t_map and sym in self.states:
                                st = self.states[sym]
                                st.price_change_24h = float(t_map[sym].get("priceChangePercent", 0.0))
                                st.last_price = float(t_map[sym].get("lastPrice", st.last_price))
            except Exception as e:
                logger.warning("24h ticker bootstrap warning: %s", e)

            async def fetch_symbol(sym: str) -> None:
                state = self.states[sym]
                for tf in ("1d", "4h", "1h", "15m", "5m"):
                    limit = 90 if tf == "1d" else self.cfg.history_fetch_limit
                    url = f"{BINANCE_FAPI_REST}/fapi/v1/klines?symbol={sym}&interval={tf}&limit={limit}"
                    async with sem:
                        try:
                            async with session.get(url) as resp:
                                if resp.status == 200:
                                    data = await resp.json()
                                    state.load_historical_klines(tf, data)
                                elif resp.status == 451:
                                    return
                        except Exception as ex:
                            logger.warning("Kline fetch error %s %s: %s", sym, tf, ex)
                        await asyncio.sleep(0.03)

                try:
                    async with sem:
                        async with session.get(f"{BINANCE_FAPI_REST}/fapi/v1/depth?symbol={sym}&limit=500") as r_dp:
                            if r_dp.status == 200:
                                dp = await r_dp.json()
                                state.process_binance_depth(dp.get("bids", []), dp.get("asks", []), is_deep=True)
                except Exception:
                    pass

                try:
                    async with sem:
                        async with session.get(f"{BINANCE_FAPI_REST}/fapi/v1/aggTrades?symbol={sym}&limit=1000") as r_ag:
                            if r_ag.status == 200:
                                trades = await r_ag.json()
                                for tr in trades:
                                    state.process_trade(
                                        price=float(tr["p"]),
                                        qty=float(tr["q"]),
                                        is_sell_taker=bool(tr["m"]),
                                        ts_ms=int(tr["T"]),
                                        exchange="BINANCE",
                                        agg_id=int(tr["a"]),
                                    )
                                cfg_s = self.get_settings()
                                imb_r = float(cfg_s.get("imbalance_ratio", 3.0))
                                st_m = int(cfg_s.get("stacked_min_rows", 3))
                                for tf_r in ("5m", "15m", "1h"):
                                    state.ensure_metrics_fresh(tf_r, imb_r, st_m)
                except Exception:
                    pass

            await asyncio.gather(*(fetch_symbol(s) for s in symbols))
            logger.info("Bootstrapped 260-bar Day-Mode history & Liquidity Walls for %d symbols!", len(symbols))

    def _build_binance_initial_url(self) -> str:
        streams: List[str] = ["!miniTicker@arr"]
        for sym in self._watchlist:
            s = sym.lower()
            streams.extend([f"{s}@kline_5m", f"{s}@kline_15m", f"{s}@kline_1h"])
        for sym in self._tier2_desired:
            if sym in self._watchlist:
                s = sym.lower()
                streams.extend([f"{s}@aggTrade", f"{s}@depth20@100ms", f"{s}@forceOrder"])
        return f"{BINANCE_FAPI_WS}?streams={'/'.join(streams)}"

    async def _bn_ws_loop(self) -> None:
        backoff = 1.0
        while self._running:
            if not self._watchlist:
                await asyncio.sleep(2.0)
                continue
            self._reload_bn_requested = False
            url = self._build_binance_initial_url()
            try:
                logger.info("Connecting Binance Futures WS (%d Tier-1, %d Tier-2)...",
                            len(self._watchlist), len(self._tier2_desired))
                async with websockets.connect(
                    url, ping_interval=20, ping_timeout=20, max_size=4 * 1024 * 1024, close_timeout=5
                ) as ws:
                    self._bn_ws = ws
                    self._bn_tier2_active = set(self._tier2_desired)
                    backoff = 1.0
                    logger.info("Binance Futures WS Connected!")
                    while self._running and not self._reload_bn_requested:
                        try:
                            raw = await asyncio.wait_for(ws.recv(), timeout=30.0)
                        except asyncio.TimeoutError:
                            continue
                        await self._handle_binance_msg(raw)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.warning("Binance WS error: %s — reconnecting in %.1fs", e, backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2.0, 15.0)
            finally:
                self._bn_ws = None
                self._bn_tier2_active.clear()

    async def _bb_ws_loop(self) -> None:
        backoff = 1.0
        while self._running:
            if not self._watchlist:
                await asyncio.sleep(2.0)
                continue
            try:
                logger.info("Connecting Bybit V5 Linear WS...")
                async with websockets.connect(
                    BYBIT_V5_WS_LINEAR, ping_interval=None, max_size=4 * 1024 * 1024, close_timeout=5
                ) as ws:
                    self._bb_ws = ws
                    self._bb_tier2_active.clear()
                    backoff = 1.0
                    logger.info("Bybit V5 Linear WS Connected!")
                    await self._apply_bybit_tier2_diff()

                    async def ping_loop() -> None:
                        while self._running and self._bb_ws is ws:
                            await asyncio.sleep(19.0)
                            try:
                                await ws.send(json.dumps({"op": "ping"}))
                            except Exception:
                                break

                    ping_t = asyncio.create_task(ping_loop())
                    try:
                        while self._running:
                            try:
                                raw = await asyncio.wait_for(ws.recv(), timeout=30.0)
                            except asyncio.TimeoutError:
                                continue
                            await self._handle_bybit_msg(raw)
                    finally:
                        ping_t.cancel()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.warning("Bybit WS error: %s — reconnecting in %.1fs", e, backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2.0, 15.0)
            finally:
                self._bb_ws = None
                self._bb_tier2_active.clear()

    async def _handle_binance_msg(self, raw: str) -> None:
        try:
            msg = json.loads(raw)
        except json.JSONDecodeError:
            return
        if "result" in msg and "id" in msg:
            return
        stream = msg.get("stream", "")
        data = msg.get("data")
        if not stream or data is None:
            return

        if stream == "!miniTicker@arr" and isinstance(data, list):
            for t in data:
                sym = str(t.get("s", "")).upper()
                st = self.states.get(sym)
                if st is not None:
                    px = float(t.get("c", 0.0))
                    op = float(t.get("o", 0.0))
                    if px > 0:
                        st.last_price = px
                    if op > 0 and px > 0:
                        st.price_change_24h = round(((px - op) / op) * 100.0, 2)
            return

        if not isinstance(data, dict):
            return
        sym = stream.split("@")[0].upper()
        state = self.states.get(sym)
        if state is None:
            return

        settings = self.get_settings()
        imb_ratio = float(settings.get("imbalance_ratio", 3.0))
        st_min = int(settings.get("stacked_min_rows", 3))

        if "@aggTrade" in stream:
            state.process_trade(
                price=float(data["p"]),
                qty=float(data["q"]),
                is_sell_taker=bool(data["m"]),
                ts_ms=int(data["T"]),
                exchange="BINANCE",
                agg_id=int(data["a"]),
            )
            now = time.monotonic()
            if self.on_live_tick and (now - self._last_tick_notify.get(sym, 0.0) >= 0.85):
                self._last_tick_notify[sym] = now
                asyncio.create_task(self.on_live_tick(sym))

        elif "@depth20" in stream:
            bids = data.get("b") or data.get("bids") or []
            asks = data.get("a") or data.get("asks") or []
            if not state.has_deep_walls:
                state.process_binance_depth(bids, asks, is_deep=False)

        elif "@kline_" in stream:
            k = data.get("k")
            if k:
                tf = str(k.get("i", ""))
                closed_candle = state.process_kline_ws(tf, k, imb_ratio, st_min)
                if closed_candle is not None and self.on_candle_closed:
                    asyncio.create_task(self.on_candle_closed(sym, tf, closed_candle))

        elif "@forceOrder" in stream:
            o = data.get("o", {})
            if o:
                side = str(o.get("S", ""))
                px = float(o.get("ap") or o.get("p") or 0.0)
                qty = float(o.get("z") or o.get("q") or 0.0)
                ts_ms = int(o.get("T") or time.time() * 1000)
                if px > 0 and qty > 0:
                    state.process_liquidation("BINANCE", px, qty, ts_ms, side_hint=side)

    async def _handle_bybit_msg(self, raw: str) -> None:
        try:
            msg = json.loads(raw)
        except json.JSONDecodeError:
            return
        if msg.get("op") in ("pong", "subscribe", "unsubscribe"):
            if msg.get("success") is False:
                logger.debug("Bybit op warning: %s", msg)
            return

        topic = str(msg.get("topic", ""))
        data = msg.get("data")
        if not topic or data is None:
            return

        parts = topic.split(".")
        sym = parts[-1].upper()
        state = self.states.get(sym)
        if state is None:
            return

        if topic.startswith("publicTrade.") and isinstance(data, list):
            for tr in data:
                px = float(tr.get("p", 0.0))
                qty = float(tr.get("v", 0.0))
                side = str(tr.get("S", ""))
                ts_ms = int(tr.get("T", time.time() * 1000))
                tid = str(tr.get("i", ""))
                if px > 0 and qty > 0:
                    state.process_trade(
                        price=px,
                        qty=qty,
                        is_sell_taker=(side == "Sell"),
                        ts_ms=ts_ms,
                        exchange="BYBIT",
                        bybit_id=tid or None,
                    )

        elif topic.startswith("orderbook.50.") and isinstance(data, dict):
            msg_type = str(msg.get("type", "delta"))
            state.process_bybit_depth(msg_type, data)

        elif topic.startswith("allLiquidation.") and isinstance(data, list):
            for liq in data:
                px = float(liq.get("p", 0.0))
                qty = float(liq.get("v", 0.0))
                ts_ms = int(liq.get("T", time.time() * 1000))
                side = str(liq.get("S", ""))
                if px > 0 and qty > 0:
                    state.process_liquidation("BYBIT", px, qty, ts_ms, side_hint=side)

    async def _backfill_loop(self) -> None:
        await asyncio.sleep(2.0)
        timeout = aiohttp.ClientTimeout(total=12)
        target_pages = 12
        while self._running:
            try:
                sym_to_fill: Optional[str] = None
                while self._backfill_priority:
                    cand = self._backfill_priority.pop(0)
                    st = self.states.get(cand)
                    if st and st.backfilled_pages < target_pages and st.min_bn_agg_id:
                        sym_to_fill = cand
                        break
                if sym_to_fill is None:
                    for sym in list(self._tier2_desired) + list(self._watchlist):
                        st = self.states.get(sym)
                        if st and st.backfilled_pages < target_pages and st.min_bn_agg_id:
                            sym_to_fill = sym
                            break

                if sym_to_fill is None:
                    await asyncio.sleep(8.0)
                    continue

                state = self.states[sym_to_fill]
                async with aiohttp.ClientSession(timeout=timeout) as session:
                    pages_now = 0
                    while self._running and state.backfilled_pages < target_pages and pages_now < 4:
                        min_id = state.min_bn_agg_id
                        if not min_id or min_id <= 1000:
                            break
                        from_id = max(0, min_id - 1000)
                        url = f"{BINANCE_FAPI_REST}/fapi/v1/aggTrades?symbol={sym_to_fill}&fromId={from_id}&limit=1000"
                        async with session.get(url) as resp:
                            if resp.status != 200:
                                break
                            trades = await resp.json()
                            if not trades:
                                state.backfilled_pages = target_pages
                                break
                            older = [t for t in trades if int(t["a"]) < min_id]
                            if not older:
                                state.backfilled_pages = target_pages
                                break
                            cfg_s = self.get_settings()
                            state.apply_backfill_trades(
                                older,
                                imbalance_ratio=float(cfg_s.get("imbalance_ratio", 3.0)),
                                stacked_min=int(cfg_s.get("stacked_min_rows", 3)),
                            )
                            state.backfilled_pages += 1
                            pages_now += 1
                        await asyncio.sleep(0.20)

                if self.on_backfill_done:
                    await self.on_backfill_done(sym_to_fill)
                await asyncio.sleep(0.5)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.warning("Backfill loop warning: %s", e)
                await asyncio.sleep(3.0)

    async def _periodic_rest_loop(self) -> None:
        timeout = aiohttp.ClientTimeout(total=10)
        cycle = 0
        while self._running:
            try:
                await asyncio.sleep(15.0)
                cycle += 1
                await self.sync_tier2_subscriptions()

                cur_utc_day = time.strftime("%Y-%m-%d", time.gmtime())
                rollover = cur_utc_day != self._last_utc_day
                if rollover:
                    self._last_utc_day = cur_utc_day
                    logger.info("UTC Daily Rollover (%s) — updating 1D/4H Liquidity Maps!", cur_utc_day)

                async with aiohttp.ClientSession(timeout=timeout) as session:
                    for sym in list(self._tier2_desired):
                        st = self.states.get(sym)
                        if not st:
                            continue
                        try:
                            async with session.get(f"{BINANCE_FAPI_REST}/fapi/v1/depth?symbol={sym}&limit=500") as r_dp:
                                if r_dp.status == 200:
                                    dp = await r_dp.json()
                                    st.process_binance_depth(dp.get("bids", []), dp.get("asks", []), is_deep=True)
                        except Exception:
                            pass
                        try:
                            async with session.get(f"{BINANCE_FAPI_REST}/fapi/v1/openInterest?symbol={sym}") as r_oi:
                                if r_oi.status == 200:
                                    d = await r_oi.json()
                                    st.update_oi(float(d["openInterest"]), int(d["time"]), exchange="BINANCE")
                        except Exception:
                            pass
                        try:
                            url_bb_oi = f"{BYBIT_V5_REST}/v5/market/open-interest?category=linear&symbol={sym}&intervalTime=5min&limit=1"
                            async with session.get(url_bb_oi) as r_bb:
                                if r_bb.status == 200:
                                    d_bb = await r_bb.json()
                                    lst = d_bb.get("result", {}).get("list", [])
                                    if lst:
                                        st.update_oi(float(lst[0]["openInterest"]), int(lst[0]["timestamp"]), exchange="BYBIT")
                        except Exception:
                            pass
                        await asyncio.sleep(0.15)

                    if rollover or (cycle % 60 == 0):
                        for sym in list(self._watchlist):
                            st = self.states.get(sym)
                            if not st:
                                continue
                            for tf in ("4h", "1d"):
                                limit = 90 if tf == "1d" else self.cfg.history_fetch_limit
                                try:
                                    url = f"{BINANCE_FAPI_REST}/fapi/v1/klines?symbol={sym}&interval={tf}&limit={limit}"
                                    async with session.get(url) as resp:
                                        if resp.status == 200:
                                            st.load_historical_klines(tf, await resp.json())
                                except Exception:
                                    pass
                                await asyncio.sleep(0.05)

                if rollover and self.on_daily_rollover:
                    await self.on_daily_rollover(cur_utc_day)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.warning("Periodic REST loop warning: %s", e)