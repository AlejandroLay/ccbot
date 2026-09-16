#!/usr/bin/env python3
"""
Monitor de nuevos productos en Cash Converters -> aviso por Discord.

Vigila una o varias busquedas (definidas en config.json) y avisa por un
webhook de Discord (definido en .env) cuando aparece un producto nuevo que
cumple los filtros de esa busqueda.

Uso:
    python cc_bot.py                # ejecucion normal: todas las busquedas activas
    python cc_bot.py --solo NOMBRE  # ejecuta solo esa busqueda
    python cc_bot.py --dump         # guarda el HTML crudo de cada busqueda y sale
    python cc_bot.py --seed         # marca todo lo actual como visto, SIN avisar
"""

import argparse
import json
import os
import re
import sys
import time
import unicodedata
from datetime import datetime, timedelta, timezone
from pathlib import Path

from curl_cffi import requests as creq

# ---------------------------------------------------------------------------
# RUTAS Y CONSTANTES
# ---------------------------------------------------------------------------

BASE_DIR = Path(__file__).resolve().parent
CONFIG_FILE = BASE_DIR / "config.json"
ENV_FILE = BASE_DIR / ".env"
STATE_DIR = BASE_DIR / "state"
LOG_FILE = BASE_DIR / "cc_bot.log"

RETENCION_DIAS = 30           # dias que se recuerda un producto como "visto"
MAX_REINTENTOS = 3            # reintentos de descarga por fallo de red
ESPERA_ENTRE_BUSQUEDAS = 2    # segundos de pausa entre una busqueda y la siguiente
# Con 8 busquedas, cada segundo de pausa son 7 segundos de ejecucion. GitHub
# factura por minutos redondeando hacia arriba, asi que bajar de 5s a 2s hace
# que el trabajo entre en 1 minuto en vez de 2: el doble de ejecuciones con
# los mismos 2.000 minutos gratuitos. 2s entre peticiones sigue siendo mas
# suave que una persona navegando.
TIMEOUT_DESCARGA = 30
EMBEDS_POR_MENSAJE = 10       # limite de Discord

HEADERS = {
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "es-ES,es;q=0.9,ca;q=0.8,en;q=0.7",
    "Referer": "https://www.cashconverters.es/",
}


# ---------------------------------------------------------------------------
# LOGGING
# ---------------------------------------------------------------------------

def log(msg: str, nivel: str = "INFO") -> None:
    linea = f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')} [{nivel}] {msg}"
    print(linea)
    with LOG_FILE.open("a", encoding="utf-8") as f:
        f.write(linea + "\n")


# ---------------------------------------------------------------------------
# CONFIGURACION Y SECRETOS
# ---------------------------------------------------------------------------

def cargar_config() -> dict:
    if not CONFIG_FILE.exists():
        log(f"No existe {CONFIG_FILE}. Copia config.json.example o crea uno.", "ERROR")
        sys.exit(1)
    try:
        return json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        log(f"config.json no es JSON valido: {e}", "ERROR")
        sys.exit(1)


def obtener_webhook() -> str:
    """El webhook sale de la variable de entorno (asi lo inyecta GitHub Actions
    desde Secrets) y, si no esta, del fichero .env (ejecucion local)."""
    return os.environ.get("DISCORD_WEBHOOK_URL") or cargar_env(ENV_FILE).get("DISCORD_WEBHOOK_URL", "")


def cargar_env(path: Path) -> dict:
    """Parser minimo de un fichero .env (KEY=VALOR), sin dependencias externas."""
    valores = {}
    if not path.exists():
        return valores
    for linea in path.read_text(encoding="utf-8").splitlines():
        linea = linea.strip()
        if not linea or linea.startswith("#") or "=" not in linea:
            continue
        clave, _, valor = linea.partition("=")
        valores[clave.strip()] = valor.strip().strip('"').strip("'")
    return valores


# ---------------------------------------------------------------------------
# 1. DESCARGA
# ---------------------------------------------------------------------------

