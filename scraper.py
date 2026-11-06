"""Extracción de precios de PS5 Slim desde SoloTodo (API pública) y Falabella (respaldo)."""
from __future__ import annotations

import asyncio
import json
import logging
import random
import re
import unicodedata
from dataclasses import dataclass
from typing import Any, Iterable

import httpx

from config import MODEL_DIGITAL, MODEL_STANDARD, Settings

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Modelo de datos
# ---------------------------------------------------------------------------


@dataclass
class Offer:
    product_id: str          # ID canónico (permite deduplicar entre fuentes)
    store: str               # Nombre de la tienda
    model: str               # DIGITAL | STANDARD
    price: int               # Precio usado para el filtro (según PRICE_MODE)
    normal_price: int        # Precio con cualquier medio de pago
    offer_price: int         # Menor precio (puede requerir tarjeta de la tienda)
    payment_note: str        # Texto sobre el medio de pago
    url: str
    name: str
    source: str              # "solotodo" | "falabella"
    in_stock: bool = True


# ---------------------------------------------------------------------------
# Utilidades
# ---------------------------------------------------------------------------

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/130.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/130.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:132.0) Gecko/20100101 Firefox/132.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_6) AppleWebKit/605.1.15 (KHTML, like Gecko) "
    "Version/18.0 Safari/605.1.15",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/130.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/130.0.0.0 Safari/537.36 Edg/130.0.0.0",
]


def normalize(text: str) -> str:
    text = unicodedata.normalize("NFKD", text or "")
    text = "".join(c for c in text if not unicodedata.combining(c))
    return re.sub(r"\s+", " ", text.lower()).strip()


def parse_clp(value: Any) -> int:
    """'$ 549.990' | '549990.00' | 549990 | ['549.990'] -> 549990."""
    if value is None:
        return 0
    if isinstance(value, (list, tuple)):
        return parse_clp(value[0]) if value else 0
    if isinstance(value, (int, float)):
        return int(round(value))
    s = str(value).strip()
    s = s.split("-")[0]  # rangos "549.990 - 599.990" -> primer valor
    # "549990.00" (decimal con punto) vs "549.990" (miles con punto)
    if re.fullmatch(r"\s*\d+\.\d{1,2}\s*", s):
        return int(round(float(s)))
    digits = re.sub(r"[^\d]", "", s)
    return int(digits) if digits else 0


def fmt_clp(value: int) -> str:
    return f"${value:,.0f}".replace(",", ".")


# ---------------------------------------------------------------------------
# Clasificación de modelo
# ---------------------------------------------------------------------------

_PS5_RE = re.compile(r"\b(ps5|playstation ?5)\b")
_SLIM_RE = re.compile(r"\bslim\b|cfi-?20\d\d")

_EXCLUDE_ALWAYS = re.compile(
    r"\bpro\b|portal|\bvr2?\b|psvr|reacondicionad|refurbish|\busad[oa]\b|seminuev|"
    r"open ?box|caja abierta|\bps4\b|\bps3\b|replica|\bskin\b|sticker|vinilo"
)
# Accesorios típicos cuando el nombre EMPIEZA por ellos (p.ej. "Lector de discos para PS5")
_ACCESSORY_PREFIX = re.compile(
    r"^(sony )?(lector|unidad|disc drive|drive|control|mando|dualsense|joystick|"
    r"funda|carcasa|cover|tapa|faceplate|soporte|base|cargador|estacion|audifono|"
    r"auricular|headset|cable|juego|ventilador|cooler|disco duro|ssd|bolso|maleta)\b"
)

_DIGITAL_STRONG = re.compile(
    r"all ?digital|digital edition|edicion digital|version digital|sin lector|sin disco|"
    r"\bdigital\b"
)
_STANDARD_STRONG = re.compile(
    r"blu-?ray|blu ray|\blector|lectora|con disco|disc edition|\bdisc\b|\bdisco\b|"
    r"standard|estandar|edicion fisica|fisica"
)


