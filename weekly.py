"""Resumen semanal de precios de PS5 Slim (se envía una vez por semana aunque no haya ofertas)."""
from __future__ import annotations

import logging
import os
import sqlite3
from datetime import datetime

from config import HARD_PRICE_CAP_CLP, MODEL_DIGITAL, MODEL_STANDARD, TZ_CHILE, settings
from notifier import TelegramNotifier, esc
from scraper import Offer, fmt_clp

log = logging.getLogger(__name__)

# Domingo = 6 (lunes = 0). Hora local de Chile desde la cual se envía.
SUMMARY_WEEKDAY = int(os.getenv("WEEKLY_SUMMARY_WEEKDAY", "6"))
SUMMARY_HOUR = int(os.getenv("WEEKLY_SUMMARY_HOUR", "10"))
ENABLED = os.getenv("ENABLE_WEEKLY_SUMMARY", "true").strip().lower() in {"1", "true", "yes", "si", "sí"}

MODELS = [
    (MODEL_DIGITAL, "🎮", "Digital"),
    (MODEL_STANDARD, "💿", "Con Lector"),
]


def _meta(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path, timeout=30)
    conn.execute("CREATE TABLE IF NOT EXISTS meta (clave TEXT PRIMARY KEY, valor TEXT)")
    return conn


def _get(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute("SELECT valor FROM meta WHERE clave = ?", (key,)).fetchone()
    return row[0] if row else None


def _set(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO meta (clave, valor) VALUES (?, ?) "
        "ON CONFLICT(clave) DO UPDATE SET valor = excluded.valor",
        (key, value),
    )


def is_due(now: datetime, last_week: str | None) -> bool:
    week = now.strftime("%G-W%V")
    return ENABLED and now.weekday() == SUMMARY_WEEKDAY and now.hour >= SUMMARY_HOUR and last_week != week


def build_message(offers: list[Offer], prev_min: dict[str, int], now: datetime) -> str:
    lines = [f"📊 *{esc('Resumen semanal PS5 Slim')}*", ""]
    for model, icon, label in MODELS:
        items = sorted((o for o in offers if o.model == model and o.in_stock), key=lambda o: o.price)
        lines.append(f"{icon} *{esc(label)}*")
        if not items:
            lines.append(esc("Sin stock en las tiendas revisadas."))
            lines.append("")
            continue
        best = items[0]
        trend = ""
        if model in prev_min:
            diff = best.price - prev_min[model]
            if diff < 0:
                trend = f" ▼ bajó {fmt_clp(-diff)}"
            elif diff > 0:
                trend = f" ▲ subió {fmt_clp(diff)}"
            else:
                trend = " = sin cambio"
        lines.append(f"Mejor precio: *{esc(fmt_clp(best.price))}*{esc(trend)}")
        tope = settings.threshold_for(model)
        if best.price >= tope:
            lines.append(esc(f"Está {fmt_clp(best.price - tope)} sobre tu tope de {fmt_clp(tope)}"))
        seen: set[str] = set()
        rank = 0
        for o in items:
            if o.store in seen:
                continue
            seen.add(o.store)
            rank += 1
            lines.append(f"{rank}\\. [{esc(o.store)}]({o.url.replace(')', '%29')}) · {esc(fmt_clp(o.price))}")
            if rank == 3:
                break
        lines.append("")
    lines.append(esc(f"🎯 Tope de alerta: bajo {fmt_clp(HARD_PRICE_CAP_CLP)} CLP"))
    lines.append(esc(f"🕒 {now.strftime('%d-%m-%Y %H:%M')} (hora de Chile)"))
    return "\n".join(lines)


async def maybe_send_weekly_summary(offers: list[Offer], notifier: TelegramNotifier,
                                    ok_sources: set[str], force: bool = False) -> bool:
    """Envía el resumen si corresponde esta semana. Devuelve True si se envió."""
    if not ok_sources:
        return False  # sin datos frescos no tiene sentido enviar
    now = datetime.now(TZ_CHILE)
    conn = _meta(settings.db_path)
    try:
        if not force and not is_due(now, _get(conn, "resumen_semana")):
            return False
        prev_min = {m: int(v) for m, *_ in MODELS if (v := _get(conn, f"resumen_min_{m}"))}
        text = build_message(offers, prev_min, now)
        if not await notifier.send(text):
            log.warning("No se pudo enviar el resumen semanal; se reintentará en la próxima revisión")
            return False
        _set(conn, "resumen_semana", now.strftime("%G-W%V"))
        for model, *_ in MODELS:
            prices = [o.price for o in offers if o.model == model and o.in_stock]
            if prices:
                _set(conn, f"resumen_min_{model}", str(min(prices)))
        conn.commit()
        log.info("Resumen semanal enviado")
        return True
    finally:
        conn.close()