def descargar(url: str, intentos: int = MAX_REINTENTOS) -> str:
    """Descarga imitando el fingerprint TLS de Chrome (esquiva la mayoria de WAF).

    Reintenta con espera exponencial (2s, 4s, 8s...) ante fallos de red.
    """
    ultimo_error = None
    for intento in range(1, intentos + 1):
        try:
            r = creq.get(url, headers=HEADERS, impersonate="chrome", timeout=TIMEOUT_DESCARGA)
            r.raise_for_status()
            return r.text
        except Exception as e:
            ultimo_error = e
            if intento < intentos:
                espera = 2 ** intento
                log(
                    f"Descarga fallida (intento {intento}/{intentos}): "
                    f"{type(e).__name__}: {e}. Reintento en {espera}s",
                    "WARN",
                )
                time.sleep(espera)
    raise ultimo_error


# ---------------------------------------------------------------------------
# 2. EXTRACCION  <-- la parte que tendras que tocar si cashconverters.es
#    cambia de maquetacion. Todo lo especifico de la web vive aqui dentro,
#    en funciones pequenas, para que un cambio de HTML se arregle en un
#    solo sitio.
# ---------------------------------------------------------------------------
# Cada producto sale como un dict con estas claves:
#   id (str, unico y estable = data-pid), titulo (str), precio (float o None),
#   url (str), imagen (str o None)

def extraer_productos(html: str) -> list[dict]:
    """Punto de entrada de la extraccion. Prioriza los tiles SFRA (data-pid);
    si la web cambiase y dejasen de aparecer, cae a JSON-LD como plan B.
    """
    items = _desde_tiles(html)
    if items:
        return items
    return _desde_jsonld(html)


def _desde_tiles(html: str) -> list[dict]:
    """
    Extractor principal para Salesforce Commerce Cloud (SFRA), que es lo que
    usa cashconverters.es. Cada producto del listado es un elemento con
    atributo data-pid: ese pid es el identificador estable, no la URL.
    """
    from bs4 import BeautifulSoup

    sopa = BeautifulSoup(html, "html.parser")
    items, vistos = [], set()

    for tile in sopa.select("[data-pid]"):
        pid = (tile.get("data-pid") or "").strip()
        if not pid or pid in vistos:
            continue

        enlace = tile.select_one(".pdp-link a[href]") or tile.select_one("a[href]")
        if enlace is None:
            continue
        href = enlace.get("href", "")
        if "javascript:" in href or href.startswith("#"):
            continue

        titulo = enlace.get_text(" ", strip=True)
        if not titulo:
            img = tile.select_one("img[alt]")
            titulo = img.get("alt", "").strip() if img else ""
        if not titulo:
            continue  # tile vacio (carrusel, placeholder...)

        vistos.add(pid)
        items.append({
            "id": pid,
            "titulo": " ".join(titulo.split()),
            "precio": _precio_de_tile(tile),
            "precio_antes": _precio_antes_de_tile(tile),
            "estado": _estado_de_tile(tile),
            "url": _url_de_producto(pid, href),
            "imagen": _imagen_de_tile(tile),
        })
    return items


def _precio_de_tile(tile):
    """Precio de venta ACTUAL, que en un producto rebajado no es el que canta.

    La maquetacion real de cashconverters.es es esta:

        <div class="old-price">Antes <del>1.008,95 €</del></div>
        <div class="principal" data-price="968.95">968,95 €</div>

    O sea que hay que quedarse con .principal e ignorar el <del>. El atributo
    data-price viene ya normalizado (punto decimal), asi que es la fuente
    preferida; lo demas son planes B por si cambian la plantilla.
    """
    # 1. Lo mejor: el atributo data-price de .principal
    nodo = tile.select_one(".principal[data-price]")
    if nodo is not None:
        valor = _a_float(nodo["data-price"])
        if valor is not None:
            return valor

    # 2. El texto de .principal
    nodo = tile.select_one(".principal")
    if nodo is not None:
        valor = _a_float(nodo.get_text(" ", strip=True))
        if valor is not None:
            return valor

    # 3. El JSON de analitica que la propia web mete en el tile
    valor = _precio_de_datalayer(tile)
    if valor is not None:
        return valor

    # 4. Maquetacion estandar de SFRA, por si volvieran a ella
    nodo = tile.select_one(".sales .value, .sales")
    if nodo is not None:
        if nodo.get("content"):
            valor = _a_float(nodo["content"])
            if valor is not None:
                return valor
        valor = _a_float(nodo.get_text(" ", strip=True))
        if valor is not None:
            return valor

    # 5. Ultimo recurso: el primer importe en euros que no sea un precio viejo
    for texto in tile.find_all(string=re.compile(r"\d[\d.,]*\s*€")):
        if _es_precio_viejo(texto.parent):
            continue
        valor = _a_float(str(texto))
        if valor is not None:
            return valor
    return None


