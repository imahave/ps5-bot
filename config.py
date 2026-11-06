"""Configuración central del bot (lee variables desde .env)."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

# Lee la configuración desde "configuracion.txt" (visible) o, si existe, desde ".env".
_BASE = os.path.dirname(os.path.abspath(__file__))
for _name in (".env", "configuracion.txt"):
    _path = os.path.join(_BASE, _name)
    if os.path.exists(_path):
        load_dotenv(_path)
        break

# ---------------------------------------------------------------------------
# FILTRO GLOBAL INMUTABLE: ninguna alerta se envía si precio >= 600.000 CLP.
# Aunque en .env se configure un umbral mayor, se recorta a este valor.
# ---------------------------------------------------------------------------
HARD_PRICE_CAP_CLP: int = 600_000

TZ_CHILE = ZoneInfo("America/Santiago")

MODEL_DIGITAL = "DIGITAL"
MODEL_STANDARD = "STANDARD"
MODEL_LABELS = {
    MODEL_DIGITAL: "Digital",
    MODEL_STANDARD: "Con Lector (Blu-ray)",
}


def _int_env(name: str, default: int | None) -> int | None:
    raw = os.getenv(name, "").strip().replace(".", "").replace("_", "")
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _float_env(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, default))
    except (TypeError, ValueError):
        return default


def _bool_env(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "si", "sí", "on"}


def _list_env(name: str) -> list[str]:
    return [x.strip() for x in os.getenv(name, "").split(",") if x.strip()]


@dataclass(frozen=True)
class Settings:
    telegram_bot_token: str = field(default_factory=lambda: os.getenv("TELEGRAM_BOT_TOKEN", ""))
    telegram_chat_id: str = field(default_factory=lambda: os.getenv("TELEGRAM_CHAT_ID", ""))

    check_interval_minutes: int = field(default_factory=lambda: _int_env("CHECK_INTERVAL_MINUTES", 15) or 15)

    max_price_threshold: int = field(default_factory=lambda: _int_env("MAX_PRICE_THRESHOLD", HARD_PRICE_CAP_CLP))
    max_price_digital: int | None = field(default_factory=lambda: _int_env("MAX_PRICE_DIGITAL", None))
    max_price_standard: int | None = field(default_factory=lambda: _int_env("MAX_PRICE_STANDARD", None))

    # Precio mínimo "razonable" para descartar accesorios mal clasificados.
    min_sane_price: int = field(default_factory=lambda: _int_env("MIN_SANE_PRICE", 250_000) or 250_000)

    # "offer" = usa el menor precio (puede requerir tarjeta de la tienda)
    # "normal" = usa sólo el precio con cualquier medio de pago
    price_mode: str = field(default_factory=lambda: os.getenv("PRICE_MODE", "offer").strip().lower())

    db_path: str = field(default_factory=lambda: os.getenv("DB_PATH", "offers.db"))

    request_delay_min: float = field(default_factory=lambda: _float_env("REQUEST_DELAY_MIN", 2.0))
    request_delay_max: float = field(default_factory=lambda: _float_env("REQUEST_DELAY_MAX", 6.0))
    request_timeout: float = field(default_factory=lambda: _float_env("REQUEST_TIMEOUT", 25.0))

    enable_solotodo: bool = field(default_factory=lambda: _bool_env("ENABLE_SOLOTODO", True))
    enable_falabella: bool = field(default_factory=lambda: _bool_env("ENABLE_FALABELLA", True))

    # IDs de producto SoloTodo fijos (opcional), p.ej. "364198,363533"
    solotodo_product_ids: list[str] = field(default_factory=lambda: _list_env("SOLOTODO_PRODUCT_IDS"))
    # ID de categoría "Consolas" en SoloTodo (opcional, mejora la búsqueda)
    solotodo_category_id: int | None = field(default_factory=lambda: _int_env("SOLOTODO_CATEGORY_ID", None))

    send_startup_message: bool = field(default_factory=lambda: _bool_env("SEND_STARTUP_MESSAGE", True))
    log_level: str = field(default_factory=lambda: os.getenv("LOG_LEVEL", "INFO").upper())

    # ------------------------------------------------------------------
    def threshold_for(self, model: str) -> int:
        """Umbral efectivo para un modelo. Nunca supera HARD_PRICE_CAP_CLP."""
        base = min(self.max_price_threshold or HARD_PRICE_CAP_CLP, HARD_PRICE_CAP_CLP)
        specific = {
            MODEL_DIGITAL: self.max_price_digital,
            MODEL_STANDARD: self.max_price_standard,
        }.get(model)
        if specific:
            base = min(base, specific)
        return min(base, HARD_PRICE_CAP_CLP)

    def is_below_threshold(self, model: str, price: int) -> bool:
        """Regla estricta: precio < umbral (igual o mayor se descarta)."""
        return 0 < price < self.threshold_for(model) and price < HARD_PRICE_CAP_CLP

    def validate(self) -> list[str]:
        errors = []
        if not self.telegram_bot_token:
            errors.append("Falta TELEGRAM_BOT_TOKEN en .env")
        if not self.telegram_chat_id:
            errors.append("Falta TELEGRAM_CHAT_ID en .env")
        if self.price_mode not in {"offer", "normal"}:
            errors.append("PRICE_MODE debe ser 'offer' o 'normal'")
        return errors


settings = Settings()