def classify_model(name: str) -> str | None:
    """Devuelve DIGITAL / STANDARD si el nombre corresponde a una consola PS5 Slim, o None."""
    n = normalize(name)
    if not _PS5_RE.search(n) or not _SLIM_RE.search(n):
        return None
    if _EXCLUDE_ALWAYS.search(n) or _ACCESSORY_PREFIX.search(n):
        return None
    # Ignora lo que viene después del primer '+' (juegos/accesorios del bundle,
    # p.ej. "+ FC 26 Digital" no debe convertir la consola en Digital).
    core = n.split("+")[0]
    if _DIGITAL_STRONG.search(core):
        # "PS5 Slim Digital + lector de discos" -> el core sigue siendo Digital
        return MODEL_DIGITAL
    if _STANDARD_STRONG.search(core):
        return MODEL_STANDARD
    if re.search(r"\b1 ?tb\b", core):
        # La PS5 Slim de 1TB sin mención "digital" suele ser la versión con lector
        return MODEL_STANDARD
    return None


# ---------------------------------------------------------------------------
# Cliente HTTP "educado": UA realista, pausas aleatorias, reintentos
# ---------------------------------------------------------------------------


class PoliteClient:
    def __init__(self, settings: Settings):
        self.s = settings
        self._client = httpx.AsyncClient(
            timeout=settings.request_timeout,
            follow_redirects=True,
            http2=False,
        )

    async def close(self) -> None:
        await self._client.aclose()

    def _headers(self, accept_json: bool, referer: str | None) -> dict[str, str]:
        h = {
            "User-Agent": random.choice(USER_AGENTS),
            "Accept-Language": "es-CL,es;q=0.9,en;q=0.7",
            "Accept": "application/json, text/plain, */*" if accept_json
            else "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Cache-Control": "no-cache",
            "Pragma": "no-cache",
            "DNT": "1",
        }
        if referer:
            h["Referer"] = referer
        return h

    async def pause(self) -> None:
        await asyncio.sleep(random.uniform(self.s.request_delay_min, self.s.request_delay_max))

    async def get(
        self,
        url: str,
        *,
        params: Any = None,
        accept_json: bool = True,
        referer: str | None = None,
        retries: int = 3,
    ) -> httpx.Response:
        last_exc: Exception | None = None
        for attempt in range(1, retries + 1):
            await self.pause()
            try:
                r = await self._client.get(
                    url, params=params, headers=self._headers(accept_json, referer)
                )
                if r.status_code in (429, 500, 502, 503, 504):
                    wait = float(r.headers.get("Retry-After", 0) or 0) or (2 ** attempt) * 3
                    log.warning("HTTP %s en %s; reintento %s/%s en %.0fs",
                                r.status_code, url, attempt, retries, wait)
                    await asyncio.sleep(wait + random.uniform(0, 3))
                    continue
                if r.status_code == 403:
                    log.warning("HTTP 403 (posible WAF) en %s", url)
                if 400 <= r.status_code < 500:
                    # Error del cliente (404, 403...): reintentar no sirve
                    raise RuntimeError(f"HTTP {r.status_code} en {url}")
                r.raise_for_status()
                return r
            except httpx.HTTPError as exc:
                last_exc = exc
                log.warning("Error HTTP en %s (intento %s/%s): %s", url, attempt, retries, exc)
                await asyncio.sleep(2 ** attempt)
        raise RuntimeError(f"No se pudo obtener {url}: {last_exc}")


# ---------------------------------------------------------------------------
# Base
# ---------------------------------------------------------------------------


class BaseScraper:
    source = "base"

    def __init__(self, client: PoliteClient, settings: Settings):
        self.http = client
        self.s = settings

    async def fetch(self) -> list[Offer]:  # pragma: no cover - interfaz
        raise NotImplementedError

    def _pick_price(self, normal: int, offer: int) -> int:
        normal = normal or offer
        offer = offer or normal
        return normal if self.s.price_mode == "normal" else min(normal, offer)


_FALABELLA_ID_RE = re.compile(r"falabella\.com/falabella-cl/product/(\d+)")


def canonical_id(url: str, fallback: str) -> str:
    """Usa el ID de la tienda cuando es reconocible, para deduplicar entre fuentes."""
    m = _FALABELLA_ID_RE.search(url or "")
    if m:
        return f"falabella:{m.group(1)}"
    return fallback


# ---------------------------------------------------------------------------
# SoloTodo (API pública: https://publicapi.solotodo.com)
# ---------------------------------------------------------------------------