def _precio_de_datalayer(tile):
    return _a_float((_datalayer(tile) or {}).get("price"))


def _datalayer(tile):
    """La web incrusta en cada tile un JSON de analitica con datos utiles:
    data-product-datalayer='{"id":"...","price":968.95,"variant":"Usado",...}'."""
    crudo = tile.get("data-product-datalayer")
    if not crudo:
        return None
    try:
        datos = json.loads(crudo)
    except json.JSONDecodeError:
        return None
    return datos if isinstance(datos, dict) else None


def _es_precio_viejo(nodo) -> bool:
    """True si el importe cuelga de un tachado: <del>, .old-price,
    .strike-through... Son el precio de antes del descuento."""
    while nodo is not None and getattr(nodo, "name", None):
        if nodo.name in ("del", "s", "strike"):
            return True
        clases = " ".join(nodo.get("class") or [])
        if "old-price" in clases or "strike" in clases or "line-through" in clases:
            return True
        nodo = nodo.parent
    return False


def _precio_antes_de_tile(tile):
    """El precio tachado de antes de la rebaja, si el producto esta rebajado."""
    nodo = tile.select_one(".old-price")
    if nodo is None:
        return None
    return _a_float(nodo.get_text(" ", strip=True))


def _estado_de_tile(tile):
    """Estado del articulo de segunda mano: "Perfecto", "Usado"..."""
    nodo = tile.select_one(".status")
    if nodo is not None:
        texto = nodo.get_text(" ", strip=True)
        if texto:
            return texto
    return (_datalayer(tile) or {}).get("variant") or None


def _imagen_de_tile(tile):
    img = tile.select_one("img")
    if img is None:
        return None
    src = (
        img.get("src")
        or img.get("data-src")
        or (img.get("srcset") or "").split(" ")[0]
        or (img.get("data-srcset") or "").split(" ")[0]
    )
    if not src or src.startswith("data:"):
        return None
    return _absoluta(src)


def _desde_jsonld(html: str) -> list[dict]:
    """Plan B si algun dia los tiles dejan de traer data-pid: los productos
    tambien se anuncian como JSON-LD (@type Product) en el <head>."""
    items = []
    bloques = re.findall(
        r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
        html, re.DOTALL | re.IGNORECASE,
    )
    for bloque in bloques:
        try:
            data = json.loads(bloque.strip())
        except json.JSONDecodeError:
            continue
        for prod in _buscar_productos(data):
            oferta = prod.get("offers") or {}
            if isinstance(oferta, list):
                oferta = oferta[0] if oferta else {}
            url = _absoluta(prod.get("url") or "")
            ident = prod.get("sku") or prod.get("productID") or url
            if not ident:
                continue
            imagen = prod.get("image")
            if isinstance(imagen, list):
                imagen = imagen[0] if imagen else None
            items.append({
                "id": str(ident),
                "titulo": str(prod.get("name") or ""),
                "precio": _a_float(oferta.get("price")),
                "url": url,
                "imagen": _absoluta(imagen) if imagen else None,
            })
    return items


