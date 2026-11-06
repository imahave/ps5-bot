"""Bot de alertas de PS5 Slim bajo $600.000 CLP — punto de entrada y scheduler."""
from __future__ import annotations

import argparse
import asyncio
import logging
import signal
import sys
from datetime import datetime

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from config import HARD_PRICE_CAP_CLP, MODEL_LABELS, TZ_CHILE, settings
from database import OffersDB, now_utc_iso, should_notify
from notifier import TelegramNotifier, esc
from scraper import Offer, collect_offers, fmt_clp

log = logging.getLogger("ps5bot")


async def run_check(db: OffersDB, notifier: TelegramNotifier) -> None:
    scan_started = now_utc_iso()
    log.info("=== Iniciando revisión de precios ===")
    offers, ok_sources = await collect_offers(settings)

    sent = 0
    for o in sorted(offers, key=lambda x: x.price):
        below = settings.is_below_threshold(o.model, o.price)
        # Doble candado: el filtro global nunca se salta
        if o.price >= HARD_PRICE_CAP_CLP:
            below = False

        prev = db.get(o.product_id)
        notify, reason = should_notify(prev, o.price, o.in_stock, below)

        db.upsert_seen(
            id_producto=o.product_id, tienda=o.store, modelo=o.model, precio=o.price,
            url=o.url, fuente=o.source, nombre=o.name, seen_at=now_utc_iso(),
        )

        log.info("%-9s %-24s %-11s %s %s", o.source, o.store[:24], o.model,
                 fmt_clp(o.price), "→ ALERTA" if notify else "")
        if notify and await notifier.send_offer(o, reason):
            db.mark_notified(id_producto=o.product_id, tienda=o.store, modelo=o.model,
                             precio=o.price, url=o.url, motivo=reason)
            sent += 1
            await asyncio.sleep(1.5)  # no saturar Telegram

    for src in ok_sources:
        n = db.mark_unseen_out_of_stock(src, scan_started)
        if n:
            log.info("%s: %s ofertas marcadas como agotadas", src, n)

    log.info("=== Revisión terminada: %s ofertas vistas, %s alertas, fuentes OK: %s ===",
             len(offers), sent, ", ".join(sorted(ok_sources)) or "ninguna")


def startup_text() -> str:
    th = {m: fmt_clp(settings.threshold_for(m)) for m in MODEL_LABELS}
    return (
        "🤖 *Bot PS5 iniciado*\n"
        + esc(f"Revisando cada {settings.check_interval_minutes} min.") + "\n"
        + esc(f"Umbral Digital: < {th['DIGITAL']} · Con Lector: < {th['STANDARD']}") + "\n"
        + esc(f"Modo de precio: {settings.price_mode}")
    )


async def main_async(args: argparse.Namespace) -> None:
    errors = [] if args.dry_run else settings.validate()
    if errors:
        for e in errors:
            log.error(e)
        sys.exit(1)

    db = OffersDB(settings.db_path)
    notifier = TelegramNotifier(settings.telegram_bot_token, settings.telegram_chat_id,
                                dry_run=args.dry_run)

    if args.test_notify:
        demo = Offer(
            product_id="demo", store="Tienda de prueba", model="DIGITAL", price=499_990,
            normal_price=529_990, offer_price=499_990,
            payment_note="Precio oferta con tarjeta de la tienda. Precio normal: $529.990",
            url="https://www.solotodo.cl", name="Mensaje de prueba", source="test",
        )
        ok = await notifier.send_offer(demo, "Mensaje de prueba")
        print("✅ Mensaje enviado" if ok else "❌ Falló el envío (revisa token / chat id)")
        return

    if args.once:
        await run_check(db, notifier)
        return

    if settings.send_startup_message:
        await notifier.send(startup_text())

    scheduler = AsyncIOScheduler(timezone=TZ_CHILE)
    scheduler.add_job(
        run_check, "interval", args=[db, notifier],
        minutes=settings.check_interval_minutes,
        next_run_time=datetime.now(TZ_CHILE),   # primera ejecución inmediata
        jitter=60,                               # +/- 60 s aleatorio entre ciclos
        max_instances=1, coalesce=True, misfire_grace_time=300,
        id="ps5_check",
    )
    scheduler.start()
    log.info("Scheduler activo: cada %s min. Ctrl+C para salir.", settings.check_interval_minutes)

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:  # Windows
            pass
    try:
        await stop.wait()
    finally:
        scheduler.shutdown(wait=False)
        log.info("Bot detenido.")


def main() -> None:
    parser = argparse.ArgumentParser(description="Bot de alertas PS5 Slim < $600.000 CLP")
    parser.add_argument("--once", action="store_true", help="Ejecuta una sola revisión y sale")
    parser.add_argument("--dry-run", action="store_true",
                        help="No envía a Telegram; imprime los mensajes en consola")
    parser.add_argument("--test-notify", action="store_true",
                        help="Envía un mensaje de prueba a Telegram y sale")
    args = parser.parse_args()

    logging.basicConfig(
        level=getattr(logging, settings.log_level, logging.INFO),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("apscheduler").setLevel(logging.WARNING)

    try:
        asyncio.run(main_async(args))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