class SoloTodoScraper(BaseScraper):
    source = "solotodo"
    API = "https://publicapi.solotodo.com"
    CHILE_COUNTRY_ID = 1
    QUERIES = ("playstation 5 slim", "ps5 slim")

    def __init__(self, client: PoliteClient, settings: Settings):
        super().__init__(client, settings)
        self._stores: dict[str, str] = {}
        self._warned_shape = False

    # -- tiendas ------------------------------------------------------------
    async def _load_stores(self) -> None:
        if self._stores:
            return
        try:
            r = await self.http.get(f"{self.API}/stores/", params={"countries": self.CHILE_COUNTRY_ID})
            data = r.json()
            items = data.get("results", data) if isinstance(data, dict) else data
            for st in items or []:
                name = st.get("name", "")
                if st.get("url"):
                    self._stores[st["url"].rstrip("/")] = name
                if st.get("id") is not None:
                    self._stores[str(st["id"])] = name
            log.info("SoloTodo: %s tiendas cargadas", len(items or []))
        except Exception as exc:  # noqa: BLE001
            log.warning("SoloTodo: no se pudieron cargar tiendas: %s", exc)

    def _store_name(self, store: Any) -> str:
        if isinstance(store, dict):
            return store.get("name") or self._store_name(store.get("url") or store.get("id"))
        key = str(store or "").rstrip("/")
        if key in self._stores:
            return self._stores[key]
        m = re.search(r"/stores/(\d+)", key)
        if m and m.group(1) in self._stores:
            return self._stores[m.group(1)]
        return "Tienda desconocida"

    # -- búsqueda de productos -------------------------------------------------
    DEFAULT_CATEGORY_ID = 33  # "Consolas" en SoloTodo

    @staticmethod
    def _walk_products(obj: Any) -> Iterable[dict]:
        """Recorre cualquier JSON y extrae dicts con forma de producto SoloTodo."""
        if isinstance(obj, dict):
            looks_like_product = (
                "id" in obj and "name" in obj
                and ("category_id" in obj or "slug" in obj or "/products/" in str(obj.get("url", "")))
            )
            if looks_like_product:
                yield obj
                return
            for v in obj.values():
                yield from SoloTodoScraper._walk_products(v)
        elif isinstance(obj, list):
            for v in obj:
                yield from SoloTodoScraper._walk_products(v)

    async def _find_products(self) -> dict[int, str]:
        found: dict[int, str] = {}
        for pid in self.s.solotodo_product_ids:
            if pid.isdigit():
                found[int(pid)] = ""
        category = self.s.solotodo_category_id or self.DEFAULT_CATEGORY_ID
        for q in self.QUERIES:
            url: str | None = f"{self.API}/products/"
            params: dict | None = {"search": q, "page_size": 50, "categories": category}
            pages = 0
            while url and pages < 4:
                pages += 1
                try:
                    r = await self.http.get(url, params=params)
                    data = r.json()
                except Exception as exc:  # noqa: BLE001
                    log.warning("SoloTodo: búsqueda '%s' falló: %s", q, exc)
                    break
                for p in self._walk_products(data):
                    cat = p.get("category_id", p.get("category"))
                    if isinstance(cat, int) and cat != category:
                        continue
                    if classify_model(p["name"]):
                        found[int(p["id"])] = p["name"]
                url = data.get("next") if isinstance(data, dict) else None
                params = None  # "next" ya trae los parámetros
        log.info("SoloTodo: %s productos PS5 Slim candidatos", len(found))
        return found

    # -- entidades (ofertas por tienda) ----------------------------------------
    async def _available_entities(self, ids: list[int]) -> list[tuple[dict, dict]]:
        """Devuelve [(producto, entidad)] para las entidades disponibles."""
        pairs: list[tuple[dict, dict]] = []
        for i in range(0, len(ids), 20):
            chunk = ids[i:i + 20]
            params = [("ids", x) for x in chunk] + [("countries", self.CHILE_COUNTRY_ID)]
            try:
                r = await self.http.get(f"{self.API}/products/available_entities/", params=params)
                data = r.json()
                rows = data.get("results", data) if isinstance(data, dict) else data
                for row in rows or []:
                    product = row.get("product", {})
                    for ent in row.get("entities", []):
                        pairs.append((product, ent))
            except Exception as exc:  # noqa: BLE001
                log.warning("SoloTodo: available_entities falló (%s); uso /entities/", exc)
                for pid in chunk:
                    try:
                        r = await self.http.get(
                            f"{self.API}/entities/",
                            params={"products": pid, "countries": self.CHILE_COUNTRY_ID,
                                    "page_size": 100},
                        )
                        data = r.json()
                        for ent in data.get("results", data) if isinstance(data, dict) else data:
                            pairs.append((ent.get("product") or {"id": pid}, ent))
                    except Exception as exc2:  # noqa: BLE001
                        log.warning("SoloTodo: /entities/ producto %s falló: %s", pid, exc2)
        return pairs

    async def fetch(self) -> list[Offer]:
        await self._load_stores()
        products = await self._find_products()
        if not products:
            raise RuntimeError("SoloTodo no devolvió productos (¿API caída o cambió?)")
        pairs = await self._available_entities(list(products))

        offers: list[Offer] = []
        for product, ent in pairs:
            reg = ent.get("active_registry") or ent.get("registry") or {}
            if not reg and ("offer_price" in ent or "normal_price" in ent):
                reg = ent
            if not reg:
                if not self._warned_shape:
                    log.warning("SoloTodo: entidad sin precios; claves: %s", sorted(ent)[:25])
                    self._warned_shape = True
                continue
            if reg.get("is_available") is False:
                continue
            condition = str(ent.get("condition", "NewCondition"))
            if "NewCondition" not in condition:
                continue  # descarta reacondicionados / usados

            pname = (product.get("name") if isinstance(product, dict) else "") \
                or products.get(int(product.get("id", 0) or 0), "") or ent.get("name", "")
            model = classify_model(pname) or classify_model(ent.get("name", ""))
            if not model:
                continue

            normal = parse_clp(reg.get("normal_price"))
            offer_p = parse_clp(reg.get("offer_price"))
            price = self._pick_price(normal, offer_p)
            if price < self.s.min_sane_price:
                continue

            if offer_p and normal and offer_p < normal:
                note = (f"Precio oferta {fmt_clp(offer_p)} con medio de pago específico "
                        f"(normalmente tarjeta de la tienda). Precio normal: {fmt_clp(normal)}")
            else:
                note = "Precio normal (cualquier medio de pago)"

            url = ent.get("external_url") or f"https://www.solotodo.cl/products/{product.get('id')}"
            offers.append(Offer(
                product_id=canonical_id(url, f"solotodo:{ent.get('id')}"),
                store=self._store_name(ent.get("store") or ent.get("store_id")),
                model=model,
                price=price,
                normal_price=normal or price,
                offer_price=offer_p or price,
                payment_note=note,
                url=url,
                name=pname,
                source=self.source,
            ))
        log.info("SoloTodo: %s ofertas PS5 Slim disponibles", len(offers))
        return offers


