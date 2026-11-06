# 🎮 Bot de alertas PS5 Slim < $600.000 CLP

Bot que revisa SoloTodo (agrega Falabella, Ripley, Paris, Lider, PC Factory, Weplay, Zmart, etc.)
y Falabella directo, y avisa por Telegram **solo si el precio es menor a $600.000 CLP**.

## Estructura

```
ps5-bot/
├── main.py           # Scheduler (APScheduler + asyncio), flujo de cada revisión
├── scraper.py        # SoloTodoScraper, FalabellaScraper, clasificación Digital/Con Lector
├── database.py       # SQLite (offers.db) + regla anti-spam
├── notifier.py       # Mensajes MarkdownV2 vía Bot API de Telegram
├── config.py         # Lectura de .env y tope inmutable de $600.000
├── .env.example      # Plantilla de variables
├── requirements.txt
├── Dockerfile / docker-compose.yml
└── tests/            # Pruebas offline (lógica y ciclo completo con HTTP simulado)
```

## Reglas clave

- **Filtro global inmutable**: `HARD_PRICE_CAP_CLP = 600_000` en `config.py`. Se alerta sólo si
  `precio < umbral`. Si en `.env` pones un valor mayor, se recorta a 600.000.
- **Umbrales por modelo** (opcionales): `MAX_PRICE_DIGITAL`, `MAX_PRICE_STANDARD`. El umbral
  efectivo es el menor entre el umbral del modelo, `MAX_PRICE_THRESHOLD` y 600.000.
- **Modelo**: se detecta Digital / Con Lector (Blu-ray) a partir del nombre. Se descartan PS5 Pro,
  Portal, VR, reacondicionados, usados, accesorios (lector de discos suelto, controles, etc.) y
  todo lo que cueste menos que `MIN_SANE_PRICE`.
- **Medio de pago**: si el menor precio exige tarjeta de la tienda (CMR en Falabella, o
  `offer_price < normal_price` en SoloTodo), el mensaje lo indica junto al precio normal.
  Con `PRICE_MODE=normal` el filtro usa sólo el precio con cualquier medio de pago.
- **Anti-spam**: una oferta ya notificada sólo vuelve a avisarse si **baja aún más** o si
  **estuvo agotada y vuelve con stock** bajo el umbral. Una fuente que falla en un ciclo no
  marca sus productos como agotados.

## 1. Credenciales de Telegram

1. En Telegram, abre **@BotFather** → `/newbot` → elige nombre y usuario (debe terminar en `bot`).
   Copia el token (`123456789:AA...`) en `TELEGRAM_BOT_TOKEN`.
2. Abre el chat con tu bot nuevo y envíale cualquier mensaje (p. ej. `/start`).
3. Obtén tu chat ID con cualquiera de estas opciones:
   - Abre `https://api.telegram.org/bot<TU_TOKEN>/getUpdates` en el navegador y busca
     `"chat":{"id": ...}`.
   - O escribe a **@userinfobot**, que responde con tu ID.
   - Para un grupo: agrega el bot al grupo, escribe un mensaje y usa `getUpdates`
     (el ID del grupo es negativo, p. ej. `-1001234567890`).
4. Pon ese número en `TELEGRAM_CHAT_ID`.

## 2. Ejecutar localmente

```bash
cd ps5-bot
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt
# edita configuracion.txt (token / chat id)

python main.py --test-notify       # envía un mensaje de prueba a Telegram
python main.py --once --dry-run    # una revisión, imprime alertas sin enviarlas
python main.py                     # modo continuo (cada CHECK_INTERVAL_MINUTES)
```

Pruebas: `python tests/test_logic.py` y `python tests/test_e2e_mock.py`.

## 3. Ejecutar con Docker

```bash
# edita configuracion.txt
docker compose up -d --build
docker compose logs -f             # ver actividad
docker compose down                # detener
```

La base de datos queda en `./data/offers.db` y sobrevive a reinicios del contenedor.

Sin compose:

```bash
docker build -t ps5-bot .
docker run -d --name ps5-bot --env-file configuracion.txt -v "$(pwd)/data:/app/data" --restart unless-stopped ps5-bot
```

## Consultar la base de datos

```bash
sqlite3 offers.db "SELECT tienda, modelo, precio_actual, en_stock, fecha_notificacion FROM offers ORDER BY precio_actual;"
sqlite3 offers.db "SELECT * FROM notificaciones ORDER BY id DESC LIMIT 10;"
```

## Notas y mantenimiento

- **SoloTodo**: usa `publicapi.solotodo.com` (`/products/?search=`, `/products/available_entities/`
  y `/entities/` como respaldo). Para mayor precisión puedes fijar productos con
  `SOLOTODO_PRODUCT_IDS` (el número al inicio de la URL de un producto en solotodo.cl, p. ej.
  `364198`) o la categoría con `SOLOTODO_CATEGORY_ID`.
- **Falabella**: prueba primero su API JSON de búsqueda y, si falla, extrae `__NEXT_DATA__` del HTML.
  Las tiendas cambian sus webs: si una fuente empieza a fallar, el log lo muestra
  (`Fuente falabella falló`) y el resto sigue funcionando.
- Para agregar otra tienda: crea una clase que herede de `BaseScraper`, implementa `fetch()`
  devolviendo `Offer`s y regístrala en `collect_offers()`.
- Respeta los términos de uso de cada sitio. Un intervalo de 15 minutos con pausas aleatorias
  de 2 a 6 s entre requests es un ritmo prudente; no lo bajes demasiado.