def _buscar_productos(nodo) -> list[dict]:
    """Recorre el JSON-LD en profundidad y saca todo lo que sea @type Product."""
    encontrados = []
    if isinstance(nodo, dict):
        tipo = nodo.get("@type")
        tipos = tipo if isinstance(tipo, list) else [tipo]
        if "Product" in tipos:
            encontrados.append(nodo)
        for valor in nodo.values():
            encontrados += _buscar_productos(valor)
    elif isinstance(nodo, list):
        for valor in nodo:
            encontrados += _buscar_productos(valor)
    return encontrados


def _a_float(valor):
    if valor is None:
        return None
    if isinstance(valor, (int, float)):
        return float(valor)
    s = re.sub(r"[^\d,.]", "", str(valor))
    if not s:
        return None
    if "," in s and "." in s:
        # el separador que va el ultimo es el decimal: 1.299,00 o 1,299.00
        dec = "," if s.rfind(",") > s.rfind(".") else "."
        mil = "." if dec == "," else ","
        s = s.replace(mil, "").replace(dec, ".")
    elif "," in s:
        s = s.replace(",", ".")          # formato espanol: 329,00
    elif s.count(".") == 1:
        entero, resto = s.split(".")
        if len(resto) == 3 and len(entero) <= 3:
            s = entero + resto           # 1.299 -> miles
    else:
        s = s.replace(".", "")           # 1.299.000
    try:
        return float(s)
    except ValueError:
        return None


def _url_de_producto(pid: str, href: str) -> str:
    """Enlace a la FICHA de la unidad concreta, no al listado donde aparece.

    Muchos tiles no enlazan a la ficha, sino a la categoria con la unidad
    marcada al final:

        /comprar/videojuegos-y-consolas/ps5/consolas/playstation-5/?firstProduct=CF001_E56200_0

    Si mandas eso por Discord, el aviso te deja en una lista de 26 consolas y
    tienes que buscar a mano cual era. La ficha de cada unidad vive siempre en
    /segunda-mano/<pid>.html, asi que cuando el href no es ya una ficha la
    reconstruimos a partir del pid.

    Lo que distingue a una ficha es que su ruta acaba en .html; los enlaces a
    listado son la ruta de la categoria mas ?firstProduct=<pid>.
    """
    ruta = href.split("?")[0].split("#")[0]
    if ruta.endswith(".html"):
        return _absoluta(href)
    return f"https://www.cashconverters.es/es/es/segunda-mano/{pid}.html"


def _absoluta(url: str) -> str:
    if url.startswith("//"):
        return "https:" + url
    if url.startswith("/"):
        return "https://www.cashconverters.es" + url
    return url


# ---------------------------------------------------------------------------
# 2b. FICHA TECNICA DEL PRODUCTO
#
# El listado no trae las especificaciones: hay que abrir la ficha, que las
# publica como pares etiqueta/valor. Solo se consulta para los productos que
# ya sabemos que vamos a avisar, que son pocos, no para todo el listado.
# ---------------------------------------------------------------------------

MAX_FICHAS = 10   # tope de fichas por busqueda y ejecucion, por cortesia


def ficha_tecnica(url: str) -> dict:
    """Especificaciones de UNA unidad concreta (no del modelo): pulgadas, RAM,
    capacidad, procesador, idioma del teclado..."""
    try:
        return _parsear_ficha(descargar(url, intentos=2))
    except Exception as e:
        # Que no llegue la ficha nunca debe impedir el aviso.
        log(f"No se pudo leer la ficha ({type(e).__name__}): {url[:80]}", "WARN")
        return {}


def _parsear_ficha(html: str) -> dict:
    """<li class="attribute-values"><span class="label">pulgadas:</span>
        <span class="value">13.0</span></li>  ->  {"pulgadas": "13.0"}"""
    from bs4 import BeautifulSoup

    datos = {}
    for li in BeautifulSoup(html, "html.parser").select(".attribute-values"):
        etiqueta = li.select_one(".label")
        valor = li.select_one(".value")
        if etiqueta is None or valor is None:
            continue
        clave = etiqueta.get_text(" ", strip=True).rstrip(":").strip().lower()
        texto = valor.get_text(" ", strip=True)
        if clave and texto:
            datos[clave] = texto
    return datos


