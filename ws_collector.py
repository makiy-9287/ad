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
    ):
        self.cfg = cfg
        self.get_settings = get_settings_fn
        self.on_candle_closed = on_candle_closed
        self.on_live_tick = on_live_tick

        self.states: Dict[str, SymbolOrderflowState] = {}
        self._watchlist: List[str] = []
        self._running = False
        self._reload_requested = False
        self._ws_task: Optional[asyncio.Task] = None
        self._oi_task: Optional[asyncio.Task] = None
        self._last_tick_notify: Dict[str, float] = {}

    def get_state(self, symbol: str) -> Optional[SymbolOrderflowState]:
        return self.states.get(symbol.upper())

    async def start(self, watchlist: List[str]) -> None:
        self._running = True
        self._watchlist = [s.upper().strip() for s in watchlist if s.strip()]
        for sym in self._watchlist:
            if sym not in self.states:
                self.states[sym] = SymbolOrderflowState(sym, max_candles=self.cfg.max_candles_per_tf)

        await self.bootstrap_history(self._watchlist)
        self._ws_task = asyncio.create_task(self._ws_loop(), name="binance_ws_loop")
        self._oi_task = asyncio.create_task(self._oi_poll_loop(), name="binance_oi_loop")

    async def stop(self) -> None:
        self._running = False
        for task in (self._ws_task, self._oi_task):
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
            # Fetch 24h ticker stats once for all symbols
            try:
                async with session.get(f"{BINANCE_FAPI_REST}/fapi/v1/ticker/24hr") as resp:
                    if resp.status == 451:
                        logger.error(
                            "HTTP 451 from Binance Futures API: Restricted IP/Region! "
                            "Run on Sri Lanka local network or non-US VPS (SG/DE/FI)."
                        )
                        return
                    if resp.status == 200:
                        tickers = await resp.json()
                        t_map = {t["symbol"]: t for t in tickers}
                        for sym in symbols:
                            if sym in t_map and sym in self.states:
                                self.states[sym].price_change_24h = float(t_map[sym].get("priceChangePercent", 0.0))
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
                                    logger.error("HTTP 451 blocked fetching %s %s", sym, tf)
                                    return
                                else:
                                    logger.warning("Kline fetch %s %s status %s", sym, tf, resp.status)
                        except Exception as ex:
                            logger.warning("Kline fetch error %s %s: %s", sym, tf, ex)
                        await asyncio.sleep(0.04)

                # Seed latest ~1000 aggTrades for immediate Footprint data on live candles
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
                                        imbalance_ratio=imb_ratio,
                                        stacked_min=st_min,
                                    )
                except Exception as ex:
                    logger.warning("aggTrades seed error %s: %s", sym, ex)

            await asyncio.gather(*(fetch_symbol(s) for s in symbols))
            logger.info("Bootstrapped historical data for %d symbols", len(symbols))

    def _build_stream_url(self) -> str:
        streams: List[str] = []
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
                backoff = min(backoff * 2.0, 20.0)

    async def _handle_message(self, raw: str) -> None:
        try:
            msg = json.loads(raw)
        except json.JSONDecodeError:
            return
        stream = msg.get("stream", "")
        data = msg.get("data", {})
        if not stream or not data:
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
                imbalance_ratio=imb_ratio,
                stacked_min=st_min,
            )
            now = time.monotonic()
            if self.on_live_tick and (now - self._last_tick_notify.get(sym, 0.0) >= 0.45):
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
        while self._running:
            try:
                async with aiohttp.ClientSession(timeout=timeout) as session:
                    for sym in list(self._watchlist):
                        if not self._running:
                            break
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

                    # Refresh 24h % change
                    try:
                        async with session.get(f"{BINANCE_FAPI_REST}/fapi/v1/ticker/24hr") as resp:
                            if resp.status == 200:
                                tickers = await resp.json()
                                t_map = {t["symbol"]: t for t in tickers}
                                for sym in list(self._watchlist):
                                    if sym in t_map and sym in self.states:
                                        self.states[sym].price_change_24h = float(
                                            t_map[sym].get("priceChangePercent", 0.0)
                                        )
                    except Exception:
                        pass
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.warning("OI poll loop warning: %s", e)

            await asyncio.sleep(30.0)