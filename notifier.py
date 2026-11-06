"""Envío de alertas mediante la Bot API de Telegram (MarkdownV2)."""
from __future__ import annotations

import asyncio
import logging
import re
from datetime import datetime

import httpx

from config import HARD_PRICE_CAP_CLP, MODEL_LABELS, TZ_CHILE
from scraper import Offer, fmt_clp

log = logging.getLogger(__name__)

_MDV2_SPECIAL = re.compile(r"([_*\[\]()~`>#+\-=|{}.!\\])")


def esc(text: str) -> str:
    """Escapa texto para MarkdownV2."""
    return _MDV2_SPECIAL.sub(r"\\\1", str(text))


def esc_url(url: str) -> str:
    """Dentro de (...) de un enlace sólo hay que escapar ')' y '\\'."""
    return url.replace("\\", "\\\\").replace(")", "\\)")


def format_offer_message(offer: Offer, reason: str, detected_at: datetime | None = None) -> str:
    detected_at = detected_at or datetime.now(TZ_CHILE)
    lines = [
        f"🚨 *{esc('¡OFERTA PS5 BAJO ' + fmt_clp(HARD_PRICE_CAP_CLP) + ' CLP!')}*",
        "",
        f"🎮 *Modelo:* {esc(MODEL_LABELS.get(offer.model, offer.model))}",
        f"💰 *Precio:* {esc(fmt_clp(offer.price) + ' CLP')}",
        f"🏪 *Tienda:* {esc(offer.store)}",
        f"💳 *Medio de pago:* {esc(offer.payment_note)}",
        f"🔗 *Enlace directo:* [Ver oferta]({esc_url(offer.url)})",
        f"🕒 *Detectado:* {esc(detected_at.strftime('%d-%m-%Y %H:%M') + ' (hora de Chile)')}",
    ]
    if reason:
        lines.append(f"📌 _{esc(reason)}_")
    if offer.name:
        lines.append(f"🏷️ {esc(offer.name[:120])}")
    return "\n".join(lines)


class TelegramNotifier:
    def __init__(self, token: str, chat_id: str, dry_run: bool = False):
        self.url = f"https://api.telegram.org/bot{token}/sendMessage"
        self.chat_id = chat_id
        self.dry_run = dry_run

    async def send(self, text: str, retries: int = 3) -> bool:
        if self.dry_run:
            print("\n----- [DRY-RUN] Mensaje Telegram -----\n" + text + "\n--------------------------------------")
            return True
        payload = {
            "chat_id": self.chat_id,
            "text": text,
            "parse_mode": "MarkdownV2",
            "disable_web_page_preview": False,
        }
        async with httpx.AsyncClient(timeout=20) as client:
            for attempt in range(1, retries + 1):
                try:
                    r = await client.post(self.url, json=payload)
                    data = r.json()
                    if data.get("ok"):
                        return True
                    if r.status_code == 429:
                        wait = data.get("parameters", {}).get("retry_after", 5)
                        log.warning("Telegram rate limit; espero %ss", wait)
                        await asyncio.sleep(wait + 1)
                        continue
                    log.error("Telegram rechazó el mensaje: %s", data.get("description"))
                    if r.status_code == 400 and "parse" in str(data.get("description", "")).lower():
                        # Último recurso: texto plano sin formato
                        payload.pop("parse_mode", None)
                        payload["text"] = re.sub(r"\\(.)", r"\1", text).replace("*", "").replace("_", "")
                        continue
                    return False
                except httpx.HTTPError as exc:
                    log.warning("Error enviando a Telegram (%s/%s): %s", attempt, retries, exc)
                    await asyncio.sleep(2 ** attempt)
        return False

    async def send_offer(self, offer: Offer, reason: str) -> bool:
        return await self.send(format_offer_message(offer, reason))
