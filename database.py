"""Persistencia SQLite (offers.db) + lógica anti-spam."""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Iterator

from config import TZ_CHILE

SCHEMA = """
CREATE TABLE IF NOT EXISTS offers (
    id_producto         TEXT PRIMARY KEY,
    tienda              TEXT NOT NULL,
    modelo              TEXT NOT NULL,
    precio_actual       INTEGER NOT NULL,
    url                 TEXT NOT NULL,
    fecha_notificacion  TEXT,             -- última vez que se notificó (NULL = nunca)
    precio_notificado   INTEGER,          -- precio de la última notificación
    en_stock            INTEGER NOT NULL DEFAULT 1,
    fuente              TEXT,
    nombre              TEXT,
    ultima_vista        TEXT NOT NULL,
    creado              TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS notificaciones (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    id_producto TEXT NOT NULL,
    tienda      TEXT NOT NULL,
    modelo      TEXT NOT NULL,
    precio      INTEGER NOT NULL,
    motivo      TEXT NOT NULL,
    url         TEXT NOT NULL,
    fecha       TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_offers_fuente ON offers(fuente, ultima_vista);
"""


def now_iso() -> str:
    """Hora local de Chile (para fechas de notificación legibles)."""
    return datetime.now(TZ_CHILE).isoformat(timespec="seconds")


def now_utc_iso() -> str:
    """UTC con microsegundos: comparable como texto (no le afecta el cambio de horario)."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def should_notify(prev: sqlite3.Row | dict | None, price: int, in_stock: bool,
                  below_threshold: bool) -> tuple[bool, str]:
    """Regla anti-spam. Devuelve (notificar, motivo)."""
    if not below_threshold or not in_stock:
        return False, ""
    if prev is None or prev["precio_notificado"] is None:
        return True, "Nueva oferta"
    if not prev["en_stock"]:
        return True, "Volvió a tener stock"
    if price < prev["precio_notificado"]:
        return True, f"Bajó de precio (antes ${prev['precio_notificado']:,.0f})".replace(",", ".")
    return False, ""


class OffersDB:
    def __init__(self, path: str = "offers.db"):
        self.path = path
        with self._conn() as c:
            c.executescript(SCHEMA)

    @contextmanager
    def _conn(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.path, timeout=30)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def get(self, id_producto: str) -> sqlite3.Row | None:
        with self._conn() as c:
            return c.execute("SELECT * FROM offers WHERE id_producto = ?", (id_producto,)).fetchone()

    def upsert_seen(self, *, id_producto: str, tienda: str, modelo: str, precio: int,
                    url: str, fuente: str, nombre: str, seen_at: str) -> None:
        """Registra una observación (no toca los campos de notificación)."""
        with self._conn() as c:
            c.execute(
                """
                INSERT INTO offers (id_producto, tienda, modelo, precio_actual, url, en_stock,
                                    fuente, nombre, ultima_vista, creado)
                VALUES (?, ?, ?, ?, ?, 1, ?, ?, ?, ?)
                ON CONFLICT(id_producto) DO UPDATE SET
                    tienda = excluded.tienda,
                    modelo = excluded.modelo,
                    precio_actual = excluded.precio_actual,
                    url = excluded.url,
                    en_stock = 1,
                    fuente = excluded.fuente,
                    nombre = excluded.nombre,
                    ultima_vista = excluded.ultima_vista
                """,
                (id_producto, tienda, modelo, precio, url, fuente, nombre, seen_at, seen_at),
            )

    def mark_notified(self, *, id_producto: str, tienda: str, modelo: str, precio: int,
                      url: str, motivo: str) -> None:
        ts = now_iso()
        with self._conn() as c:
            c.execute(
                "UPDATE offers SET fecha_notificacion = ?, precio_notificado = ? "
                "WHERE id_producto = ?",
                (ts, precio, id_producto),
            )
            c.execute(
                "INSERT INTO notificaciones (id_producto, tienda, modelo, precio, motivo, url, fecha) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (id_producto, tienda, modelo, precio, motivo, url, ts),
            )

    def mark_unseen_out_of_stock(self, fuente: str, scan_started_at: str) -> int:
        """Lo que una fuente exitosa ya no listó en este ciclo se marca como agotado."""
        with self._conn() as c:
            cur = c.execute(
                "UPDATE offers SET en_stock = 0 WHERE fuente = ? AND ultima_vista < ? AND en_stock = 1",
                (fuente, scan_started_at),
            )
            return cur.rowcount