# ---------------------------------------------------------------------------
# 3. FILTROS
# ---------------------------------------------------------------------------

def normalizar(texto: str) -> str:
    """minusculas + sin acentos, para que 'portatil' case con 'portátil'."""
    texto = texto.lower()
    texto = unicodedata.normalize("NFKD", texto)
    return "".join(c for c in texto if not unicodedata.combining(c))


def pasa_filtros(item: dict, busqueda: dict) -> bool:
    titulo = normalizar(item["titulo"])
    todas = [normalizar(k) for k in busqueda.get("keywords_todas") or []]
    ninguna = [normalizar(k) for k in busqueda.get("keywords_ninguna") or []]
    precio_min = busqueda.get("precio_min")
    precio_max = busqueda.get("precio_max")

    if todas and not all(k in titulo for k in todas):
        return False
    if ninguna and any(k in titulo for k in ninguna):
        return False

    # Un producto sin precio legible NUNCA se descarta por precio: mejor un
    # aviso de mas que perderse el bueno por no saber cuanto cuesta.
    if item["precio"] is not None:
        if precio_min is not None and item["precio"] < precio_min:
            return False
        if precio_max is not None and item["precio"] > precio_max:
            return False
    return True


# ---------------------------------------------------------------------------
# 4. ESTADO (uno por busqueda, en state/<nombre>.json)
# ---------------------------------------------------------------------------

def _state_file(nombre: str) -> Path:
    return STATE_DIR / f"{nombre}.json"


