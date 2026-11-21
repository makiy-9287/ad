import html
import logging
import re
import socket
from typing import Any, Dict, Optional, Tuple

import aiohttp

from config import format_price
from database import Database

logger = logging.getLogger("telegram_bot")


class TelegramNotifier:
    def __init__(self, db: Database):
        self.db = db

    async def send_raw(
        self,
        text: str,
        token_override: Optional[str] = None,
        chat_id_override: Optional[str] = None,
        respect_enabled: bool = True,
    ) -> Tuple[bool, str]:
        settings = self.db.get_all_settings()
        if respect_enabled and not settings.get("telegram_enabled", True):
            return False, "Telegram alerts disabled in Settings"

        token = (token_override if token_override is not None else settings.get("telegram_bot_token", "")).strip()
        chat_id = (chat_id_override if chat_id_override is not None else settings.get("telegram_chat_id", "")).strip()
        if not token or not chat_id:
            return False, "Missing Bot Token or Chat ID"

        url = f"https://api.telegram.org/bot{token}/sendMessage"
        payload = {
            "chat_id": chat_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }

        connector = aiohttp.TCPConnector(family=socket.AF_INET)
        timeout = aiohttp.ClientTimeout(total=12)
        try:
            async with aiohttp.ClientSession(connector=connector, timeout=timeout) as session:
                async with session.post(url, json=payload) as resp:
                    data = await resp.json(content_type=None)
                    if resp.status == 200 and data.get("ok"):
                        return True, "Alert sent successfully!"
                    if resp.status == 400 and "parse entities" in str(data.get("description", "")).lower():
                        plain = re.sub(r"<[^>]+>", "", text)
                        payload_plain = {"chat_id": chat_id, "text": plain, "disable_web_page_preview": True}
                        async with session.post(url, json=payload_plain) as r2:
                            d2 = await r2.json(content_type=None)
                            if r2.status == 200 and d2.get("ok"):
                                return True, "Alert sent (plain text fallback)"
                            return False, f"Telegram error {r2.status}: {d2.get('description', 'Unknown')}"
                    return False, f"Telegram error {resp.status}: {data.get('description', 'Unknown')}"
        except Exception as e:
            logger.warning("Telegram send failed: %s", e)
            return False, f"Connection error: {e}"

    async def send_test_alert(self, token: Optional[str] = None, chat_id: Optional[str] = None) -> Tuple[bool, str]:
        msg = (
            "🤖 <b>OrderFlowX V3.1 Ultra — Test Alert</b>\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            "✅ Telegram Bot connection successful!\n"
            "⚡ Dual-Exchange: <b>Binance Futures + Bybit Linear</b>\n"
            "🎯 Mode: <b>DAY MODE ONLY</b> (1D/4H/1H Bias · 15m/5m Sniper)"
        )
        return await self.send_raw(msg, token_override=token, chat_id_override=chat_id, respect_enabled=False)

    def format_signal_message(self, sig: Dict[str, Any]) -> str:
        is_long = sig["direction"] == "LONG"
        badge = "🟢 <b>A+ DAY LONG</b>" if is_long else "🔴 <b>A+ DAY SHORT</b>"
        entry_type = str(sig.get("entry_type", "LIMIT")).upper()
        exec_tag = "⚡ <b>CMP (MARKET NOW)</b>" if entry_type == "CMP" else "⏳ <b>LIMIT ORDER</b>"
        sym = html.escape(str(sig["symbol"]))
        tf_tag = html.escape(str(sig.get("timeframe", "15m->5m")))
        bias = html.escape(str(sig.get("htf_bias", "")))
        liq = html.escape(str(sig.get("liquidity_level", "")))
        tp_name = html.escape(str(sig.get("tp_target_name", "Opposing Liquidity")))
        reason = html.escape(str(sig.get("reason", "")))

        e_mid = float(sig["entry_mid"])
        sl = float(sig["stop_loss"])
        sl_pct = abs(e_mid - sl) / max(e_mid, 1e-9) * 100.0

        return (
            f"{badge}  <b>#{sym}</b>  <code>[{tf_tag}]</code>\n"
            f"{exec_tag}\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"🧭 <b>HTF Bias:</b> <code>{bias}</code>\n"
            f"🎯 <b>Entry Price:</b> <code>{format_price(e_mid)}</code>\n"
            f"📦 <b>Entry Zone:</b>  <code>{format_price(sig['entry_low'])} – {format_price(sig['entry_high'])}</code>\n"
            f"🛑 <b>Stop Loss:</b>   <code>{format_price(sl)}</code> (<i>{sl_pct:.2f}%</i>)\n"
            f"💰 <b>TP1 (50% + BE):</b> <code>{format_price(sig['tp1'])}</code> (<i>1.5R</i>)\n"
            f"🚀 <b>TP2 (Target):</b>   <code>{format_price(sig['tp2'])}</code> (<i>{tp_name}</i>)\n"
            f"⚖️ <b>Est. RR:</b> <b>1 : {sig['rr']:.2f}</b>  |  🔥 <b>Score:</b> <b>{sig['confidence']}/100</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"📍 <b>Swept Level:</b> {liq}\n"
            f"📊 <b>Orderflow:</b> {reason}\n\n"
            f"<i>Execute on Binance Futures ({sym}). Lock SL &amp; TP1/TP2 immediately.</i>"
        )

    async def send_signal_alert(self, sig: Dict[str, Any]) -> None:
        msg = self.format_signal_message(sig)
        ok, err = await self.send_raw(msg)
        if not ok:
            logger.warning("Signal Telegram alert failed (%s): %s", sig.get("symbol"), err)

    def format_trade_update(self, sig: Dict[str, Any], event: str, current_px: float) -> Optional[str]:
        sym = html.escape(str(sig["symbol"]))
        direction = html.escape(str(sig["direction"]))
        entry_type = html.escape(str(sig.get("entry_type", "LIMIT")))

        if event == "FILLED":
            header = "⚡ <b>LIMIT ORDER FILLED — TRADE ACTIVE!</b>"
            detail = (
                f"Entry filled around <code>{format_price(current_px)}</code>.\n"
                f"🛑 SL: <code>{format_price(sig['stop_loss'])}</code>  |  💰 TP1: <code>{format_price(sig['tp1'])}</code>  |  🚀 TP2: <code>{format_price(sig['tp2'])}</code>"
            )
        elif event == "TP1_BE":
            header = "💰 <b>TP1 HIT (+0.75R) — MOVE SL TO BREAKEVEN!</b>"
            detail = (
                f"50% Partial secured @ <code>{format_price(current_px)}</code>.\n"
                f"🛡️ Move SL to Entry <code>{format_price(sig['entry_mid'])}</code> &amp; let remaining 50% run to TP2 <code>{format_price(sig['tp2'])}</code>!"
            )
        elif event == "TP2_HIT":
            pnl_r = float(sig.get("pnl_r", sig["rr"]))
            header = f"🚀 <b>FULL TP2 TARGET HIT! (+{pnl_r:.2f}R)</b>"
            detail = (
                f"Target reached @ <code>{format_price(current_px)}</code>!\n"
                f"🔄 <b>#{sym}</b> automatically reset to <b>WATCHING</b>."
            )
        elif event == "BE_EXIT":
            header = "🛡️ <b>BREAKEVEN CLOSED AFTER TP1 (+0.75R)</b>"
            detail = (
                f"Remaining 50% exited at Breakeven <code>{format_price(sig['entry_mid'])}</code>.\n"
                f"🔄 <b>#{sym}</b> automatically reset to <b>WATCHING</b>."
            )
        elif event == "SL_HIT":
            header = "🛑 <b>STOP LOSS HIT (-1.00R)</b>"
            detail = (
                f"Stopped out @ <code>{format_price(current_px)}</code>. Capital protected.\n"
                f"🔄 <b>#{sym}</b> automatically reset to <b>WATCHING</b>."
            )
        else:
            return None

        return f"{header}\n<b>#{sym}</b> ({direction} · {entry_type})\n{detail}"

    async def send_trade_update(self, sig: Dict[str, Any], event: str, current_px: float) -> None:
        msg = self.format_trade_update(sig, event, current_px)
        if not msg:
            return
        ok, err = await self.send_raw(msg)
        if not ok:
            logger.warning("Trade update alert failed (%s %s): %s", sig.get("symbol"), event, err)

    async def send_daily_summary(self, utc_day: str, stats: Dict[str, Any], symbols_count: int) -> None:
        msg = (
            f"📅 <b>DAILY UTC ROLLOVER ({html.escape(utc_day)})</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"🔄 Refreshed <b>1D / 4H / 1H</b> Liquidity Maps for <b>{symbols_count}</b> coins.\n"
            f"📊 <b>Cumulative Performance:</b>\n"
            f"• Closed Trades: <b>{stats.get('total_closed', 0)}</b> (W:{stats.get('wins', 0)} · BE:{stats.get('be_exits', 0)} · L:{stats.get('losses', 0)})\n"
            f"• Win Rate: <b>{stats.get('win_rate', 0.0)}%</b>\n"
            f"• Net PnL: <b>{stats.get('net_r', 0.0):+.2f}R</b>\n"
            f"• Active Open: <b>{stats.get('active_count', 0)}</b>"
        )
        await self.send_raw(msg)