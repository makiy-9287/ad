import html
import logging
import re
import socket
from typing import Any, Dict, Optional, Tuple

import aiohttp

from database import Database

logger = logging.getLogger("telegram_bot")


def fmt_px(px: float) -> str:
    if px >= 1000:
        return f"{px:,.1f}"
    if px >= 10:
        return f"{px:,.3f}"
    if px >= 1:
        return f"{px:,.4f}"
    return f"{px:,.5f}"


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
        timeout = aiohttp.ClientTimeout(total=10)
        try:
            async with aiohttp.ClientSession(connector=connector, timeout=timeout) as session:
                async with session.post(url, json=payload) as resp:
                    data = await resp.json(content_type=None)
                    if resp.status == 200 and data.get("ok"):
                        return True, "Alert sent successfully!"
                    # Fallback plain-text retry if HTML entity parsing failed
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
            "🤖 <b>Orderflow &amp; Liquidity Engine — Test Alert</b>\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            "✅ Telegram Bot connection successful!\n"
            "📡 Ready to deliver A+ Binance Futures Orderflow Signals."
        )
        return await self.send_raw(msg, token_override=token, chat_id_override=chat_id, respect_enabled=False)

    async def send_signal_alert(self, sig: Dict[str, Any]) -> None:
        is_long = sig["direction"] == "LONG"
        badge = "🟢 <b>A+ LONG SIGNAL</b>" if is_long else "🔴 <b>A+ SHORT SIGNAL</b>"
        mode_tag = f"[{html.escape(sig['mode'])} · {html.escape(sig['timeframe'])}]"
        sym = html.escape(sig["symbol"])
        liq = html.escape(sig["liquidity_level"])
        reason = html.escape(sig["reason"])

        msg = (
            f"{badge}  <b>#{sym}</b>  <code>{mode_tag}</code>\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"🎯 <b>Entry Zone:</b> <code>{fmt_px(sig['entry_low'])} – {fmt_px(sig['entry_high'])}</code>\n"
            f"⚡ <b>Mid Entry:</b>  <code>{fmt_px(sig['entry_mid'])}</code>\n"
            f"🛑 <b>Stop Loss:</b>  <code>{fmt_px(sig['stop_loss'])}</code>\n"
            f"💰 <b>TP1 (50% + BE):</b> <code>{fmt_px(sig['tp1'])}</code>\n"
            f"🚀 <b>TP2 (Target):</b>   <code>{fmt_px(sig['tp2'])}</code>\n"
            f"⚖️ <b>Est. RR:</b> <b>1 : {sig['rr']:.2f}</b>  |  🔥 <b>Score:</b> <b>{sig['confidence']}/100</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"📍 <b>Swept Level:</b> {liq}\n"
            f"📊 <b>Orderflow:</b> {reason}\n\n"
            f"<i>Note: Execute on Binance Futures ({sym}). Wait for entry zone fill &amp; lock SL/TP immediately.</i>"
        )
        ok, err = await self.send_raw(msg)
        if not ok:
            logger.warning("Signal Telegram alert failed (%s): %s", sym, err)

    async def send_trade_update(self, sig: Dict[str, Any], event: str, current_px: float) -> None:
        sym = html.escape(sig["symbol"])
        direction = html.escape(sig["direction"])
        mode = html.escape(sig["mode"])
        if event == "TP1_BE":
            header = f"💰 <b>TP1 HIT — MOVE SL TO BREAKEVEN!</b>"
            detail = f"50% Partial secured (+0.75R). Move SL to <code>{fmt_px(sig['entry_mid'])}</code> &amp; hold for TP2 <code>{fmt_px(sig['tp2'])}</code>."
        elif event == "TP2_HIT":
            header = f"🚀 <b>TP2 TARGET HIT! (+{sig.get('pnl_r', sig['rr']):.2f}R)</b>"
            detail = f"Full target reached at <code>{fmt_px(current_px)}</code>! Setup completed."
        elif event == "SL_HIT":
            header = f"🛑 <b>STOP LOSS HIT (-1.00R)</b>"
            detail = f"Closed at <code>{fmt_px(current_px)}</code>. Capital protected — waiting for next clean setup."
        elif event == "BE_EXIT":
            header = f"🛡️ <b>BREAKEVEN CLOSED (+0.75R)</b>"
            detail = f"Remaining 50% closed at Breakeven <code>{fmt_px(sig['entry_mid'])}</code> after TP1 partial."
        else:
            return

        msg = (
            f"{header}\n"
            f"<b>#{sym}</b> ({direction} · {mode})\n"
            f"{detail}"
        )
        await self.send_raw(msg)