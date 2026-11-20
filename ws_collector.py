import asyncio
import json
import logging
import time
from typing import Awaitable, Callable, Dict, List, Optional

import aiohttp
import websockets

from config import BINANCE_FAPI_REST, BINANCE_FAPI_WS, EngineConfig
from footprint_engine import FootprintCandle, SymbolOrderflowState

logger = logging.getLogger("ws_collector")


class BinanceFuturesCollector:
    def __init__(
        self,
        cfg: EngineConfig,
        get_settings_fn: Callable[[], Dict],
        on_candle_closed: Optional[Callable[[str, str, FootprintCandle], Awaitable[None]]] = None,
        on_live_tick: Optional[Callable[[str], Awaitable[None]]] = None,
        on_backfill_done: Optional[Callable[[str], Awaitable[None]]] = None,
    ):
        self.cfg = cfg
        self.get_settings = get_settings_fn
        self.on_candle_closed = on_candle_closed
        self.on_live_tick = on_live_tick
        self.on_backfill_done = on_backfill_done

        self.states: Dict[str, SymbolOrderflowState] = {}
        self._watchlist: List[str] = []
        self._running = False
        self._reload_requested = False
        self._ws_task: Optional[asyncio.Task] = None
        self._oi_task: Optional[asyncio.Task] = None
        self._backfill_task: Optional[asyncio.Task] = None
        self._last_tick_notify: Dict[str, float] = {}
        self._backfill_priority: List[str] = []

    def get_state(self, symbol: str) -> Optional[SymbolOrderflowState]:
        return self.states.get(symbol.upper())

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
        self._ws_task = asyncio.create_task(self._ws_loop(), name="binance_ws_loop")
        self._oi_task = asyncio.create_task(self._oi_poll_loop(), name="binance_oi_loop")
        self._backfill_task = asyncio.create_task(self._backfill_loop(), name="binance_backfill_loop")

    async def stop(self) -> None:
        self._running = False
        for task in (self._ws_task, self._oi_task, self._backfill_task):
            if task and not task.done():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass

    async def reload_watchlist(self, new_watchlist: List[str]) -> None:
        cleaned = [s.upper().strip() for s in new_watchlist if s.strip()]
        if cleaned == self._watchlist:
            return
        logger.info("Reloading watchlist: %s", cleaned)
        added = [s for s in cleaned if s not in self.states]
        for sym in added:
            self.states[sym] = SymbolOrderflowState(sym, max_candles=self.cfg.max_candles_per_tf)
        self._watchlist = cleaned
        if added:
            await self.bootstrap_history(added)
        self._reload_requested = True

    async def bootstrap_history(self, symbols: List[str]) -> None:
        timeout = aiohttp.ClientTimeout(total=15)
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
                for tf in ("4h", "1h", "15m", "5m", "1m"):
                    limit = 80 if tf == "4h" else self.cfg.history_fetch_limit
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

                # Seed latest 1000 aggTrades + initial 500-level deep orderbook walls
                try:
                    url_agg = f"{BINANCE_FAPI_REST}/fapi/v1/aggTrades?symbol={sym}&limit=1000"
                    async with sem:
                        async with session.get(url_agg) as resp:
                            if resp.status == 200:
                                trades = await resp.json()
                                cfg_settings = self.get_settings()
                                imb_ratio = float(cfg_settings.get("imbalance_ratio", 3.0))
                                st_min = int(cfg_settings.get("stacked_min_rows", 3))
                                for tr in trades:
                                    state.process_agg_trade(
                                        price=float(tr["p"]),
                                        qty=float(tr["q"]),
                                        is_buyer_maker=bool(tr["m"]),
                                        ts_ms=int(tr["T"]),
                                        agg_id=int(tr["a"]),
                                        imbalance_ratio=imb_ratio,
                                        stacked_min=st_min,
                                        recompute=False,
                                    )
                                for tf_r in ("1m", "5m", "15m", "1h"):
                                    state.ensure_metrics_fresh(tf_r, imb_ratio, st_min)
                except Exception as ex:
                    logger.warning("aggTrades seed error %s: %s", sym, ex)

                try:
                    url_dp = f"{BINANCE_FAPI_REST}/fapi/v1/depth?symbol={sym}&limit=500"
                    async with sem:
                        async with session.get(url_dp) as resp:
                            if resp.status == 200:
                                dp = await resp.json()
                                state.process_deep_orderbook(dp.get("bids", []), dp.get("asks", []))
                except Exception:
                    pass

            await asyncio.gather(*(fetch_symbol(s) for s in symbols))
            logger.info("Bootstrapped historical data for %d symbols", len(symbols))

    async def _backfill_loop(self) -> None:
        """Progressively backfills past 8-12 footprint candles (zero duplicates via fromId) for every coin."""
        await asyncio.sleep(1.5)
        timeout = aiohttp.ClientTimeout(total=12)
        target_pages = 8
        while self._running:
            try:
                # Pick prioritized symbol or next un-backfilled symbol
                sym_to_fill: Optional[str] = None
                while self._backfill_priority:
                    cand = self._backfill_priority.pop(0)
                    st = self.states.get(cand)
                    if st and st.backfilled_pages < target_pages and st.min_seen_agg_id:
                        sym_to_fill = cand
                        break
                if sym_to_fill is None:
                    for sym in list(self._watchlist):
                        st = self.states.get(sym)
                        if st and st.backfilled_pages < target_pages and st.min_seen_agg_id:
                            sym_to_fill = sym
                            break

                if sym_to_fill is None:
                    await asyncio.sleep(10.0)
                    continue

                state = self.states[sym_to_fill]
                async with aiohttp.ClientSession(timeout=timeout) as session:
                    pages_now = 0
                    while self._running and state.backfilled_pages < target_pages and pages_now < 4:
                        min_id = state.min_seen_agg_id
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
                            # Keep strictly older than min_id
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
                        await asyncio.sleep(0.18)

                if self.on_backfill_done:
                    await self.on_backfill_done(sym_to_fill)
                await asyncio.sleep(0.4)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.warning("Backfill loop warning: %s", e)
                await asyncio.sleep(3.0)

    def _build_stream_url(self) -> str:
        streams: List[str] = ["!miniTicker@arr"]
        for sym in self._watchlist:
            s = sym.lower()
            streams.extend([
                f"{s}@aggTrade",
                f"{s}@depth20@100ms",
                f"{s}@kline_1m",
                f"{s}@kline_5m",
                f"{s}@kline_15m",
                f"{s}@kline_1h",
                f"{s}@forceOrder",
            ])
        return f"{BINANCE_FAPI_WS}?streams={'/'.join(streams)}"

    async def _ws_loop(self) -> None:
        backoff = 1.0
        while self._running:
            if not self._watchlist:
                await asyncio.sleep(2.0)
                continue
            self._reload_requested = False
            url = self._build_stream_url()
            try:
                logger.info("Connecting Binance Futures WS (%d coins)...", len(self._watchlist))
                async with websockets.connect(
                    url,
                    ping_interval=20,
                    ping_timeout=20,
                    max_size=4 * 1024 * 1024,
                    close_timeout=5,
                ) as ws:
                    backoff = 1.0
                    logger.info("Binance Futures WS Connected!")
                    while self._running and not self._reload_requested:
                        try:
                            raw = await asyncio.wait_for(ws.recv(), timeout=30.0)
                        except asyncio.TimeoutError:
                            continue
                        await self._handle_message(raw)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.warning("Binance WS error: %s — reconnecting in %.1fs", e, backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2.0, 15.0)

    async def _handle_message(self, raw: str) -> None:
        try:
            msg = json.loads(raw)
        except json.JSONDecodeError:
            return
        stream = msg.get("stream", "")
        data = msg.get("data")
        if not stream or data is None:
            return

        # 1-second live price & 24h% ticker array for all watchlist symbols
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
            state.process_agg_trade(
                price=float(data["p"]),
                qty=float(data["q"]),
                is_buyer_maker=bool(data["m"]),
                ts_ms=int(data["T"]),
                agg_id=int(data["a"]),
                imbalance_ratio=imb_ratio,
                stacked_min=st_min,
                recompute=False,
            )
            now = time.monotonic()
            if self.on_live_tick and (now - self._last_tick_notify.get(sym, 0.0) >= 0.85):
                self._last_tick_notify[sym] = now
                asyncio.create_task(self.on_live_tick(sym))

        elif "@depth20" in stream:
            bids = data.get("b") or data.get("bids") or []
            asks = data.get("a") or data.get("asks") or []
            ts_ms = int(data.get("E") or time.time() * 1000)
            state.process_depth(bids, asks, ts_ms)

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
                    state.process_liquidation(side, px, qty, ts_ms)

    async def _oi_poll_loop(self) -> None:
        timeout = aiohttp.ClientTimeout(total=10)
        cycle = 0
        while self._running:
            try:
                cycle += 1
                async with aiohttp.ClientSession(timeout=timeout) as session:
                    for sym in list(self._watchlist):
                        if not self._running:
                            break
                        # Refresh 500-level institutional walls every 15s (cycle % 2 == 1)
                        if cycle % 2 == 1:
                            try:
                                async with session.get(f"{BINANCE_FAPI_REST}/fapi/v1/depth?symbol={sym}&limit=500") as r_dp:
                                    if r_dp.status == 200:
                                        dp = await r_dp.json()
                                        st = self.states.get(sym)
                                        if st:
                                            st.process_deep_orderbook(dp.get("bids", []), dp.get("asks", []))
                            except Exception:
                                pass

                        url = f"{BINANCE_FAPI_REST}/fapi/v1/openInterest?symbol={sym}"
                        try:
                            async with session.get(url) as resp:
                                if resp.status == 200:
                                    d = await resp.json()
                                    st = self.states.get(sym)
                                    if st:
                                        st.update_oi(float(d["openInterest"]), int(d["time"]))
                        except Exception:
                            pass
                        await asyncio.sleep(0.2)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.warning("OI/Depth poll loop warning: %s", e)

            await asyncio.sleep(15.0)