def cargar_vistos(nombre: str) -> dict:
    ruta = _state_file(nombre)
    if not ruta.exists():
        return {}
    try:
        return json.loads(ruta.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        log(f"[{nombre}] state corrupto, empiezo de cero", "WARN")
        return {}


def guardar_vistos(nombre: str, vistos: dict) -> None:
    limite = datetime.now(timezone.utc) - timedelta(days=RETENCION_DIAS)
    podado = {
        k: v for k, v in vistos.items()
        if datetime.fromisoformat(v) > limite
    }
    STATE_DIR.mkdir(exist_ok=True)
    ruta = _state_file(nombre)
    tmp = ruta.with_suffix(".tmp")
    tmp.write_text(json.dumps(podado, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(ruta)  # escritura atomica: no se corrompe si el proceso muere a medias


# ---------------------------------------------------------------------------
# 5. DISCORD
# ---------------------------------------------------------------------------

def _euros(valor) -> str:
    """1470.95 -> '1.470,95 €' (formato espanol)."""
    if valor is None:
        return "sin precio"
    entero, _, decimales = f"{valor:,.2f}".partition(".")
    return f"{entero.replace(',', '.')},{decimales} €"


def _titulo_legible(titulo: str) -> str:
    """Los titulos vienen en minusculas y con palabras repetidas
    ("portatil apple apple macbook air m2"). Se limpia para el aviso."""
    palabras, limpio = titulo.split(), []
    for palabra in palabras:
        if not limpio or palabra.lower() != limpio[-1].lower():
            limpio.append(palabra)
    texto = " ".join(limpio)
    return texto[:1].upper() + texto[1:]


# Que campos de la ficha se enseñan y en que orden. Se pintan solo los que
# el producto tenga: un iPad no trae teclado y una consola no trae RAM.
CAMPOS_FICHA = [
    ("Chip", ("procesador",)),
    ("Pantalla", ("pulgadas",)),
    ("RAM", ("memoria ram",)),
    ("Almacenamiento", ("capacidad ssd", "capacidad", "capacidad hdd")),
    ("Teclado", ("idioma teclado",)),
    ("Año", ("año de lanzamiento",)),
]


def _formatear_spec(nombre: str, valor: str) -> str:
    """La web da los numeros en crudo: '13.0', '16.0', '1000.0'."""
    numero = _a_float(valor)
    if nombre == "Pantalla" and numero:
        return f'{numero:g}"'
    if nombre in ("RAM", "Almacenamiento") and numero:
        if numero >= 1000:
            return f"{numero / 1000:g} TB"
        return f"{numero:g} GB"
    if nombre == "Año" and numero:
        return f"{numero:.0f}"
    if nombre == "Chip":
        return valor.upper() if len(valor) <= 6 else valor.capitalize()
    return valor.capitalize()


def _campos_de_ficha(ficha: dict) -> list[dict]:
    campos = []
    for nombre, claves in CAMPOS_FICHA:
        for clave in claves:
            valor = (ficha or {}).get(clave)
            if not valor:
                continue
            numero = _a_float(valor)
            if numero == 0:          # "capacidad hdd: 0.0" = no tiene
                continue
            campos.append({"name": nombre, "value": _formatear_spec(nombre, valor), "inline": True})
            break
    return campos


def _embed_de(item: dict, nombre_busqueda: str, detectado: datetime) -> dict:
    """Un producto -> una tarjeta de Discord con los datos separados en campos."""
    precio = _euros(item.get("precio"))
    antes = item.get("precio_antes")
    if antes and item.get("precio") and antes > item["precio"]:
        descuento = round((1 - item["precio"] / antes) * 100)
        precio = f"**{precio}**\nantes {_euros(antes)} · **-{descuento}%**"
    else:
        precio = f"**{precio}**"

    campos = [{"name": "Precio", "value": precio, "inline": True}]
    if item.get("estado"):
        campos.append({"name": "Estado", "value": item["estado"], "inline": True})
    # <t:...:R> lo pinta Discord como "hace 3 minutos", en la zona horaria de
    # quien lo lee. La web no publica cuando subieron el articulo, asi que lo
    # honesto es decir cuando lo vio el bot.
    campos.append({
        "name": "Detectado",
        "value": f"<t:{int(detectado.timestamp())}:R>",
        "inline": True,
    })
    campos += _campos_de_ficha(item.get("ficha"))

    embed = {
        "title": _titulo_legible(item["titulo"])[:250] or "Producto nuevo",
        "url": item["url"],
        "fields": campos,
        "color": 0x2ECC71,
        "footer": {"text": f"Cash Converters · {nombre_busqueda}"},
        "timestamp": detectado.isoformat(),
    }
    if item.get("imagen"):
        embed["thumbnail"] = {"url": item["imagen"]}
    return embed


def avisar(nuevos: list[dict], webhook_url: str, nombre_busqueda: str) -> None:
    detectado = datetime.now(timezone.utc)
    lotes = [nuevos[i:i + EMBEDS_POR_MENSAJE] for i in range(0, len(nuevos), EMBEDS_POR_MENSAJE)]
    for lote in lotes:
        cuantos = f"**{len(lote)} producto{'s' if len(lote) > 1 else ''} nuevo{'s' if len(lote) > 1 else ''}**"
        payload = {
            "content": f"🆕 {cuantos} en `{nombre_busqueda}`",
            "embeds": [_embed_de(i, nombre_busqueda, detectado) for i in lote],
        }
        r = creq.post(webhook_url, json=payload, timeout=20)
        if r.status_code == 429:  # rate limit de Discord
            espera = r.json().get("retry_after", 2)
            time.sleep(espera + 0.5)
            r = creq.post(webhook_url, json=payload, timeout=20)
            if r.status_code >= 300:
                log(f"[{nombre_busqueda}] Discord respondio {r.status_code} tras reintento: {r.text[:200]}", "ERROR")
        elif r.status_code >= 300:
            log(f"[{nombre_busqueda}] Discord respondio {r.status_code}: {r.text[:200]}", "ERROR")
        time.sleep(1)


# ---------------------------------------------------------------------------
# PROCESO DE UNA BUSQUEDA
# ---------------------------------------------------------------------------

def procesar_busqueda(busqueda: dict, webhook_url: str, modo_dump: bool, modo_seed: bool) -> bool:
    """Devuelve False si la busqueda ha fallado. main() usa eso para terminar
    con codigo de salida 1, y asi GitHub Actions marca la ejecucion en rojo y
    te avisa por correo en vez de fallar en silencio."""
    nombre = busqueda.get("nombre", "?")

    if not busqueda.get("activa", True):
        log(f"[{nombre}] inactiva, se omite")
        return True

    url = busqueda.get("url")
    if not url:
        log(f"[{nombre}] sin 'url' en config.json, se omite", "ERROR")
        return False

    try:
        html = descargar(url)
    except Exception as e:
        log(f"[{nombre}] ERROR descargando: {type(e).__name__}: {e}", "ERROR")
        return False

    if modo_dump:
        destino = BASE_DIR / f"debug_{nombre}.html"
        destino.write_text(html, encoding="utf-8")
        log(f"[{nombre}] Respuesta guardada en {destino} ({len(html)} bytes)")
        return True

    try:
        items = extraer_productos(html)
    except Exception as e:
        log(f"[{nombre}] ERROR extrayendo: {type(e).__name__}: {e}", "ERROR")
        return False

    items = [i for i in items if i["id"] and i["id"] != "None"]
    if not items:
        # 0 productos = HTML cambiado o bloqueo (WAF). Nunca lo confundimos
        # con "no hay nada nuevo": si guardaramos el estado vacio, la
        # proxima ejecucion "descubriria" todo el listado como nuevo.
        log(
            f"[{nombre}] ERROR: 0 productos extraidos. Revisa con "
            f"'python cc_bot.py --dump --solo {nombre}' (WAF o cambio de HTML).",
            "ERROR",
        )
        return False

    filtrados = [i for i in items if pasa_filtros(i, busqueda)]
    vistos = cargar_vistos(nombre)
    ahora = datetime.now(timezone.utc).isoformat()
    nuevos = [i for i in filtrados if i["id"] not in vistos]

    for i in filtrados:
        vistos[i["id"]] = ahora
    guardar_vistos(nombre, vistos)

    if modo_seed:
        log(f"[{nombre}] Seed: {len(filtrados)} productos marcados como vistos, sin avisar.")
        return True

    log(f"[{nombre}] {len(items)} extraidos / {len(filtrados)} tras filtros / {len(nuevos)} nuevos")
    if nuevos:
        for i in nuevos:
            log(f"[{nombre}]   NUEVO: {i['titulo'][:60]} | {i['precio']} | {i['url']}")
        if not webhook_url:
            log(f"[{nombre}] DISCORD_WEBHOOK_URL sin configurar, no se puede avisar", "ERROR")
            return False
        for n, i in enumerate(nuevos[:MAX_FICHAS]):
            if n:
                time.sleep(1)   # cortesia entre fichas, no tras la ultima
            i["ficha"] = ficha_tecnica(i["url"])
        avisar(nuevos, webhook_url, nombre)
    return True


# ---------------------------------------------------------------------------
# MAIN
# ---------------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser(description="Monitor de Cash Converters -> Discord")
    ap.add_argument("--dump", action="store_true", help="guarda el HTML crudo de cada busqueda y sale")
    ap.add_argument("--seed", action="store_true", help="marca todo como visto sin avisar")
    ap.add_argument("--solo", metavar="NOMBRE", help="ejecuta solo la busqueda con ese nombre")
    args = ap.parse_args()

    config = cargar_config()
    webhook_url = obtener_webhook()

    busquedas = config.get("busquedas", [])
    if not busquedas:
        log("config.json no define ninguna busqueda", "ERROR")
        return 1

    if args.solo:
        busquedas = [b for b in busquedas if b.get("nombre") == args.solo]
        if not busquedas:
            log(f"No existe la busqueda '{args.solo}' en config.json", "ERROR")
            return 1

    STATE_DIR.mkdir(exist_ok=True)

    fallos = 0
    for idx, busqueda in enumerate(busquedas):
        if not procesar_busqueda(busqueda, webhook_url, args.dump, args.seed):
            fallos += 1
        if idx < len(busquedas) - 1:
            time.sleep(ESPERA_ENTRE_BUSQUEDAS)

    if fallos:
        log(f"{fallos} de {len(busquedas)} busquedas han fallado", "ERROR")
    return 1 if fallos else 0


if __name__ == "__main__":
    sys.exit(main())