# ---------------------------------------------------------------------------
# Falabella (respaldo directo)
# ---------------------------------------------------------------------------


class FalabellaScraper(BaseScraper):
    source = "falabella"
    SEARCH_API = "https://www.falabella.com/s/browse/v1/search/cl"
    SEARCH_HTML = "https://www.falabella.com/falabella-cl/search"
    QUERIES = ("ps5 slim", "playstation 5 slim")

    _NEXT_DATA_RE = re.compile(
        r'<script id="__NEXT_DATA__" type="application/json"[^>]*>(.*?)</script>', re.S
    )

    _api_dead = False  # se activa si la API JSON responde 404 (cambió de ruta)

    async def _search(self, query: str) -> list[dict]:
        # 1) API JSON interna (se omite si ya respondió 404 en esta sesión)
        try:
            if FalabellaScraper._api_dead:
                raise RuntimeError("API omitida")
            r = await self.http.get(
                self.SEARCH_API,
                params={"Ntt": query, "page": 1},
                referer="https://www.falabella.com/falabella-cl",
            )
            results = (r.json().get("data") or {}).get("results") or []
            if results:
                return results
        except Exception as exc:  # noqa: BLE001
            if "404" in str(exc):
                FalabellaScraper._api_dead = True
            log.debug("Falabella API no disponible (%s); uso HTML", exc)
        # 2) HTML con __NEXT_DATA__
        r = await self.http.get(
            self.SEARCH_HTML, params={"Ntt": query}, accept_json=False,
            referer="https://www.falabella.com/falabella-cl",
        )
        m = self._NEXT_DATA_RE.search(r.text)
        if not m:
            raise RuntimeError("Falabella: no se encontró __NEXT_DATA__ (¿bloqueo WAF?)")
        data = json.loads(m.group(1))
        page = (data.get("props") or {}).get("pageProps") or {}
        return page.get("results") or []

    @staticmethod
    def _prices(item: dict) -> tuple[int, int, str | None]:
        """Devuelve (precio_normal_sin_tarjeta, precio_minimo, tipo_del_minimo)."""
        by_type: dict[str, int] = {}
        for p in item.get("prices") or []:
            if p.get("crossed"):
                continue  # precio tachado "antes"
            val = parse_clp(p.get("price"))
            if val:
                by_type[str(p.get("type", ""))] = val
        if not by_type:
            return 0, 0, None
        non_card = [v for k, v in by_type.items() if "cmr" not in k.lower()]
        min_type = min(by_type, key=by_type.get)
        normal = min(non_card) if non_card else by_type[min_type]
        return normal, by_type[min_type], min_type

    async def fetch(self) -> list[Offer]:
        offers: dict[str, Offer] = {}
        errors = 0
        for q in self.QUERIES:
            try:
                items = await self._search(q)
            except Exception as exc:  # noqa: BLE001
                errors += 1
                log.warning("Falabella: búsqueda '%s' falló: %s", q, exc)
                continue
            for it in items:
                name = f"{it.get('brand', '')} {it.get('displayName', '')}".strip()
                model = classify_model(name)
                if not model:
                    continue
                normal, minimum, min_type = self._prices(it)
                price = self._pick_price(normal, minimum)
                if price < self.s.min_sane_price:
                    continue
                url = it.get("url") or ""
                if url and not url.startswith("http"):
                    url = "https://www.falabella.com" + url
                seller = it.get("sellerName") or ""
                store = "Falabella" if not seller or "falabella" in seller.lower() \
                    else f"Falabella (vendido por {seller})"
                if min_type and "cmr" in min_type.lower() and minimum < normal:
                    note = (f"Solo con tarjeta CMR Falabella: {fmt_clp(minimum)}. "
                            f"Con otros medios: {fmt_clp(normal)}")
                else:
                    note = "Precio normal (cualquier medio de pago)"
                pid = canonical_id(url, f"falabella:{it.get('productId') or it.get('skuId')}")
                offers[pid] = Offer(
                    product_id=pid, store=store, model=model, price=price,
                    normal_price=normal or price, offer_price=minimum or price,
                    payment_note=note, url=url, name=name, source=self.source,
                )
        if errors == len(self.QUERIES):
            raise RuntimeError("Falabella: todas las búsquedas fallaron")
        log.info("Falabella: %s ofertas PS5 Slim", len(offers))
        return list(offers.values())


# ---------------------------------------------------------------------------
# Orquestador
# ---------------------------------------------------------------------------


async def collect_offers(settings: Settings) -> tuple[list[Offer], set[str]]:
    """Consulta todas las fuentes. Devuelve (ofertas_deduplicadas, fuentes_ok)."""
    client = PoliteClient(settings)
    scrapers: list[BaseScraper] = []
    if settings.enable_solotodo:
        scrapers.append(SoloTodoScraper(client, settings))
    if settings.enable_falabella:
        scrapers.append(FalabellaScraper(client, settings))

    all_offers: list[Offer] = []
    ok_sources: set[str] = set()
    try:
        for sc in scrapers:  # secuencial a propósito: menos ruido para los WAF
            try:
                all_offers.extend(await sc.fetch())
                ok_sources.add(sc.source)
            except Exception as exc:  # noqa: BLE001
                log.error("Fuente %s falló: %s", sc.source, exc)
    finally:
        await client.close()

    # Deduplicación: misma oferta vista por SoloTodo y por la tienda -> menor precio
    best: dict[str, Offer] = {}
    for o in all_offers:
        if o.product_id not in best or o.price < best[o.product_id].price:
            best[o.product_id] = o
    return list(best.values()), ok_sources
